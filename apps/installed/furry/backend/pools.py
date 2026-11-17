"""Pools"""

import time

from flask import Blueprint, jsonify, request as flask_request


from core.db import get_db, qq

bp = Blueprint("pools", __name__)

# Усі «учасники» пулів: файли, що лежать у теці пулу (furry_posts.subfolder),
# і файли, привʼязані до пулу додатково (furry_pool_links — та сама робота
# в кількох пулах). Скрізь, де рахуються чи показуються пули, — через це.
#
# Умову (автор, пул, файл) треба вписувати В КОЖНУ половину UNION, а не
# ставити WHERE зовні: старіші MariaDB/MySQL не проштовхують зовнішню умову
# всередину й спершу збирають ВЕСЬ архів у тимчасову таблицю — на сотнях
# тисяч файлів картка арта відкривалась по кілька хвилин.
def pool_members(cond: str = "", args=()):
    """
    (sql, args) — підзапит учасників пулів з умовою в обох половинах.
    cond пишеться через {file_id}, {artist_id}, {pool_name}, напр.
    "{artist_id}=%s AND {pool_name}=%s"; args — для однієї половини.
    """
    def side(file_id, artist_id, pool_name):
        return cond.format(file_id=file_id, artist_id=artist_id, pool_name=pool_name)
    fp_cond = f" AND ({side('fp.file_id', 'fp.artist_id', 'fp.subfolder')})" if cond else ""
    pl_cond = f" WHERE {side('pl.file_id', 'pl.artist_id', 'pl.pool_name')}" if cond else ""
    sql = f"""(
    SELECT fp.file_id, fp.artist_id, fp.subfolder AS pool_name, fp.pool_order, fp.source, 0 AS linked
      FROM furry_posts fp WHERE fp.subfolder IS NOT NULL AND fp.subfolder <> ''{fp_cond}
    UNION ALL
    SELECT pl.file_id, pl.artist_id, pl.pool_name, pl.pool_order, pl.source, 1 AS linked
      FROM furry_pool_links pl{pl_cond}
)"""
    return sql, list(args) * 2



def all_pools(db, hidden_folders=None) -> list:
    """
    Повний список пулів з кількістю сторінок. Два звичайні GROUP BY (свої
    файли й привʼязки) і додавання в Python — без UNION усього архіву в
    тимчасову таблицю, яка на повільному диску будувалась хвилинами.
    """
    args, hide = [], ""
    if hidden_folders:
        hide = f" AND a.folder_name NOT IN ({','.join(['%s'] * len(hidden_folders))})"
        args = list(hidden_folders)
    counts = {}
    names = {}
    for sql in (f"""SELECT fp.artist_id, fp.subfolder AS name, fp.source, COUNT(*) AS cnt, a.artist_name
                    FROM furry_posts fp JOIN artists a ON a.id = fp.artist_id
                    WHERE fp.subfolder IS NOT NULL AND fp.subfolder <> ''{hide}
                    GROUP BY fp.artist_id, fp.subfolder, fp.source""",
                f"""SELECT pl.artist_id, pl.pool_name AS name, pl.source, COUNT(*) AS cnt, a.artist_name
                    FROM furry_pool_links pl JOIN artists a ON a.id = pl.artist_id
                    WHERE 1=1{hide}
                    GROUP BY pl.artist_id, pl.pool_name, pl.source"""):
        try:
            rows = qq(db, sql, args)
        except Exception:
            rows = []          # таблиці привʼязок ще немає
        for r in rows:
            k = (r["artist_id"], r["name"], r["source"])
            counts[k] = counts.get(k, 0) + int(r["cnt"])
            names[r["artist_id"]] = r["artist_name"]
    out = [{"name": k[1], "artist_id": k[0], "artist_name": names.get(k[0]), "source": k[2], "cnt": n}
           for k, n in counts.items()]
    out.sort(key=lambda r: -r["cnt"])
    return out


def pool_size(cur, artist_id, pool_name) -> int:
    """Скільки сторінок пулу в архіві (свої + привʼязані) — по індексах."""
    cur.execute("""SELECT (SELECT COUNT(*) FROM furry_posts WHERE artist_id=%s AND subfolder=%s)
                        + (SELECT COUNT(*) FROM furry_pool_links WHERE artist_id=%s AND pool_name=%s) AS n""",
                (artist_id, pool_name, artist_id, pool_name))
    return int(cur.fetchone()["n"] or 0)


def add_link(db, file_id, artist_id, pool_name, pool_order=None, source=None):
    """
    Додати файл у ще один пул (без копії файлу). Повертає True, якщо
    з'явилась нова привʼязка. Пул, у теці якого файл і так лежить, не
    дублюється.
    """
    if not pool_name:
        return False
    with db.cursor() as cur:
        cur.execute("SELECT subfolder FROM furry_posts WHERE file_id=%s", (file_id,))
        row = cur.fetchone()
        if row and (row["subfolder"] or "") == pool_name:
            return False
        cur.execute("""INSERT INTO furry_pool_links (file_id, artist_id, pool_name, pool_order, source)
                       VALUES (%s,%s,%s,%s,%s)
                       ON DUPLICATE KEY UPDATE pool_order = COALESCE(VALUES(pool_order), pool_order)""",
                    (file_id, artist_id, pool_name, pool_order, source))
        added = cur.rowcount == 1
    _invalidate_pools_cache()
    return added


def remove_link(db, file_id, pool_name):
    with db.cursor() as cur:
        cur.execute("DELETE FROM furry_pool_links WHERE file_id=%s AND pool_name=%s", (file_id, pool_name))
        n = cur.rowcount
    _invalidate_pools_cache()
    return n > 0


def set_order(cur, file_id, pool_order, pool_name=None):
    """Номер сторінки файлу в пулі: у «рідному» пулі — furry_posts,
    у додатковому — furry_pool_links."""
    if pool_name:
        cur.execute("SELECT subfolder FROM furry_posts WHERE file_id=%s", (file_id,))
        row = cur.fetchone()
        if not row or (row["subfolder"] or "") != pool_name:
            cur.execute("UPDATE furry_pool_links SET pool_order=%s WHERE file_id=%s AND pool_name=%s",
                        (pool_order, file_id, pool_name))
            return
    cur.execute("UPDATE furry_posts SET pool_order=%s WHERE file_id=%s", (pool_order, file_id))


_pools_cache = {"data": None, "ts": 0}
_POOLS_CACHE_TTL = 3600  # 1 год — той самий підхід, що й для тегів

def _invalidate_pools_cache():
    _pools_cache["data"] = None

def _rebuild_pools_cache():
    """Рахує непрофільтрований список пулів (найважчий випадок — без
    artist_id/tag_id звужень, скан усієї таблиці) і кладе в кеш. Викликається
    і при старті, і фоновим потоком регулярно, як і для тегів."""
    try:
        db   = get_db()
        rows = all_pools(db)
        db.close()
        _pools_cache["data"] = rows
        _pools_cache["ts"]   = time.time()
        return True
    except Exception as e:
        print(f"  ⚠ Не вдалося оновити кеш пулів: {e}")
        return False

def _pools_cache_background_loop():
    while True:
        time.sleep(_POOLS_CACHE_TTL - 30)
        _rebuild_pools_cache()

@bp.route("/api/pools")
def api_pools():
    """Список усіх pool (унікальних subfolder) з кількістю файлів — для пікера в галереї."""
    artist_id = flask_request.args.get("artist_id", None)
    tag_ids   = flask_request.args.getlist("tag_id")

    # Непрофільтрований випадок (без автора й тегів) — найважчий (скан усієї
    # таблиці), тому саме його кешуємо так само, як і теги.
    if not artist_id and not tag_ids:
        if _pools_cache["data"] is None:
            _rebuild_pools_cache()
        return jsonify(_pools_cache["data"] or [])

    conds = []
    args  = []
    if artist_id:
        conds.append("{artist_id} = %s")
        args.append(int(artist_id))
    if tag_ids:
        placeholders = ",".join(["%s"] * len(tag_ids))
        conds.append(f"""
            EXISTS (
                SELECT dt.download_id FROM download_tags dt
                WHERE dt.download_id = {{file_id}} AND dt.tag_id IN ({placeholders})
                GROUP BY dt.download_id
                HAVING COUNT(DISTINCT dt.tag_id) = %s
            )
        """)
        args.extend(int(t) for t in tag_ids)
        args.append(len(tag_ids))
    members, margs = pool_members(" AND ".join(conds), args)
    try:
        db   = get_db()
        rows = qq(db, f"""
            SELECT d.pool_name AS name, d.artist_id, a.artist_name, d.source, COUNT(*) AS cnt
            FROM {members} d
            JOIN artists a ON a.id = d.artist_id
            GROUP BY d.artist_id, d.pool_name, d.source
            ORDER BY cnt DESC
        """, margs)
        db.close()
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 503


# ─────────────────────────────────────────────────────────────────────
#  Серії для ЧУЖИХ архівів
#
#  Хтось підключив твій архів собі — і хоче гортати його серіями.
#  Той самий список, що й /api/pools, але:
#    * доступ за ключем, а не за сесією;
#    * серії з прихованих для цього ключа тек не показуються взагалі.
#  Старий код цієї адреси не має — тоді проксі нижче скаже "не вміє",
#  і інтерфейс просто сховає кнопку серій для цього архіву.
# ─────────────────────────────────────────────────────────────────────

@bp.route("/api/external-access/pools")
def api_external_access_pools():
    from blueprints.federation import _validate_external_key, get_hidden_folders_for_key
    key = flask_request.headers.get("X-Access-Key", "")
    valid, key_id = _validate_external_key(key, flask_request.headers.get("X-Identity", ""))
    if not valid:
        return jsonify({"error": "Ключ доступу недійсний або деактивований"}), 403

    hidden = get_hidden_folders_for_key(key_id)
    db = get_db()
    try:
        return jsonify(all_pools(db, hidden))
    finally:
        db.close()


@bp.route("/api/external-archives/<archive_id>/pools")
def api_external_archive_pools(archive_id):
    """Список серій підключеного чужого архіву."""
    import requests as req
    from blueprints.federation import _get_external_cfg_or_404
    cfg = _get_external_cfg_or_404(archive_id)
    if not cfg:
        return jsonify({"error": "Архів не знайдено"}), 404
    base = cfg.get("base_url", "").rstrip("/")
    try:
        r = req.get(f"{base}/api/external-access/pools", headers={
            "X-Access-Key": cfg.get("access_key", ""),
            "X-Identity":   cfg.get("my_identity", ""),
        }, timeout=15)
    except req.exceptions.RequestException as e:
        return jsonify({"error": f"Не вдалося звернутись до архіву: {e}"}), 502
    if r.status_code == 404:
        # у того архіву стара версія — адреси серій там ще немає
        return jsonify({"unsupported": True, "pools": []})
    if r.status_code == 403:
        return jsonify({"error": "Ключ доступу недійсний або деактивований власником архіву"}), 403
    return jsonify({"pools": r.json()})
