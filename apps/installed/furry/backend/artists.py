"""Художники, pools, злиття"""

import os, re, shutil

from flask import Blueprint, jsonify, request as flask_request


from .pools import _invalidate_pools_cache
from . import poolstore
from blueprints.tags import _invalidate_tags_cache
from core.auth import admin_required, moderator_required, login_required, current_user_id
from core import modlog
from core.db import get_db, q, qq
from core.warmcache import WarmCache
from .library import resync_rel_path
from core.storage_api import get_storage
from core.storage import get_storage_mode, local_file_path

bp = Blueprint("artists", __name__)


# ── Artists ───────────────────────────────────────────────────────────────

_KIND_SQL = ("CASE WHEN f2.ext IN ('png','jpg','jpeg','gif','webp') THEN 'image' "
             "WHEN f2.ext IN ('webm','mp4') THEN 'video' ELSE 'other' END")
_VIEW_KINDS = {"all": ("image", "video", "other"), "image": ("image",), "video": ("video",)}


def _artist_rows(db):
    """
    Усе для вкладки «Автори» й вибору автора — один прохід по постах:
      views       — готові списки для «усе / фото / відео»;
      file_artist — автор кожного файлу за його id (масив чисел), щоб
                    фільтр за тегами визначав авторів у пам'яті, а не
                    перебором усього архіву.
    """
    import pymysql
    from array import array
    meta = {r["id"]: r for r in qq(db, """SELECT id, folder_name, artist_name, created_at, auto_update
                                          FROM artists""")}
    pending = {r["artist_id"]: int(r["n"]) for r in qq(db, """SELECT artist_id, COUNT(*) AS n
                                                             FROM pending_duplicates GROUP BY artist_id""")}
    aliases = {}
    for r in qq(db, "SELECT artist_id, alias_name FROM artist_aliases ORDER BY alias_name"):
        aliases.setdefault(r["artist_id"], []).append(r["alias_name"])
    stats = {}           # artist_id -> {kind: [files, e621, ib, size]}, last
    last = {}
    for r in qq(db, f"""SELECT fp.artist_id, fp.source, {_KIND_SQL} AS k, COUNT(*) AS n,
                               COALESCE(SUM(f2.size_bytes),0) AS sz, MAX(fp.downloaded_at) AS last
                        FROM furry_posts fp JOIN files f2 ON f2.id = fp.file_id
                        GROUP BY fp.artist_id, fp.source, k"""):
        a = stats.setdefault(r["artist_id"], {})
        c = a.setdefault(r["k"], [0, 0, 0, 0])
        n = int(r["n"])
        c[0] += n
        if r["source"] == "e621":
            c[1] += n
        elif r["source"] == "inkbunny":
            c[2] += n
        c[3] += int(r["sz"] or 0)
        if r["last"] and (last.get(r["artist_id"]) is None or r["last"] > last[r["artist_id"]]):
            last[r["artist_id"]] = r["last"]
    views = {}
    for view, kinds in _VIEW_KINDS.items():
        rows = []
        for aid, by_kind in stats.items():
            m = meta.get(aid)
            if not m:
                continue
            tot = [sum(by_kind.get(k, (0, 0, 0, 0))[i] for k in kinds) for i in range(4)]
            if tot[0] <= 0:
                continue
            rows.append({
                "id": aid, "folder_name": m["folder_name"], "artist_name": m["artist_name"],
                "created_at": m["created_at"].isoformat() if m["created_at"] else None,
                "auto_update": m["auto_update"],
                "total_files": tot[0], "e621_files": tot[1], "ib_files": tot[2], "total_size": tot[3],
                "last_download": last[aid].isoformat() if last.get(aid) else None,
                "pending_count": pending.get(aid, 0),
                "aliases": [x for x in aliases.get(aid, [])
                            if x.lower() not in (m["artist_name"].lower(), m["folder_name"].lstrip("@").lower())],
            })
        rows.sort(key=lambda r: (-r["total_files"], r["id"]))
        views[view] = rows
    with db.cursor(pymysql.cursors.Cursor) as cur:
        cur.execute("SELECT file_id, artist_id FROM furry_posts")
        pairs = cur.fetchall()
    fa = array("i", [0]) * ((max((p[0] for p in pairs), default=0)) + 1)
    for fid, aid in pairs:
        fa[fid] = aid
    return {"views": views, "file_artist": fa}


# Рахується на старті й оновлюється у фоні після змін (core/warmcache.py),
# тож вкладка «Автори» і вибір автора відкриваються миттєво — з будь-яким
# фільтром і тегами.
artists_cache = WarmCache("artists", _artist_rows)


def _artists_with_tags(tag_ids) -> set:
    """Автори, у яких є хоч один файл з усіма цими тегами."""
    import pymysql
    files = None
    db = get_db()
    try:
        with db.cursor(pymysql.cursors.Cursor) as cur:
            for tid in dict.fromkeys(int(t) for t in tag_ids):
                cur.execute("SELECT download_id FROM download_tags WHERE tag_id=%s", (tid,))
                got = [r[0] for r in cur.fetchall()]
                files = set(got) if files is None else files.intersection(got)
                if not files:
                    return set()
            fa = artists_cache.get()["file_artist"]
            n = len(fa)
            out = {fa[i] for i in files if i < n}
            newer = [i for i in files if i >= n]       # додане після підрахунку
            if newer:
                ph = ",".join(["%s"] * len(newer))
                cur.execute(f"SELECT DISTINCT artist_id FROM furry_posts WHERE file_id IN ({ph})", newer)
                out.update(r[0] for r in cur.fetchall())
            out.discard(0)
            return out
    finally:
        db.close()


@bp.route("/api/artists")
@moderator_required
def api_artists():
    f = flask_request.args.get("filter", "all")
    tag_ids = flask_request.args.getlist("tag_id")
    sort = flask_request.args.get("sort", "files")  # files | size
    try:
        rows = artists_cache.get()["views"].get(f if f in _VIEW_KINDS else "all", [])
        if tag_ids:
            keep = _artists_with_tags(tag_ids)
            rows = [r for r in rows if r["id"] in keep]
        if sort == "size":
            rows = sorted(rows, key=lambda r: (-r["total_size"], r["id"]))
        return jsonify(_with_favorites(rows))
    except Exception as e:
        return jsonify({"error": str(e)}), 503


# ── Автори для підключених архівів ───────────────────────────────────

@bp.route("/api/external-access/artists")
def api_external_access_artists():
    """Автори цього архіву для вибору автора в підключеному архіві (без прихованих для ключа тек)."""
    from blueprints.federation import external_key_or_403
    key_id, hidden = external_key_or_403()
    if key_id is None:
        return hidden
    f = flask_request.args.get("filter", "all")
    rows = artists_cache.get()["views"].get(f if f in _VIEW_KINDS else "all", [])
    return jsonify([{"id": r["id"], "artist_name": r["artist_name"], "folder_name": r["folder_name"],
                     "total_files": r["total_files"]} for r in rows if r["folder_name"] not in hidden])


@bp.route("/api/external-archives/<archive_id>/artists")
def api_external_archive_artists(archive_id):
    from blueprints.federation import proxy_external_list
    return proxy_external_list(archive_id, "/api/external-access/artists", "artists",
                               {"filter": flask_request.args.get("filter", "all")})


# ── Улюблені автори (у кожного користувача свої) ─────────────────────

def favorite_artist_ids(user_id) -> set:
    if not user_id:
        return set()
    try:
        return {r["artist_id"] for r in qq(get_db(), "SELECT artist_id FROM furry_favorite_artists WHERE user_id=%s",
                                           (user_id,))}
    except Exception:
        return set()       # таблиці ще немає


def _with_favorites(rows):
    """Позначка «улюблений» для поточного користувача; улюблені — першими.
    Рядки з кешу спільні для всіх — тож копії, а не зміна на місці."""
    fav = favorite_artist_ids(current_user_id())
    out = [dict(r, is_fav=r["id"] in fav) for r in rows]
    out.sort(key=lambda r: not r["is_fav"])        # стабільне: решта порядку не змінюється
    return out


@bp.route("/api/favorite-artists")
@login_required
def api_favorite_artists():
    """Улюблені автори поточного користувача (для каталогу й галереї)."""
    try:
        rows = qq(get_db(), """SELECT a.id, a.folder_name, a.artist_name FROM furry_favorite_artists fa
                               JOIN artists a ON a.id = fa.artist_id
                               WHERE fa.user_id=%s ORDER BY a.artist_name""", (current_user_id(),))
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/favorite-artists", methods=["POST"])
@login_required
def api_favorite_artist_by_folder():
    """♥ автора за текою (каталог): {"folder": "@нік", "fav": true|false}."""
    body = flask_request.get_json(silent=True) or {}
    row = q(get_db(), "SELECT id FROM artists WHERE folder_name=%s", ((body.get("folder") or "").strip(),))
    if not row:
        return jsonify({"error": "Це не тека автора"}), 404
    return api_artist_favorite(row["id"])


@bp.route("/api/artists/<int:artist_id>/favorite", methods=["POST"])
@login_required
def api_artist_favorite(artist_id):
    """♥ автора: {"fav": true|false}; без тіла — перемкнути."""
    body = flask_request.get_json(silent=True) or {}
    uid = current_user_id()
    try:
        db = get_db()
        if not q(db, "SELECT 1 AS x FROM artists WHERE id=%s", (artist_id,)):
            return jsonify({"error": "Автора не знайдено"}), 404
        now = q(db, "SELECT 1 AS x FROM furry_favorite_artists WHERE user_id=%s AND artist_id=%s", (uid, artist_id))
        want = bool(body["fav"]) if "fav" in body else not now
        with db.cursor() as cur:
            if want:
                cur.execute("INSERT IGNORE INTO furry_favorite_artists (user_id, artist_id) VALUES (%s,%s)", (uid, artist_id))
            else:
                cur.execute("DELETE FROM furry_favorite_artists WHERE user_id=%s AND artist_id=%s", (uid, artist_id))
        db.commit()
        return jsonify({"ok": True, "fav": want})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/artist/<int:artist_id>/downloads")
@moderator_required
def api_artist_downloads(artist_id):
    page   = int(flask_request.args.get("page", 1))
    limit  = int(flask_request.args.get("limit", 100))
    offset = (page - 1) * limit
    try:
        db = get_db()
        owner = q(db, "SELECT id FROM artists WHERE id=%s", (artist_id,))
        if not owner:
            db.close()
            return jsonify({"error": "not found"}), 404
        rows  = qq(db, """
            SELECT fp.post_id, fp.source, fp.post_url, f.name AS filename, f.ext AS file_ext,
                   f.width, f.height, fp.page_num, fp.downloaded_at
            FROM furry_posts fp JOIN files f ON f.id = fp.file_id WHERE fp.artist_id=%s
            ORDER BY fp.downloaded_at DESC LIMIT %s OFFSET %s
        """, (artist_id, limit, offset))
        total = q(db, "SELECT COUNT(*) as c FROM furry_posts WHERE artist_id=%s", (artist_id,))["c"]
        db.close()
        for r in rows:
            if r["downloaded_at"]: r["downloaded_at"] = r["downloaded_at"].isoformat()
        return jsonify({"items": rows, "total": int(total), "page": page})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


# ── Recent ────────────────────────────────────────────────────────────────

@bp.route("/api/artists/<int:artist_id>/forget", methods=["DELETE"])
@admin_required
def api_artist_forget(artist_id):
    """
    Прибирає автора і всі його записи з БД — фізичні файли НЕ чіпає
    взагалі. Для випадків типу помилково проіндексованої особистої папки:
    файли лишаються на диску (і виключені зі сканування назавжди), просто
    записи про них зникають з "Автори"/"Арти".
    """
    try:
        db  = get_db()
        row = q(db, "SELECT artist_name, folder_name FROM artists WHERE id=%s", (artist_id,))
        if not row:
            db.close()
            return jsonify({"error": "Автора не знайдено"}), 404
        with db.cursor() as cur:
            cur.execute("DELETE FROM artists WHERE id=%s", (artist_id,))  # downloads підуть каскадом
        db.commit(); db.close()
        return jsonify({"ok": True, "removed": row["artist_name"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/artists/<int:artist_id>/merge-log")
def api_artists_merge_log(artist_id):
    """Журнал того, які файли виявились візуальними дублікатами під час
    об'єднання авторів у цього автора — 'e621_123 і e621_595 це один і той
    самий арт, лишили тільки один'."""
    try:
        db   = get_db()
        rows = qq(db, """
            SELECT kept_post_id, kept_filename, removed_post_id, removed_filename, hamming_distance, created_at
            FROM merge_duplicate_log WHERE artist_id=%s ORDER BY created_at DESC
        """, (artist_id,))
        db.close()
        for r in rows:
            if r["created_at"]: r["created_at"] = r["created_at"].isoformat()
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/artists/auto-update-bulk", methods=["POST"])
@admin_required
def api_artists_toggle_auto_update_bulk():
    """Вмикає/вимикає авто-оновлення одразу для ВСІХ авторів — кнопки
    'Обрати всіх'/'Зняти всіх' у списку авторів."""
    body = flask_request.get_json(silent=True) or {}
    enabled = 1 if body.get("enabled") else 0
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("UPDATE artists SET auto_update=%s", (enabled,))
            count = cur.rowcount
        db.commit(); db.close()
        return jsonify({"ok": True, "count": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/artists/<int:artist_id>/auto-update", methods=["POST"])
@moderator_required
def api_artists_toggle_auto_update(artist_id):
    """Перемикає, чи перевіряти цього автора на нове при щоденному
    авто-оновленні."""
    body = flask_request.get_json(silent=True) or {}
    enabled = 1 if body.get("enabled") else 0
    try:
        db = get_db()
        art = q(db, "SELECT artist_name, auto_update FROM artists WHERE id=%s", (artist_id,))
        with db.cursor() as cur:
            cur.execute("UPDATE artists SET auto_update=%s WHERE id=%s", (enabled, artist_id))
        db.commit(); db.close()
        if art and bool(art["auto_update"]) != bool(enabled):
            modlog.record("artist.auto_update", f"«{art['artist_name']}»: {'увімкнено' if enabled else 'вимкнено'}",
                          {"artist_id": artist_id, "before": int(bool(art["auto_update"]))})
        return jsonify({"ok": True, "auto_update": bool(enabled)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Інші назви автора ─────────────────────────────────────────────────
#
#  Той самий автор під іншим ніком (на іншому сайті чи після перейменування).
#  Назва записується до автора в архіві: «Онлайн» показує його як уже
#  відомого, а архіватор під час оновлення шукає роботи й за цією назвою —
#  нове скачується в теку автора, копії відсіюються як звичайно.

def _alias_rows(db, artist_id):
    return qq(db, "SELECT id, alias_name FROM artist_aliases WHERE artist_id=%s ORDER BY alias_name", (artist_id,))


@bp.route("/api/artists/<int:artist_id>/aliases")
def api_artist_aliases(artist_id):
    db = get_db()
    try:
        return jsonify(list(_alias_rows(db, artist_id)))
    finally:
        db.close()


@bp.route("/api/artists/<int:artist_id>/aliases", methods=["POST"])
@moderator_required
def api_artist_alias_add(artist_id):
    """{"name": "інший_нік"} — приєднати назву до автора."""
    name = ((flask_request.get_json(silent=True) or {}).get("name") or "").strip().lstrip("@").strip()
    if not name or len(name) > 255:
        return jsonify({"error": "Вкажи назву"}), 400
    db = get_db()
    try:
        artist = q(db, "SELECT id, artist_name, folder_name FROM artists WHERE id=%s", (artist_id,))
        if not artist:
            return jsonify({"error": "Автора не знайдено"}), 404
        if name.lower() in (artist["artist_name"].lower(), artist["folder_name"].lstrip("@").lower()):
            return jsonify({"ok": True, "already": True})
        other = q(db, """SELECT id, artist_name FROM artists
                         WHERE (artist_name=%s OR folder_name=%s) AND id<>%s""", (name, f"@{name}", artist_id))
        if other:
            return jsonify({"error": f"«{name}» — це окремий автор в архіві («{other['artist_name']}»). "
                                     f"Об'єднай їх кнопкою 🔗 у вкладці «Автори»."}), 409
        taken = q(db, """SELECT a.artist_name FROM artist_aliases al JOIN artists a ON a.id=al.artist_id
                         WHERE al.alias_name=%s AND al.artist_id<>%s""", (name, artist_id))
        if taken:
            return jsonify({"error": f"«{name}» уже записана як інша назва автора «{taken['artist_name']}»"}), 409
        with db.cursor() as cur:
            cur.execute("INSERT IGNORE INTO artist_aliases (artist_id, alias_name) VALUES (%s,%s)", (artist_id, name))
        db.commit()
    finally:
        db.close()
    artists_cache.invalidate()
    from core import modlog
    modlog.record("artist.alias", f"«{name}» → автор «{artist['artist_name']}»",
                  {"artist_id": artist_id, "name": name})
    return jsonify({"ok": True, "artist_name": artist["artist_name"]})


@bp.route("/api/artists/<int:artist_id>/aliases/<int:alias_id>", methods=["DELETE"])
@moderator_required
def api_artist_alias_remove(artist_id, alias_id):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM artist_aliases WHERE id=%s AND artist_id=%s", (alias_id, artist_id))
        db.commit()
    finally:
        db.close()
    artists_cache.invalidate()
    return jsonify({"ok": True})


@bp.route("/api/artists/check-name")
def api_artists_check_name():
    """
    Перевіряє, чи цей нік уже відомий у бібліотеці — або як точна папка
    автора, або як заявлений раніше псевдонім (через об'єднання). Викликається
    ПЕРЕД завантаженням автора, щоб попередити про можливий дубль-акаунт.
    """
    name = (flask_request.args.get("name") or "").strip().lstrip("@")
    if not name:
        return jsonify({"exists": False})
    folder_name = f"@{name}"
    try:
        db = get_db()
        row = q(db, "SELECT id, artist_name, folder_name FROM artists WHERE folder_name=%s OR artist_name=%s",
                (folder_name, name))
        if not row:
            arow = q(db, """
                SELECT a.id, a.artist_name, a.folder_name FROM artist_aliases al
                JOIN artists a ON a.id = al.artist_id
                WHERE al.alias_name = %s
            """, (name,))
            row = arow
        db.close()
        if row:
            return jsonify({"exists": True, "artist_id": row["id"], "artist_name": row["artist_name"], "folder_name": row["folder_name"]})
        return jsonify({"exists": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


import re as _re_natsort

def _natural_sort_key(s: str):
    """Природне сортування — '2.jpg' йде перед '10.jpg', а не як звичайний
    текстовий порядок (де '10' випадково опинився б перед '2')."""
    return [int(t) if t.isdigit() else t.lower() for t in _re_natsort.split(r'(\d+)', s)]


@bp.route("/api/artists/<int:artist_id>/pool-editor")
def api_artist_pool_editor(artist_id):
    """
    Список файлів для редактора pool — або конкретного pool (subfolder=X),
    або файлів автора без жодного pool (subfolder порожній/не задано).
    """
    subfolder = flask_request.args.get("subfolder", "")
    try:
        db = get_db()
        if subfolder:
            # сторінки пулу: і з його теки, і привʼязані з інших пулів
            with db.cursor() as cur:
                pid = poolstore.find_pool(cur, artist_id, subfolder)
            rows = qq(db, """
                SELECT f.id, f.name AS filename, i.page AS pool_order, f.ext AS file_ext,
                       1 - i.via_folder AS linked
                FROM furry_pool_items i JOIN files f ON f.id = i.file_id
                WHERE i.pool_id = %s
                ORDER BY i.page IS NULL, i.page ASC, f.id ASC
            """, (pid,)) if pid else []
        else:
            rows = qq(db, """
                SELECT f.id, f.name AS filename, fp.pool_order, f.ext AS file_ext
                FROM furry_posts fp JOIN files f ON f.id = fp.file_id
                WHERE fp.artist_id=%s AND (fp.subfolder IS NULL OR fp.subfolder='') ORDER BY f.id ASC
            """, (artist_id,))
        db.close()
        for r in rows:
            r["stream_url"] = f"/api/gallery/stream/{r['id']}"
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/artists/<int:artist_id>/pool-names")
def api_artist_pool_names(artist_id):
    """Список назв усіх pool цього автора — для вибору в редакторі."""
    try:
        db   = get_db()
        rows = qq(db, "SELECT name FROM furry_pools WHERE artist_id=%s ORDER BY name", (artist_id,))
        db.close()
        return jsonify([r["name"] for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/downloads/<int:download_id>/set-page", methods=["POST"])
@moderator_required
def api_download_set_page(download_id):
    """Проставляє номер сторінки (pool_order) для ОДНОГО конкретного файлу."""
    body  = flask_request.get_json(silent=True) or {}
    order = body.get("pool_order")
    pool  = (body.get("pool") or "").strip() or None   # у якому пулі (файл може бути в кількох)
    try:
        db = get_db()
        old = q(db, """SELECT fp.artist_id, fp.pool_order, fp.subfolder, f.name FROM furry_posts fp
                       JOIN files f ON f.id = fp.file_id WHERE fp.file_id=%s""", (download_id,))
        pid = None
        with db.cursor() as cur:
            if old:
                pid = poolstore.find_pool(cur, old["artist_id"], pool or old["subfolder"])
            if pid:
                cur.execute("SELECT page FROM furry_pool_items WHERE pool_id=%s AND file_id=%s", (pid, download_id))
                item = cur.fetchone()
                if item:
                    old = {**old, "pool_order": item["page"], "subfolder": pool or old["subfolder"]}
                    poolstore.set_page(cur, download_id, pid, order)
                else:
                    pid = None
            if not pid:
                cur.execute("UPDATE furry_posts SET pool_order=%s WHERE file_id=%s", (order, download_id))
                poolstore.sync_file(cur, download_id)
        db.commit(); db.close()
        if old and old["pool_order"] != order:
            modlog.record("pool.set_page", f"«{old['name']}» у «{old['subfolder']}»: стор. {old['pool_order']} → {order}",
                          {"orders": [{"file_id": download_id, "pool_order": old["pool_order"], "pool_id": pid}]})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/artists/<int:artist_id>/pool-assign", methods=["POST"])
@moderator_required
def api_artist_pool_assign(artist_id):
    """
    Переносить обрані роботи (уже наявні в архіві, у цього ж автора) у
    pool з вказаною назвою — створює його, якщо такого ще нема. Реально
    переміщує файл на диску/Nextcloud, щоб subfolder у БД відповідав
    справжньому місцю файлу — так само, як і решта переносів у дашборді.
    """
    body          = flask_request.get_json(silent=True) or {}
    download_ids  = body.get("download_ids") or []
    pool_name     = (body.get("pool_name") or "").strip()
    if not download_ids or not pool_name:
        return jsonify({"error": "Вкажи роботи і назву pool"}), 400
    try:
        db  = get_db()
        art = q(db, "SELECT folder_name FROM artists WHERE id=%s", (artist_id,))
        if not art:
            db.close()
            return jsonify({"error": "Автора не знайдено"}), 404
        placeholders = ",".join(["%s"] * len(download_ids))
        rows = qq(db, f"""SELECT f.id, f.name AS filename, fp.subfolder, fp.pool_order
                          FROM furry_posts fp JOIN files f ON f.id = fp.file_id
                          WHERE f.id IN ({placeholders}) AND fp.artist_id=%s""",
                  download_ids + [artist_id])
        # як було до переносу — для відміни з журналу модераторів
        before_rows = [{"file_id": r["id"], "subfolder": r["subfolder"], "pool_order": r["pool_order"]}
                       for r in rows]

        storage = get_storage()

        moved, failed = 0, 0
        for row in rows:
            try:
                # Шляхи всередині сховища: тека автора + (стара) підпапка + ім'я.
                # Теку призначення створює саме сховище.
                folder  = art["folder_name"]
                prefix  = "" if folder == "_root" else f"{folder}/"
                old_sub = f"{row['subfolder']}/" if row.get("subfolder") else ""
                src_rel = f"{prefix}{old_sub}{row['filename']}"
                dst_rel = f"{prefix}{pool_name}/{row['filename']}"
                if src_rel != dst_rel and not storage.exists(dst_rel):
                    storage.move(src_rel, dst_rel)
                moved += 1
            except Exception as e:
                failed += 1
                print(f"  ⚠ Не вдалося перемістити {row['filename']}: {e}")

        with db.cursor() as cur:
            # ці файли вже були в пулі додатково — запамʼятовуємо (для відміни),
            # тепер вони в ньому «рідні»
            pid = poolstore.ensure_pool(cur, artist_id, pool_name)
            cur.execute(f"""SELECT file_id, page AS pool_order FROM furry_pool_items
                            WHERE pool_id=%s AND via_folder=0 AND file_id IN ({placeholders})""",
                        [pid] + download_ids)
            removed_links = [{**r, "artist_id": artist_id, "pool_name": pool_name} for r in cur.fetchall()]
            cur.execute(f"UPDATE furry_posts SET subfolder=%s WHERE file_id IN ({placeholders})",
                        [pool_name] + download_ids)
            poolstore.sync_files(cur, download_ids)
            # Файли переїхали у сховищі — оновлюємо їхній шлях у ядрі,
            # інакше вони перестануть відкриватись.
            resync_rel_path(db, download_ids)
            # Одразу проставляємо очерговість — за природнім порядком назви,
            # щоб не лишати porожнім/незрозумілим
            cur.execute("""SELECT f.id, f.name AS filename, i.page AS pool_order
                           FROM furry_pool_items i JOIN files f ON f.id = i.file_id
                           WHERE i.pool_id = %s""", (pid,))
            pool_files = cur.fetchall()
            # сторінки, що вже були в пулі, теж перенумеруються — запамʼятовуємо
            moved_ids = {int(i) for i in download_ids}
            prev_orders = [{"file_id": r["id"], "pool_order": r["pool_order"], "pool_id": pid}
                           for r in pool_files if r["id"] not in moved_ids]
            pool_files.sort(key=lambda r: _natural_sort_key(r["filename"]))
            for order, r in enumerate(pool_files, start=1):
                poolstore.set_page(cur, r["id"], pid, order)
        db.commit(); db.close()
        modlog.record("pool.assign", f"{len(before_rows)} файл(ів) → пул «{pool_name}»",
                      {"artist_id": artist_id, "pool_name": pool_name, "files": before_rows,
                       "prev_pool_orders": prev_orders, "removed_links": removed_links})
        return jsonify({"ok": True, "moved": moved, "failed": failed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/artists/<int:artist_id>/resort-pool", methods=["POST"])
@moderator_required
def api_artist_resort_pool(artist_id):
    """
    Пересортовує вже наявний pool за природнім порядком назви файлу —
    для випадків, коли очерговість загубилась (напр. після перенесення
    автора в pool до цього фіксу).
    """
    body      = flask_request.get_json(silent=True) or {}
    subfolder = (body.get("subfolder") or "").strip()
    if not subfolder:
        return jsonify({"error": "Вкажи назву pool"}), 400
    try:
        db = get_db()
        with db.cursor() as cur:
            pid = poolstore.find_pool(cur, artist_id, subfolder)
            cur.execute("""SELECT f.id, f.name AS filename, i.page AS pool_order
                           FROM furry_pool_items i JOIN files f ON f.id = i.file_id
                           WHERE i.pool_id = %s""", (pid,))
            files = cur.fetchall()
            before = [{"file_id": r["id"], "pool_order": r["pool_order"], "pool_id": pid} for r in files]
            files.sort(key=lambda r: _natural_sort_key(r["filename"]))
            for order, r in enumerate(files, start=1):
                poolstore.set_page(cur, r["id"], pid, order)
        db.commit(); db.close()
        modlog.record("pool.resort", f"пул «{subfolder}» ({len(files)} стор.)", {"orders": before})
        return jsonify({"ok": True, "count": len(files)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/artists/<int:source_id>/merge", methods=["POST"])
@admin_required
def api_artists_merge(source_id):
    """
    Об'єднує "source" автора в "target" — реально переносить файли зі
    сховища, переприв'язує всі записи БД, і запам'ятовує стару назву як
    псевдонім (щоб наступного разу при спробі завантажити цей самий нік
    система одразу впізнала, що це вже відомий автор).
    """
    body = flask_request.get_json(silent=True) or {}
    target_id = body.get("target_artist_id")
    if not target_id or int(target_id) == source_id:
        return jsonify({"error": "Вкажи іншого автора для об'єднання"}), 400
    target_id = int(target_id)
    # Опційно — перенести не в корінь автора, а як pool (підпапку) з цією
    # назвою. Для випадку "цей 'автор' насправді просто pool/серія іншого".
    target_subfolder = (body.get("target_subfolder") or "").strip() or None

    try:
        db = get_db()
        src = q(db, "SELECT id, artist_name, folder_name FROM artists WHERE id=%s", (source_id,))
        tgt = q(db, "SELECT id, artist_name, folder_name FROM artists WHERE id=%s", (target_id,))
        if not src or not tgt:
            db.close()
            return jsonify({"error": "Автора не знайдено"}), 404

        rows = qq(db, """SELECT f.id, fp.post_id, f.name AS filename, fp.subfolder, ph.phash
                         FROM furry_posts fp JOIN files f ON f.id = fp.file_id
                         LEFT JOIN file_phash ph ON ph.file_id = f.id
                         WHERE fp.artist_id=%s""", (source_id,))
        tgt_rows = qq(db, """SELECT f.id, fp.post_id, f.name AS filename, ph.phash
                             FROM furry_posts fp JOIN files f ON f.id = fp.file_id
                             JOIN file_phash ph ON ph.file_id = f.id
                             WHERE fp.artist_id=%s""", (target_id,))

        # pHash-перевірка ПЕРЕД переносом — це той самий сценарій, про який
        # питали: "e621_123 у одного, e621_595 у іншого — це один і той самий
        # арт, просто різні ID". Точний post_id тут не збігається, тому раніше
        # такі випадки взагалі не ловились — обидва файли просто дублювались
        # би в об'єднаній бібліотеці. Тепер порівнюємо pHash кожного файлу
        # source з усіма файлами target (той самий XOR+popcount підхід, що
        # й у звичайній перевірці дублікатів при скачуванні).
        PHASH_THRESHOLD = 8
        skip_ids = set()      # download.id з source, які виявились дублікатами — не переносимо
        dup_log_rows = []     # для запису в merge_duplicate_log
        for row in rows:
            if row["phash"] is None:
                continue
            best_dist, best_match = PHASH_THRESHOLD + 1, None
            for trow in tgt_rows:
                dist = bin(row["phash"] ^ trow["phash"]).count("1")
                if dist <= PHASH_THRESHOLD and dist < best_dist:
                    best_dist, best_match = dist, trow
            if best_match:
                skip_ids.add(row["id"])
                dup_log_rows.append((target_id, best_match["post_id"], best_match["filename"],
                                      row["post_id"], row["filename"], best_dist))

        moved, skipped_dup, failed = 0, 0, 0

        for row in rows:
            sub = f"{row['subfolder']}/" if row.get("subfolder") else ""
            # Якщо перетворюємо на pool — усі файли йдуть в ОДНУ підпапку з
            # цією назвою, незалежно від того, яка підпапка (чи її
            # відсутність) була в них раніше
            dst_sub = f"{target_subfolder}/" if target_subfolder else sub
            if row["id"] in skip_ids:
                # Візуальний дублікат уже наявного файлу в target — прибираємо
                # СТАРУ копію фізично (не переносимо, не лишаємо запис у БД),
                # щоб не зберігати той самий арт двічі
                skipped_dup += 1
                try:
                    src_path = local_file_path(src["folder_name"], row["filename"], row.get("subfolder"))
                    if os.path.isfile(src_path):
                        os.remove(src_path)
                except Exception as e:
                    print(f"  ⚠ Не вдалося прибрати дублікат {row['filename']}: {e}")
                continue
            try:
                src_path = local_file_path(src["folder_name"], row["filename"], row.get("subfolder"))
                dst_path = local_file_path(tgt["folder_name"], row["filename"], target_subfolder or row.get("subfolder"))
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                if os.path.isfile(src_path) and not os.path.isfile(dst_path):
                    # shutil.move витримує перенесення між різними
                    # файловими системами, os.rename — ні
                    shutil.move(src_path, dst_path)
                moved += 1
            except Exception as e:
                failed += 1
                print(f"  ⚠ Не вдалося перенести {row['filename']}: {e}")

        with db.cursor() as cur:
            # Записуємо в журнал, що з чим збіглося — щоб ця інформація не
            # губилась (саме про це й було питання)
            for dl in dup_log_rows:
                cur.execute("""
                    INSERT INTO merge_duplicate_log
                        (artist_id, kept_post_id, kept_filename, removed_post_id, removed_filename, hamming_distance)
                    VALUES (%s,%s,%s,%s,%s,%s)
                """, dl)

            # Прибираємо з БД ті записи source, що виявились pHash-дублікатами
            if skip_ids:
                placeholders = ",".join(["%s"] * len(skip_ids))
                cur.execute(f"DELETE FROM files WHERE id IN ({placeholders})", list(skip_ids))

            # Перепривʼязуємо решту на нового автора; якщо той самий пост уже
            # є в цільового автора (точний збіг post_id) — UPDATE IGNORE просто
            # пропустить конфліктний рядок (через UNIQUE artist_id+post_id+page_num)
            if target_subfolder:
                cur.execute("UPDATE IGNORE downloads SET artist_id=%s, subfolder=%s WHERE artist_id=%s",
                            (target_id, target_subfolder, source_id))
            else:
                cur.execute("UPDATE IGNORE downloads SET artist_id=%s WHERE artist_id=%s", (target_id, source_id))
            # Пули старого автора — до нового (та сама серія склеюється),
            # інакше вони зникли б разом зі старим записом автора
            poolstore.move_artist_pools(cur, source_id, target_id)
            # Ще й точні збіги post_id, які не змогли перепривʼязатись — прибираємо
            cur.execute("DELETE FROM files WHERE id IN (SELECT file_id FROM furry_posts WHERE artist_id=%s)", (source_id,))

            if target_subfolder:
                # Проставляємо очерговість "по-новому" — за природнім
                # сортуванням назви файлу (щоб "2.jpg" йшло перед "10.jpg",
                # а не як звичайний текстовий порядок), а не лишаємо порожнім
                cur.execute("""SELECT f.id, f.name AS filename
                               FROM furry_posts fp JOIN files f ON f.id = fp.file_id
                               WHERE fp.artist_id=%s AND fp.subfolder=%s""",
                            (target_id, target_subfolder))
                pool_files = cur.fetchall()
                pool_files.sort(key=lambda r: _natural_sort_key(r["filename"]))
                for order, r in enumerate(pool_files, start=1):
                    cur.execute("UPDATE furry_posts SET pool_order=%s WHERE file_id=%s", (order, r["id"]))

            # Запам'ятовуємо стару назву як псевдонім — щоб наступного разу
            # при спробі завантажити цей самий нік система одразу впізнала
            for alias in {src["folder_name"].lstrip("@"), src["artist_name"]}:
                if alias:
                    cur.execute("INSERT IGNORE INTO artist_aliases (artist_id, alias_name) VALUES (%s,%s)",
                                (target_id, alias))
            # файли переїхали (й, можливо, в pool) — звіряємо пули нового автора
            poolstore.reconcile_artist(cur, target_id)
            poolstore.merge_duplicate_pools(cur, target_id)
            cur.execute("DELETE FROM artists WHERE id=%s", (source_id,))
        db.commit(); db.close()
        _invalidate_tags_cache(); _invalidate_pools_cache()
        return jsonify({"ok": True, "moved": moved, "failed": failed, "skipped_dup": skipped_dup})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
