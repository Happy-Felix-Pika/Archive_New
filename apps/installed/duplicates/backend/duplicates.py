"""Дублікати та pending-review"""

import hashlib
import base64, mimetypes, os

from flask import Blueprint, jsonify, request as flask_request


from core.auth import admin_required, current_role, get_user_settings, current_user_id, login_required, moderator_required
from core import modlog
from core.db import get_db, q, qq
from core.imgcache import _IMG_CACHE_DIR
from core.paths import PENDING_DIR
from core.storage_api import StorageError, get_storage
from core.files_api import TrashError, trash_file, get_file, local_path
from core.storage import get_storage_mode, local_file_path

_core = None   # підставляє register(): через нього йдуть задачі ядра

bp = Blueprint("duplicates", __name__)


# ── Pending duplicates ────────────────────────────────────────────────────
#
#  Pool (серія) у черзі на перевірку.
#
#  Автор часто викладає ту саму роботу і на e621, і на Inkbunny, а в серію
#  вона входить лише на одному сайті. Тому в кожній парі важливо знати:
#    * чи новий файл — сторінка pool (pool_folder / pool_order у записі черги,
#      пише архіватор);
#    * чи вже збережений файл лежить у pool (дивимось у furry_posts).
#  «Замінити» ставить новий файл на місце старого — у ту саму серію й на ту
#  саму сторінку. «Зберегти новий» кладе його в його власний pool.

_POOL_COLS = "p.pool_folder, p.pool_order, p.pool_priority,"
_pool_cols_cache = {}


def _has_pool_cols(db) -> bool:
    """Чи застосовано міграцію з колонками pool_* (кешується на процес)."""
    if not _pool_cols_cache.get("ok"):     # кешуємо лише «так»: міграція могла пройти пізніше
        with db.cursor() as cur:
            cur.execute("""SELECT COUNT(*) AS n FROM information_schema.COLUMNS
                           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'pending_duplicates'
                             AND COLUMN_NAME = 'pool_priority'""")
            _pool_cols_cache["ok"] = bool(cur.fetchone()["n"])
    return _pool_cols_cache["ok"]


def _reject(cur, artist_id, post_id, page_num, md5, reason):
    """
    Запам'ятати відмову з причиною: 'manual' — рішення людини, 'replaced' —
    старий файл, замінений новим. «Перевірити пули» їх не чіпає, на відміну
    від автоматичних відмов архіватора.
    """
    if not _pool_cols_cache.get("reason"):
        cur.execute("""SELECT COUNT(*) AS n FROM information_schema.COLUMNS
                       WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'rejected_duplicates'
                         AND COLUMN_NAME = 'reason'""")
        _pool_cols_cache["reason"] = bool(cur.fetchone()["n"])
    if _pool_cols_cache["reason"]:
        cur.execute("""INSERT INTO rejected_duplicates (artist_id, post_id, page_num, md5_hash, reason)
                       VALUES (%s,%s,%s,%s,%s)
                       ON DUPLICATE KEY UPDATE reason=VALUES(reason)""",
                    (artist_id, post_id, page_num, md5, reason))
    else:
        cur.execute("""INSERT IGNORE INTO rejected_duplicates (artist_id, post_id, page_num, md5_hash)
                       VALUES (%s,%s,%s,%s)""", (artist_id, post_id, page_num, md5))


def _matched_file(db, row: dict) -> dict | None:
    """
    Уже збережений файл пари: id, де лежить і чи в pool.
    Автор — той, у чиїй теці файл (matched_folder_name при збігу з іншим
    автором), пост і ім'я — бо в багатосторінкового поста кілька файлів.
    """
    folder = row.get("matched_folder_name") or row.get("folder_name")
    if not row.get("matched_post_id") or not row.get("matched_filename"):
        return None
    return q(db, """SELECT f.id, f.rel_path, f.md5_hash, f.name,
                           fp.artist_id, fp.post_id, fp.page_num, fp.subfolder, fp.pool_order,
                           a.folder_name
                    FROM furry_posts fp
                    JOIN files f   ON f.id = fp.file_id
                    JOIN artists a ON a.id = fp.artist_id
                    WHERE fp.post_id = %s AND f.name = %s
                    ORDER BY (a.folder_name = %s) DESC, f.id
                    LIMIT 1""",
             (row["matched_post_id"], row["matched_filename"], folder))


def _ps():
    """Модуль пулів застосунку furry (менеджер підключає його під цим іменем)."""
    import sys
    return sys.modules.get("archive_app_furry.poolstore")


def _matched_pools(db, file_id) -> list:
    """Назви пулів збереженого файлу: «рідний» першим, далі додаткові."""
    ps = _ps()
    if not ps:
        r = q(db, "SELECT subfolder FROM furry_posts WHERE file_id=%s", (file_id,))
        return [r["subfolder"]] if r and r["subfolder"] else []
    with db.cursor() as cur:
        return [p["name"] for p in ps.file_pools(cur, file_id)]


def _link_into_pool(db, row) -> dict | None:
    """
    Новий файл відкинули як дублікат, але на сайті він — сторінка пулу,
    в якому збереженого ще немає. Одна робота — кілька пулів: додаємо
    збережений файл і в цей пул (без копії), з номером сторінки нового.
    Та сама серія під іншою назвою (інший сайт) — це той самий пул, нічого
    не додається. Повертає привʼязку (для відміни) або None.
    """
    pool, ps = row.get("pool_folder"), _ps()
    if not pool or not ps:
        return None
    m = _matched_file(db, row)
    if not m:
        return None
    try:
        with db.cursor() as cur:
            pid = ps.ensure_pool(cur, m["artist_id"], pool, row.get("source"))
            if not pid or pid in ps.file_pool_ids(cur, m["id"]):
                return None
            ps.add_item(cur, pid, m["id"], row.get("pool_order"))
    except Exception as e:
        print(f"  ⚠ Не вдалося додати «{m['name']}» у пул «{pool}»: {e}")
        return None
    return {"file_id": m["id"], "pool_id": pid, "pool_name": pool, "file_name": m["name"]}


def _attach_matched_pool(db, rows):
    """Дописує в рядки черги, чи збережений файл лежить у pool."""
    for r in rows:
        m = None
        try:
            m = _matched_file(db, r)
        except Exception:
            pass
        r["matched_file_id"]    = m["id"] if m else None
        r["matched_subfolder"]  = m["subfolder"] if m else None
        r["matched_pool_order"] = m["pool_order"] if m else None
        r["matched_pools"]      = _matched_pools(db, m["id"]) if m else []
        r.setdefault("pool_folder", None)
        r.setdefault("pool_order", None)
        r.setdefault("pool_priority", 0)


def _rel(folder: str, sub: str | None, name: str) -> str:
    parts = [] if folder == "_root" else [folder]
    if sub:
        parts.append(sub)
    parts.append(name)
    return "/".join(p for p in parts if p)


class PendingError(Exception):
    """Зрозуміла причина відмови (показується користувачу)."""


def _pending_row(db, pending_id):
    return q(db, """SELECT p.*, a.folder_name FROM pending_duplicates p
                    JOIN artists a ON a.id = p.artist_id WHERE p.id=%s""", (pending_id,))


def _store_new(db, row, *, artist_id, folder, sub, order):
    """Покласти новий файл з черги в сховище й записати в архів."""
    lp = row["local_path"]
    rel = _rel(folder, sub, row["filename"])
    st = get_storage()
    if not st.upload(lp, rel):
        raise PendingError("Не вдалося покласти файл у сховище")
    with db.cursor() as cur:
        fid = _core.services.get("furry.library").create_file_with_post(cur,
            artist_id=artist_id, post_id=row["post_id"], source=row["source"],
            post_url=row["post_url"], md5_hash=row["md5_hash"], phash=row["phash"],
            filename=row["filename"], file_ext=row["file_ext"], page_num=row["page_num"],
            subfolder=sub or None, pool_order=order if sub else None,
            folder_name=folder, size_bytes=os.path.getsize(lp))
        if not fid:
            raise PendingError("Цей пост уже є в архіві в того автора")
        if artist_id != row["artist_id"]:
            # файл записано за іншим автором (успадкував його pool) — свому
            # автору кажемо «вже є», інакше архіватор качав би пост знову
            _reject(cur, row["artist_id"], row["post_id"], row["page_num"], row["md5_hash"], "replaced")
        cur.execute("DELETE FROM pending_duplicates WHERE id=%s", (row["id"],))
    return rel, fid


def _snap(row) -> dict:
    """Рядок черги як був — щоб відміна з журналу повернула його."""
    return {k: v for k, v in dict(row).items() if k not in ("folder_name",)}


def approve_pending(pending_id) -> dict:
    """«Зберегти новий»: кладемо в теку автора — у його pool, якщо він там є."""
    db = get_db()
    try:
        row = _pending_row(db, pending_id)
        if not row:
            raise PendingError("not found")
        if not row["local_path"] or not os.path.exists(row["local_path"]):
            raise PendingError("Файл не знайдено на диску")
        sub = row.get("pool_folder")
        rel = _rel(row["folder_name"], sub, row["filename"])
        if get_storage().exists(rel):
            raise PendingError(f"У «{rel.rsplit('/', 1)[0] or 'корені'}» уже є файл «{row['filename']}»")
        snap = _snap(row)
        _rel_new, fid = _store_new(db, row, artist_id=row["artist_id"], folder=row["folder_name"],
                                   sub=sub, order=row.get("pool_order"))
        db.commit()
        try: os.remove(row["local_path"])
        except Exception: pass
        return {"ok": True, "filename": row["filename"], "pool": sub,
                "_undo": {"pending": snap, "file_id": fid, "inherited": False}}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def replace_pending(pending_id, user_id=None) -> dict:
    """
    «Замінити старий»: новий файл займає місце старого.

    Якщо старий був сторінкою pool — новий іде в ту саму серію, на ту саму
    сторінку (той самий pool_order), до того самого автора. Інакше — як
    «Зберегти новий»: у теку автора, у свій pool, якщо він є.

    Старий не зникає назавжди — іде в кошик. Порядок: спершу старий у кошик,
    потім новий у сховище (якщо ім'я однакове, новий інакше затер би старий).
    """
    db = get_db()
    try:
        row = _pending_row(db, pending_id)
        if not row:
            raise PendingError("not found")
        lp = row["local_path"]
        if not lp or not os.path.exists(lp):
            raise PendingError("Новий файл не знайдено на диску")

        old = _matched_file(db, row)
        if old and old["subfolder"]:
            artist_id, folder = old["artist_id"], old["folder_name"]
            sub, order = old["subfolder"], old["pool_order"]
        else:
            artist_id, folder = row["artist_id"], row["folder_name"]
            sub, order = row.get("pool_folder"), row.get("pool_order")

        rel = _rel(folder, sub, row["filename"])
        if get_storage().exists(rel) and not (old and old["rel_path"] == rel):
            raise PendingError(f"У «{rel.rsplit('/', 1)[0] or 'корені'}» уже є інший файл «{row['filename']}»")
        with db.cursor() as cur:
            cur.execute("""SELECT 1 FROM furry_posts WHERE artist_id=%s AND post_id=%s AND page_num<=>%s""",
                        (artist_id, row["post_id"], row["page_num"]))
            if cur.fetchone():
                raise PendingError("Цей пост уже є в архіві в того автора")

        snap = _snap(row)
        old_trash_id = None
        ps = _ps()
        old_extra = []          # додаткові пули старого — перейдуть до нового
        if old and ps:
            with db.cursor() as cur:
                old_extra = [p for p in ps.file_pools(cur, old["id"]) if p["linked"]]
        if old:
            # старий md5 — у відхилені, щоб скачування не підхопило його знову
            with db.cursor() as cur:
                _reject(cur, old["artist_id"], old["post_id"], old["page_num"], old["md5_hash"], "replaced")
            db.commit()
            old_trash_id = trash_file(old["id"], user_id)  # 1. старий — у кошик

        try:
            _rel_new, fid = _store_new(db, row, artist_id=artist_id, folder=folder, sub=sub, order=order)  # 2. новий
            if ps and fid:
                with db.cursor() as cur:
                    for p in old_extra:
                        ps.add_item(cur, p["pool_id"], fid, p["order"])
                    # новий був сторінкою свого пулу, а в пул старого став «рідним» — свій теж лишаємо
                    if row.get("pool_folder") and row.get("pool_folder") != sub:
                        ps.link(cur, fid, artist_id, row["pool_folder"], row.get("pool_order"), row.get("source"))
            db.commit()
        except PendingError as e:
            db.rollback()
            raise PendingError(f"{e}. Старий файл — у кошику, його можна відновити.")
        try: os.remove(lp)
        except Exception: pass
        return {"ok": True, "filename": row["filename"],
                "replaced": old["name"] if old else None,
                "pool": sub, "pool_order": order if sub else None,
                "_undo": {"pending": snap, "file_id": fid, "inherited": artist_id != row["artist_id"],
                          "old_trash_id": old_trash_id,
                          "old_reject": ({"artist_id": old["artist_id"], "post_id": old["post_id"],
                                          "page_num": old["page_num"]} if old else None)}}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def apply_pool_priority(log=print) -> tuple:
    """
    Автозаміна: пари, де новий файл — сторінка pool, а збережений — та сама
    робота поза pool. Архіватор ставить їм pool_priority, а тут (після його
    завершення, у веб-процесі з усіма застосунками) вони замінюються тим
    самим шляхом, що й кнопка «Замінити старий» — з кошиком і знімком.
    """
    db = get_db()
    try:
        if not _has_pool_cols(db):
            return 0, 0
        ids = [r["id"] for r in qq(db, "SELECT id FROM pending_duplicates WHERE pool_priority=1 ORDER BY id")]
    finally:
        db.close()
    done, failed = 0, 0
    for pid in ids:
        try:
            r = replace_pending(pid)
            r.pop("_undo", None)
            done += 1
            log(f"  ⇄ pool: {r['replaced'] or '—'} → {r['filename']} («{r['pool']}» #{r['pool_order']})")
        except Exception as e:
            failed += 1
            # не вийшло — лишаємо на ручний розбір, без позначки автозаміни
            try:
                db2 = get_db()
                with db2.cursor() as cur:
                    cur.execute("UPDATE pending_duplicates SET pool_priority=0 WHERE id=%s", (pid,))
                db2.commit(); db2.close()
            except Exception:
                pass
            log(f"  ⚠ pool-заміна #{pid} не вдалась ({e}) — лишилась у «На перевірку»")
    return done, failed



@bp.route("/api/pending")
@moderator_required
def api_pending():
    """
    Черга на перевірку, сторінками.

    Раніше віддавалось усе одним шматком. Поки в черзі були десятки
    записів, це ніхто не помічав; на одинадцяти тисячах браузер
    отримував їх усі, малював стільки ж карток і по дві прев'юшки
    на кожну — і вмирав. Тепер limit/offset, а фронт добирає далі.

    Без параметрів поводиться як раніше (плаский список) — щоб нічого
    стороннього, що ходить на цей ендпоінт, не зламалось.
    """
    try:
        limit  = flask_request.args.get("limit", type=int)
        offset = flask_request.args.get("offset", type=int) or 0
    except Exception:
        limit, offset = None, 0
    if limit is not None:
        limit = max(1, min(limit, 200))

    try:
        db = get_db()
        sql = """
            SELECT p.id, p.post_id, p.source, p.post_url, p.filename, p.file_ext,
                   p.page_num, p.local_path, p.nc_target_path,
                   p.matched_post_id, p.matched_filename, p.matched_folder_name,
                   p.hamming_distance, {pool_cols}
                   p.created_at, a.artist_name, a.id AS artist_id, a.folder_name
            FROM pending_duplicates p
            JOIN artists a ON a.id = p.artist_id
            ORDER BY p.hamming_distance ASC, p.created_at DESC
        """.format(pool_cols=_POOL_COLS if _has_pool_cols(db) else "")
        if limit is None:
            rows  = qq(db, sql)
            total = len(rows)
        else:
            rows = qq(db, sql + " LIMIT %s OFFSET %s", (limit, offset))
            with db.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM pending_duplicates")
                total = int(cur.fetchone()["n"])
        _attach_matched_pool(db, rows)
        db.close()
        for r in rows:
            if r["created_at"]:
                r["created_at"] = r["created_at"].isoformat()
        if limit is None:
            return jsonify(rows)
        return jsonify({"items": rows, "total": total,
                        "offset": offset, "limit": limit})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


def _get_image_info(filepath: str) -> dict:
    """Повертає розмір файлу в байтах."""
    return {"file_size": os.path.getsize(filepath)}


@bp.route("/api/pending/<int:pending_id>/preview/<which>")
@moderator_required
def api_pending_preview(pending_id, which):
    """
    Що показати в картці «На перевірку»: посилання на файл і мініатюру,
    тип і вагу.

    Раніше тут віддавався ВЕСЬ файл у base64 всередині JSON, і браузер
    вставляв його в сторінку. На сторінці 20 пар — 40 файлів одразу; для
    відео по десятки мегабайт це сотні мегабайт тексту в памʼяті й 40
    роликів, що декодуються разом: комп'ютер починав гальмувати. Тепер —
    звичайні посилання: картинка вантажиться, коли видна, відео — лише
    після натискання ▶, з перемоткою.
    """
    if which not in ("new", "matched"):
        return jsonify({"error": "bad which"}), 400
    try:
        db  = get_db()
        row = q(db, """
            SELECT p.*, a.folder_name FROM pending_duplicates p
            JOIN artists a ON a.id = p.artist_id WHERE p.id=%s
        """, (pending_id,))
        if not row:
            return jsonify({"error": "not found"}), 404

        if which == "new":
            lp = row["local_path"]
            # Файл зник з pending_review — пробуємо відновити (як і раніше)
            if not lp or not os.path.exists(lp):
                lp = _recover_pending_file(row)
                if not lp:
                    return jsonify({"error": "Файл не знайдено і не вдалося відновити"}), 404
                with db.cursor() as cur:
                    cur.execute("UPDATE pending_duplicates SET local_path=%s WHERE id=%s", (lp, pending_id))
                db.commit()
            name = row["filename"] or os.path.basename(lp)
            return jsonify({
                "mime":      mimetypes.guess_type(name)[0] or "application/octet-stream",
                "filename":  name,
                "file_size": os.path.getsize(lp),
                "url":       f"/api/pending/{pending_id}/file/new",
                "thumb_url": f"/api/pending/{pending_id}/thumb/new",
            })

        mfn = row["matched_filename"]
        if not mfn:
            return jsonify({"error": "matched_filename відсутній"}), 404
        # Уже збережений файл — у архіві, тож віддається тим самим шляхом,
        # що й у каталозі (з перемоткою для відео, кешем мініатюр).
        matched_folder = row.get("matched_folder_name") or row["folder_name"]
        rel, size = None, None
        mrow = _matched_file(db, row)
        if mrow:
            rel = mrow["rel_path"]
            fr = q(db, "SELECT size_bytes FROM files WHERE id=%s", (mrow["id"],))
            size = fr["size_bytes"] if fr else None
        if not rel:
            rel = mfn if matched_folder == "_root" else f"{matched_folder}/{mfn}"
            if get_storage_mode() == "local" and not os.path.isfile(local_path(rel)):
                frow = q(db, "SELECT rel_path, size_bytes FROM files WHERE name=%s ORDER BY id LIMIT 1", (mfn,))
                if frow:
                    rel, size = frow["rel_path"], frow["size_bytes"]
        if get_storage_mode() == "local":
            lp2 = local_path(rel)
            if not os.path.isfile(lp2):
                return jsonify({"error": "Файл не знайдено на диску"}), 404
            size = os.path.getsize(lp2)
        from urllib.parse import quote
        return jsonify({
            "mime":      mimetypes.guess_type(mfn)[0] or "application/octet-stream",
            "filename":  mfn,
            "folder":    matched_folder,
            "file_size": size,
            "url":       f"/api/catalog/file?path={quote(rel)}",
            "thumb_url": f"/api/catalog/thumb?path={quote(rel)}",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _pending_new_path(pending_id):
    """Шлях до нового файлу з черги (він ще не в архіві — лежить у pending_review)."""
    db = get_db()
    row = q(db, "SELECT local_path FROM pending_duplicates WHERE id=%s", (pending_id,))
    lp = (row or {}).get("local_path")
    # шлях береться лише з бази (його пише архіватор), а не з запиту
    if not lp or not os.path.isfile(lp):
        return None
    return lp


@bp.route("/api/pending/<int:pending_id>/file/new")
@moderator_required
def api_pending_file_new(pending_id):
    """Сам новий файл — з підтримкою Range, щоб відео перемотувалось."""
    from flask import send_file
    lp = _pending_new_path(pending_id)
    if not lp:
        return jsonify({"error": "Файл не знайдено"}), 404
    from core.db import release_db
    release_db()
    mime = mimetypes.guess_type(lp)[0] or "application/octet-stream"
    return send_file(lp, mimetype=mime, conditional=True)


@bp.route("/api/pending/<int:pending_id>/thumb/new")
@moderator_required
def api_pending_thumb_new(pending_id):
    """Мініатюра нового файлу (кадр для відео) — тими ж засобами, що й у каталозі."""
    from flask import send_file
    from blueprints.catalog import (_THUMB_IMAGE_EXT, _THUMB_VIDEO_EXT, _make_image_thumb,
                                    _make_video_thumb, _thumb_cache_path)
    lp = _pending_new_path(pending_id)
    if not lp:
        return jsonify({"error": "Файл не знайдено"}), 404
    from core.db import release_db
    release_db()
    ext = lp.rsplit(".", 1)[-1].lower() if "." in lp else ""
    if ext not in _THUMB_IMAGE_EXT and ext not in _THUMB_VIDEO_EXT:
        return jsonify({"error": "no preview"}), 404
    try:
        tag = str(int(os.path.getmtime(lp)))
    except OSError:
        tag = ""
    out = _thumb_cache_path("pending:" + lp, tag)
    if not os.path.exists(out):
        ok = _make_image_thumb(lp, out) if ext in _THUMB_IMAGE_EXT else _make_video_thumb(lp, out)
        if not ok:
            return jsonify({"error": "thumb failed"}), 404
    return send_file(out, mimetype="image/jpeg", conditional=True, max_age=86400)


@bp.route("/api/pending/<int:pending_id>/replace", methods=["POST"])
@moderator_required
def api_pending_replace(pending_id):
    """«Замінити старий» — див. replace_pending: з успадкуванням pool і сторінки."""
    try:
        res = replace_pending(pending_id, current_user_id())
        undo = res.pop("_undo", None)
        if undo:
            modlog.record("pending.replace", f"«{res.get('replaced') or '—'}» → «{res['filename']}»", undo)
        return jsonify(res)
    except PendingError as e:
        return jsonify({"error": str(e)}), 404 if str(e) == "not found" else 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/pending/<int:pending_id>/approve", methods=["POST"])
@moderator_required
def api_pending_approve(pending_id):
    """«Зберегти новий»: не дублікат — у теку автора, у свій pool, якщо є."""
    try:
        res = approve_pending(pending_id)
        undo = res.pop("_undo", None)
        if undo:
            modlog.record("pending.approve", f"«{res['filename']}»", undo)
        return jsonify(res)
    except PendingError as e:
        return jsonify({"error": str(e)}), 404 if str(e) == "not found" else 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/pending/<int:pending_id>/reject", methods=["POST"])
@moderator_required
def api_pending_reject(pending_id):
    """Відхиляє файл — зберігає в rejected_duplicates щоб не кидало знову."""
    try:
        db  = get_db()
        row = q(db, """
            SELECT p.* FROM pending_duplicates p
            JOIN artists a ON a.id = p.artist_id
            WHERE p.id=%s
        """, (pending_id,))
        if not row:
            db.close()
            return jsonify({"error": "not found"}), 404
        lp = row["local_path"]
        kept = None
        if current_role() == "moderator" and lp and os.path.exists(lp):
            # Модератор: файл не стираємо одразу, а відкладаємо — щоб адмін міг
            # відмінити рішення з журналу. Відкладене прибирається через 14 днів.
            _purge_undo_dir()
            os.makedirs(UNDO_DIR, exist_ok=True)
            kept = os.path.join(UNDO_DIR, f"{pending_id}__{os.path.basename(lp)}")
            try:
                shutil.move(lp, kept)
            except Exception:
                kept = None
        if not kept:
            try:
                if lp and os.path.exists(lp): os.remove(lp)
            except Exception: pass
        snap = _snap(row)
        link = _link_into_pool(db, row)       # сторінка іншого пулу — збережений файл іде й туди
        with db.cursor() as cur:
            _reject(cur, row["artist_id"], row["post_id"], row["page_num"], row["md5_hash"], "manual")
            cur.execute("DELETE FROM pending_duplicates WHERE id=%s", (pending_id,))
        db.commit()
        db.close()
        if kept:
            summary = f"«{row['filename']}» ≈ «{row['matched_filename']}»"
            if link:
                summary += f" (+ пул «{link['pool_name']}»)"
            modlog.record("pending.reject", summary, {"pending": snap, "kept": kept, "link": link})
        return jsonify({"ok": True, "linked_pool": link["pool_name"] if link else None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/pending/reject-all", methods=["POST"])
@admin_required
def api_pending_reject_all():
    try:
        db   = get_db()
        rows = qq(db, """
            SELECT p.*, a.folder_name
            FROM pending_duplicates p
            JOIN artists a ON a.id = p.artist_id
        """)
        linked = sum(1 for r in rows if _link_into_pool(db, r))
        for r in rows:
            try:
                if r["local_path"] and os.path.exists(r["local_path"]): os.remove(r["local_path"])
            except: pass
        with db.cursor() as cur:
            for r in rows:
                _reject(cur, r["artist_id"], r["post_id"], r["page_num"], r["md5_hash"], "manual")
                cur.execute("DELETE FROM pending_duplicates WHERE id=%s", (r["id"],))
        db.commit()
        db.close()
        return jsonify({"ok": True, "deleted": len(rows), "linked": linked})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/pending/distances")
@moderator_required
def api_pending_distances():
    """
    Розподіл черги за відстанню pHash. Потрібен, щоб перед масовим
    відхиленням було видно, що саме в черзі: 11 тисяч точних копій —
    це одне, 11 тисяч файлів на відстані 7 — зовсім інше.
    """
    try:
        db = get_db()
        rows = qq(db, """SELECT hamming_distance AS d, COUNT(*) AS n
                         FROM pending_duplicates
                         GROUP BY hamming_distance ORDER BY hamming_distance""")
        db.close()
        return jsonify([{"distance": int(r["d"] or 0), "count": int(r["n"])} for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/pending/reject-below", methods=["POST"])
@admin_required
def api_pending_reject_below():
    """
    Відхиляє з черги все, що ближче або дорівнює заданій відстані,
    лишаючи спірне на ручний розбір. На відміну від «Відхилити всі»
    це не рубає чергу наосліп.

    Архів при цьому не чіпається: ці файли в нього ще не потрапили,
    вони лежать у папці pending_review. Відхилення стирає саме їх
    і запам'ятовує пост, щоб його не качали знову.
    """
    body = flask_request.get_json(silent=True) or {}
    try:
        max_distance = int(body.get("max_distance", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "max_distance має бути числом"}), 400
    dry_run = bool(body.get("dry_run", False))

    try:
        db   = get_db()
        # Сторінки pool масово не відхиляємо: відхилення прибрало б сторінку
        # з серії. Їх видно в черзі з позначкою, вирішувати — вручну.
        pool_skip = " AND pool_folder IS NULL" if _has_pool_cols(db) else ""
        rows = qq(db, f"""SELECT id, local_path, artist_id, post_id, page_num, md5_hash
                         FROM pending_duplicates
                         WHERE hamming_distance <= %s{pool_skip}""", (max_distance,))
        skipped = 0
        if pool_skip:
            with db.cursor() as cur:
                cur.execute("""SELECT COUNT(*) AS n FROM pending_duplicates
                               WHERE hamming_distance <= %s AND pool_folder IS NOT NULL""", (max_distance,))
                skipped = int(cur.fetchone()["n"])
        if dry_run:
            db.close()
            return jsonify({"ok": True, "dry_run": True, "count": len(rows), "skipped_pool": skipped})

        for r in rows:
            try:
                if r["local_path"] and os.path.exists(r["local_path"]):
                    os.remove(r["local_path"])
            except Exception:
                pass
        with db.cursor() as cur:
            for r in rows:
                _reject(cur, r["artist_id"], r["post_id"], r["page_num"], r["md5_hash"], "manual")
                cur.execute("DELETE FROM pending_duplicates WHERE id=%s", (r["id"],))
        db.commit(); db.close()
        return jsonify({"ok": True, "deleted": len(rows), "skipped_pool": skipped})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/duplicate-scan/clear-all", methods=["POST"])
@admin_required
def api_duplicate_scan_clear_all():
    """Очищає ВЕСЬ поточний список пар-кандидатів одразу, без вибору по
    кожній окремо. На відміну від 'Не дублікат' (яке запам'ятовується
    назавжди) — це просто прибирає список зараз; якщо ті самі пари
    знову підтвердяться повторним сканом, вони можуть з'явитись знову."""
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("DELETE FROM duplicate_scan_results")
            count = cur.rowcount
        db.commit(); db.close()
        return jsonify({"ok": True, "count": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/duplicate-scan")
def api_duplicate_scan_list_all():
    """Список УСІХ пар-кандидатів на дублікати по всій бібліотеці — для
    вкладки 'На перевірку', щоб не заходити на кожного автора окремо."""
    try:
        db   = get_db()
        rows = qq(db, """
            SELECT dsr.id, dsr.hamming_distance, dsr.created_at, a.artist_name,
                   dsr.download_id_a, f_a.name AS filename_a, f_a.ext AS ext_a, da.subfolder AS subfolder_a,
                   da.source AS source_a, da.post_id AS post_id_a, da.post_url AS post_url_a,
                   f_a.width AS width_a, f_a.height AS height_a, f_a.size_bytes AS size_a,
                   dsr.download_id_b, f_b.name AS filename_b, f_b.ext AS ext_b, db2.subfolder AS subfolder_b,
                   db2.source AS source_b, db2.post_id AS post_id_b, db2.post_url AS post_url_b,
                   f_b.width AS width_b, f_b.height AS height_b, f_b.size_bytes AS size_b
            FROM duplicate_scan_results dsr
            LEFT JOIN artists a ON a.id  = dsr.artist_id
            JOIN files f_a        ON f_a.id  = dsr.download_id_a
            LEFT JOIN furry_posts da  ON da.file_id  = f_a.id
            JOIN files f_b        ON f_b.id  = dsr.download_id_b
            LEFT JOIN furry_posts db2 ON db2.file_id = f_b.id
            ORDER BY dsr.created_at DESC
        """)
        db.close()
        for r in rows:
            if r["created_at"]: r["created_at"] = r["created_at"].isoformat()
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/artists/<int:artist_id>/duplicate-scan")
def api_artists_duplicate_scan_list(artist_id):
    """Список пар-кандидатів на дублікати, знайдених ретроактивним
    скануванням у цього автора — нічого тут не видалено, тільки показано."""
    try:
        db   = get_db()
        rows = qq(db, """
            SELECT dsr.id, dsr.hamming_distance, dsr.created_at,
                   dsr.download_id_a, f_a.name AS filename_a, f_a.ext AS ext_a, da.subfolder AS subfolder_a,
                   da.source AS source_a, da.post_id AS post_id_a, da.post_url AS post_url_a,
                   f_a.width AS width_a, f_a.height AS height_a, f_a.size_bytes AS size_a,
                   dsr.download_id_b, f_b.name AS filename_b, f_b.ext AS ext_b, db2.subfolder AS subfolder_b,
                   db2.source AS source_b, db2.post_id AS post_id_b, db2.post_url AS post_url_b,
                   f_b.width AS width_b, f_b.height AS height_b, f_b.size_bytes AS size_b
            FROM duplicate_scan_results dsr
            JOIN files f_a        ON f_a.id  = dsr.download_id_a
            LEFT JOIN furry_posts da  ON da.file_id  = f_a.id
            JOIN files f_b        ON f_b.id  = dsr.download_id_b
            LEFT JOIN furry_posts db2 ON db2.file_id = f_b.id
            WHERE dsr.artist_id=%s
            ORDER BY dsr.created_at DESC
        """, (artist_id,))
        db.close()
        for r in rows:
            if r["created_at"]: r["created_at"] = r["created_at"].isoformat()
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/duplicate-scan/<int:result_id>/resolve", methods=["POST"])
@moderator_required
def api_duplicate_scan_resolve(result_id):
    """Вирішує знайдену пару: видалити один з двох файлів (реально, з
    диску й БД — той самий механізм, що й звичайне видалення файлу),
    або просто прибрати пару зі списку, якщо це не дублікат насправді."""
    body   = flask_request.get_json(silent=True) or {}
    action = body.get("action")  # 'delete_a' | 'delete_b' | 'dismiss'
    try:
        db  = get_db()
        row = q(db, "SELECT * FROM duplicate_scan_results WHERE id=%s", (result_id,))
        if not row:
            db.close()
            return jsonify({"error": "Пару не знайдено"}), 404
        undo = {"result": {k: row[k] for k in ("artist_id", "download_id_a", "download_id_b", "hamming_distance")}}

        if action in ("delete_a", "delete_b"):
            target_id = row["download_id_a"] if action == "delete_a" else row["download_id_b"]
            # У кошик: і файл, і запис. Раніше тут стирався лише файл, а запис
            # лишався — у галереї висіла картка без файлу.
            try:
                f = get_file(target_id)
                undo["trash_id"] = trash_file(target_id, current_user_id())
                undo["name"] = f["name"] if f else f"#{target_id}"
            except TrashError as e:
                db.close()
                return jsonify({"error": str(e)}), 404

        elif action == "dismiss":
            # "Це не дублікат" — запам'ятовуємо пару назавжди, щоб пошук
            # більше її не пропонував. (Ця гілка була загублена — пару
            # пропонувало знову при кожному пошуку.)
            id_a, id_b = sorted((row["download_id_a"], row["download_id_b"]))
            with db.cursor() as cur:
                cur.execute("""
                    INSERT IGNORE INTO duplicate_ignore_pairs (download_id_a, download_id_b)
                    VALUES (%s,%s)
                """, (id_a, id_b))
            undo["dismiss"] = [id_a, id_b]

        else:
            db.close()
            return jsonify({"error": f"Невідома дія: {action}"}), 400

        with db.cursor() as cur:
            cur.execute("DELETE FROM duplicate_scan_results WHERE id=%s", (result_id,))
        db.commit(); db.close()
        modlog.record("dupscan.resolve",
                      f"у кошик «{undo['name']}»" if "trash_id" in undo else "«не дублікат»", undo)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Відміна рішень модератора (журнал ядра) ──────────────────────────

import shutil
import time as _time

UNDO_DIR = os.path.join(PENDING_DIR, "_undo")
UNDO_KEEP_DAYS = 14


def _purge_undo_dir():
    """Відкладені «Дублікат» модераторів старші за 14 днів — стерти."""
    if not os.path.isdir(UNDO_DIR):
        return
    limit = _time.time() - UNDO_KEEP_DAYS * 86400
    for name in os.listdir(UNDO_DIR):
        p = os.path.join(UNDO_DIR, name)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < limit:
                os.remove(p)
        except OSError:
            pass


def _reinsert_pending(snap):
    db = get_db()
    cols = [k for k in snap if k not in ("folder_name", "created_at")]
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM pending_duplicates WHERE id=%s", (snap.get("id"),))
        if cur.fetchone():
            cols.remove("id")
        cur.execute("SELECT 1 FROM pending_duplicates WHERE artist_id=%s AND post_id=%s AND page_num<=>%s",
                    (snap["artist_id"], snap["post_id"], snap.get("page_num")))
        if cur.fetchone():
            raise modlog.UndoError("Цей пост знову в черзі «На перевірку»")
        cur.execute(f"INSERT INTO pending_duplicates ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                    [snap[c] for c in cols])
    db.commit()


def _file_back_to_queue(fid, local):
    """Збережений файл → назад у теку черги; запис з архіву прибрати повністю."""
    from core.files_api import purge_trash
    f = get_file(fid)
    if not f:
        raise modlog.UndoError("Цього файлу в архіві вже немає — можливо, його вже видалили")
    os.makedirs(os.path.dirname(local) or PENDING_DIR, exist_ok=True)
    if os.path.exists(local):
        base, ext = os.path.splitext(local)
        local = f"{base}_{fid}{ext}"
    if not get_storage().download(f["rel_path"], local):
        raise modlog.UndoError("Не вдалося забрати файл зі сховища")
    purge_trash(trash_file(fid))
    return local


def _drop_reject(artist_id, post_id, page_num, reason=None):
    db = get_db()
    with db.cursor() as cur:
        sql = "DELETE FROM rejected_duplicates WHERE artist_id=%s AND post_id=%s AND page_num<=>%s"
        args = [artist_id, post_id, page_num]
        cur.execute(sql, args)
    db.commit()


def _undo_approve(p):
    snap = dict(p["pending"])
    snap["local_path"] = _file_back_to_queue(p["file_id"], snap["local_path"])
    if p.get("inherited"):
        _drop_reject(snap["artist_id"], snap["post_id"], snap.get("page_num"))
    _reinsert_pending(snap)
    return "файл знову в черзі «На перевірку»"


def _undo_replace(p):
    note = _undo_approve(p)
    if p.get("old_reject"):
        o = p["old_reject"]
        _drop_reject(o["artist_id"], o["post_id"], o.get("page_num"))
    if p.get("old_trash_id"):
        from core.files_api import restore_trash
        try:
            restore_trash(p["old_trash_id"])
        except TrashError as e:
            return f"{note}; старий файл не відновлено: {e}"
    return f"{note}; старий файл повернуто"


def _undo_reject(p):
    snap = dict(p["pending"])
    kept = p.get("kept")
    if not kept or not os.path.exists(kept):
        raise modlog.UndoError("Відкладений файл уже стерто (минуло понад 14 днів)")
    os.makedirs(os.path.dirname(snap["local_path"]) or PENDING_DIR, exist_ok=True)
    target = snap["local_path"]
    if os.path.exists(target):
        base, ext = os.path.splitext(target)
        target = f"{base}_{snap['id']}{ext}"
    shutil.move(kept, target)
    snap["local_path"] = target
    _drop_reject(snap["artist_id"], snap["post_id"], snap.get("page_num"))
    try:
        _reinsert_pending(snap)
    except Exception:
        shutil.move(target, kept)
        raise
    link = p.get("link")
    if link and _ps():
        db = get_db()
        with db.cursor() as cur:
            pid = link.get("pool_id")
            if not pid:      # старі записи журналу — за назвою
                r = q(db, "SELECT artist_id FROM furry_posts WHERE file_id=%s", (link["file_id"],))
                pid = _ps().find_pool(cur, r["artist_id"], link["pool_name"]) if r else None
            if pid:
                _ps().unlink(cur, link["file_id"], pid)
        db.commit()
    return "файл знову в черзі «На перевірку»"


def _undo_dupscan(p):
    from core.files_api import restore_trash
    note = ""
    if p.get("trash_id"):
        try:
            restore_trash(p["trash_id"])
            note = "файл відновлено з кошика"
        except TrashError as e:
            raise modlog.UndoError(str(e))
    db = get_db()
    with db.cursor() as cur:
        if p.get("dismiss"):
            cur.execute("DELETE FROM duplicate_ignore_pairs WHERE download_id_a=%s AND download_id_b=%s",
                        tuple(p["dismiss"]))
            note = "пару знову буде запропоновано"
        r = p["result"]
        cur.execute("SELECT COUNT(*) AS n FROM files WHERE id IN (%s,%s)", (r["download_id_a"], r["download_id_b"]))
        if cur.fetchone()["n"] == 2:
            cur.execute("""INSERT IGNORE INTO duplicate_scan_results
                           (artist_id, download_id_a, download_id_b, hamming_distance) VALUES (%s,%s,%s,%s)""",
                        (r["artist_id"], r["download_id_a"], r["download_id_b"], r["hamming_distance"]))
    db.commit()
    return note


def register_undo():
    modlog.register("pending.approve", _undo_approve,  "«Зберегти новий»")
    modlog.register("pending.replace", _undo_replace,  "«Замінити старий»")
    modlog.register("pending.reject",  _undo_reject,   "«Дублікат»")
    modlog.register("dupscan.resolve", _undo_dupscan,  "Вирішив пару дублікатів")
    try:
        _purge_undo_dir()
    except Exception:
        pass


def _recover_pending_file(row: dict, user_id: int | None = None) -> str | None:
    """
    Якщо файл з pending_review зник — відновлює його.
    Спочатку пробує скачати з e621/Inkbunny по прямому URL файлу,
    Повертає шлях до відновленого файлу або None.
    """
    filename  = row.get("filename", "")
    post_url  = row.get("post_url", "")
    source    = row.get("source", "")
    post_id   = row.get("post_id", "")

    pending_dir = PENDING_DIR
    os.makedirs(pending_dir, exist_ok=True)
    local_path = os.path.join(pending_dir, filename)

    import requests as req

    # 1. Пробуємо скачати напряму з e621/Inkbunny API
    try:
        settings = get_user_settings(1)
        if source == "e621":
            numeric_id = post_id.replace("e621_", "")
            api_url    = f"https://e621.net/posts/{numeric_id}.json"
            headers    = {"User-Agent": "FurryArchiver/2.0"}
            auth       = None
            e621_user  = settings.get("e621", {}).get("username", "")
            e621_key   = settings.get("e621", {}).get("api_key", "")
            if e621_user and e621_key:
                auth = (e621_user, e621_key)
            r = req.get(api_url, headers=headers, auth=auth, timeout=15)
            if r.ok:
                post     = r.json().get("post", {})
                file_url = post.get("file", {}).get("url")
                if file_url:
                    r2 = req.get(file_url, headers=headers, auth=auth, timeout=30)
                    if r2.ok:
                        with open(local_path, "wb") as f:
                            f.write(r2.content)
                        print(f"  ✓ Відновлено з e621: {filename}")
                        return local_path

        elif source == "inkbunny":
            ib_user = settings.get("inkbunny", {}).get("username", "")
            ib_pass = settings.get("inkbunny", {}).get("password", "")
            if ib_user and ib_pass:
                # Логінимось
                login = req.post("https://inkbunny.net/api_login.php",
                                 data={"username": ib_user, "password": ib_pass}, timeout=10)
                sid = login.json().get("sid") if login.ok else None
                if sid:
                    numeric_id = post_id.replace("ib_", "").split("-")[0]
                    subs = req.post("https://inkbunny.net/api_submissions.php",
                                    data={"sid": sid, "submission_ids": numeric_id,
                                          "show_description": "no"}, timeout=15)
                    if subs.ok:
                        files = subs.json().get("submissions", [{}])[0].get("files", [])
                        # Знаходимо потрібну сторінку
                        page_num = row.get("page_num", 1)
                        fi = files[page_num - 1] if len(files) >= page_num else (files[0] if files else None)
                        if fi:
                            file_url = fi.get("file_url_full") or fi.get("file_url_screen")
                            if file_url:
                                r2 = req.get(file_url, timeout=30)
                                if r2.ok:
                                    with open(local_path, "wb") as f:
                                        f.write(r2.content)
                                    print(f"  ✓ Відновлено з Inkbunny: {filename}")
                                    return local_path
    except Exception as e:
        print(f"  ⚠ Не вдалося відновити {filename} з API: {e}")

    return None


# ─────────────────────────────────────────────────────────────────────
#  Пошук дублікатів по ВСЬОМУ архіву
#
#  Пошук у межах одного автора порівнює кожен файл із кожним — для
#  сотні файлів це дрібниця. По всьому архіву так не можна: 20 тисяч
#  файлів дали б 200 мільйонів порівнянь.
#
#  Тому:
#   1. Точні копії беремо одним запитом по md5 — миттєво.
#   2. Схожі картинки групуємо по частинах хешу (кожен pHash ділимо на
#      4 шматки по 16 біт). Два майже однакових хеші майже напевно
#      збігаються хоча б в одному шматку, тож порівнюємо тільки всередині
#      таких груп, а не всіх з усіма.
#
#  Запускається як фонова задача ядра: архів великий, а чекати
#  відповіді HTTP хвилинами не годиться.
# ─────────────────────────────────────────────────────────────────────

def _scan_whole_archive(job, params):
    threshold = int(params.get("threshold") or 8)
    db = get_db()
    try:
        job.progress(5, "шукаю точні копії")
        exact = qq(db, """
            SELECT GROUP_CONCAT(id ORDER BY id) AS ids
            FROM files WHERE md5_hash IS NOT NULL
            GROUP BY md5_hash HAVING COUNT(*) > 1
        """)
        pairs = []
        for row in exact:
            ids = [int(x) for x in row["ids"].split(",")]
            first = ids[0]
            for other in ids[1:]:
                pairs.append((first, other, 0))

        if job.cancelled():
            return {"stopped": True}

        job.progress(25, f"точних копій: {len(pairs)}; рахую схожі")
        rows = qq(db, "SELECT file_id, phash FROM file_phash WHERE phash IS NOT NULL")

        buckets = {}
        for r in rows:
            h = int(r["phash"])
            for shift in (0, 16, 32, 48):
                buckets.setdefault((shift, (h >> shift) & 0xFFFF), []).append((r["file_id"], h))

        seen = {(a, b) for a, b, _ in pairs}
        total = len(buckets) or 1
        for n, group in enumerate(buckets.values()):
            if n % 200 == 0:
                if job.cancelled():
                    return {"stopped": True}
                job.progress(25 + int(n / total * 60), f"груп оброблено {n} з {total}")
            if len(group) < 2 or len(group) > 400:
                continue          # завеликі групи — це сміттєвий хеш (напр. однотонні картинки)
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    (id_a, h_a), (id_b, h_b) = group[i], group[j]
                    a, b = (id_a, id_b) if id_a < id_b else (id_b, id_a)
                    if (a, b) in seen:
                        continue
                    dist = bin(h_a ^ h_b).count("1")
                    if dist <= threshold:
                        seen.add((a, b))
                        pairs.append((a, b, dist))

        job.progress(90, f"знайдено пар: {len(pairs)}; записую")
        added = 0
        with db.cursor() as cur:
            for a, b, dist in pairs:
                cur.execute("""SELECT 1 FROM duplicate_ignore_pairs
                               WHERE (download_id_a=%s AND download_id_b=%s)
                                  OR (download_id_a=%s AND download_id_b=%s)""", (a, b, b, a))
                if cur.fetchone():
                    continue      # пару вже позначили як "це не дублікат"
                cur.execute("""INSERT IGNORE INTO duplicate_scan_results
                               (artist_id, download_id_a, download_id_b, hamming_distance)
                               VALUES (NULL, %s, %s, %s)""", (a, b, dist))
                added += cur.rowcount
        db.commit()
        return {"пар знайдено": len(pairs), "нових записано": added}
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────
#  Масове вирішення знайдених пар.
#
#  Клікати 11 тисяч пар руками неможливо, тож для однозначних випадків
#  потрібне правило, яке можна застосувати оптом. Правило таке:
#  лишаємо файл кращої якості, гірший — у кошик.
#
#  Порядок переваги, згори вниз:
#    1. більше пікселів (width × height)
#    2. більший розмір у байтах
#    3. менший id — тобто той, що в архіві давніше
#
#  Теги й «обране» з того файлу, що йде в кошик, спершу переносяться на
#  той, що лишається: інакше видалення тихо з'їло б розмітку.
#
#  Нічого не стирається назавжди — тільки кошик, звідки все можна
#  повернути. І за замовчуванням це ПРОБНИЙ запуск: він рахує й показує,
#  але не чіпає.
# ─────────────────────────────────────────────────────────────────────

def _better_of(a: dict, b: dict) -> tuple[dict, dict]:
    """Повертає (той, що лишається, той, що в кошик)."""
    def rank(f):
        px = (f.get("width") or 0) * (f.get("height") or 0)
        return (px, f.get("size_bytes") or 0, -f["id"])
    return (a, b) if rank(a) >= rank(b) else (b, a)


def _merge_meta(db, keep_id: int, drop_id: int):
    """Переносить теги й «обране» з файлу, що видаляється, на той, що лишається."""
    with db.cursor() as cur:
        cur.execute("""INSERT IGNORE INTO download_tags (download_id, tag_id)
                       SELECT %s, tag_id FROM download_tags WHERE download_id=%s""",
                    (keep_id, drop_id))
        cur.execute("""INSERT IGNORE INTO user_favorites (user_id, download_id)
                       SELECT user_id, %s FROM user_favorites WHERE download_id=%s""",
                    (keep_id, drop_id))


def _auto_resolve(job, params):
    max_distance = int(params.get("max_distance") or 0)
    dry_run      = bool(params.get("dry_run", True))
    user_id      = params.get("user_id")

    db = get_db()
    try:
        job.progress(5, "читаю знайдені пари")
        pairs = qq(db, """SELECT id, download_id_a, download_id_b, hamming_distance
                          FROM duplicate_scan_results
                          WHERE hamming_distance <= %s
                          ORDER BY hamming_distance, id""", (max_distance,))
        total = len(pairs) or 1

        gone   = set()     # вже відправлені в кошик у цьому проході
        moved  = 0
        freed  = 0
        skipped = 0
        sample = []

        for n, pair in enumerate(pairs):
            if n % 100 == 0:
                if job.cancelled():
                    return {"зупинено": True, "у кошик": moved}
                job.progress(5 + int(n / total * 90), f"пара {n} з {total}")

            id_a, id_b = pair["download_id_a"], pair["download_id_b"]
            if id_a in gone or id_b in gone:
                # Один з двох уже поїхав у кошик через іншу пару —
                # порівнювати нема з чим, просто прибираємо запис.
                skipped += 1
                if not dry_run:
                    with db.cursor() as cur:
                        cur.execute("DELETE FROM duplicate_scan_results WHERE id=%s", (pair["id"],))
                continue

            rows = qq(db, """SELECT id, name, rel_path, width, height, size_bytes
                             FROM files WHERE id IN (%s, %s)""", (id_a, id_b))
            if len(rows) < 2:
                skipped += 1        # файл уже зник — пара неактуальна
                if not dry_run:
                    with db.cursor() as cur:
                        cur.execute("DELETE FROM duplicate_scan_results WHERE id=%s", (pair["id"],))
                continue

            keep, drop = _better_of(rows[0], rows[1])

            if len(sample) < 25:
                sample.append({
                    "лишається": keep["rel_path"],
                    "у кошик":   drop["rel_path"],
                    "відстань":  int(pair["hamming_distance"] or 0),
                })

            if dry_run:
                moved += 1
                freed += drop.get("size_bytes") or 0
                gone.add(drop["id"])
                continue

            try:
                _merge_meta(db, keep["id"], drop["id"])
                trash_file(drop["id"], user_id)
            except TrashError:
                skipped += 1
                continue
            gone.add(drop["id"])
            moved += 1
            freed += drop.get("size_bytes") or 0
            with db.cursor() as cur:
                cur.execute("DELETE FROM duplicate_scan_results WHERE id=%s", (pair["id"],))
            if moved % 200 == 0:
                db.commit()

        if not dry_run:
            db.commit()

        return {
            "режим":        "пробний (нічого не змінено)" if dry_run else "виконано",
            "пар оброблено": len(pairs),
            "у кошик":      moved,
            "пропущено":    skipped,
            "звільниться МБ" if dry_run else "звільнено МБ": round(freed / 1048576, 1),
            "приклади":     sample,
        }
    finally:
        db.close()


@bp.route("/api/duplicate-scan/auto-resolve", methods=["POST"])
@admin_required
def api_duplicate_scan_auto_resolve():
    """
    Оптом розбирає знайдені пари. Тіло запиту:
      max_distance — до якої відстані чіпати (0 = тільки точні копії)
      dry_run      — true (типово) лише рахує
    """
    body = flask_request.get_json(silent=True) or {}
    job_id = _core.jobs.create("dup-auto-resolve", {
        "max_distance": int(body.get("max_distance") or 0),
        "dry_run":      bool(body.get("dry_run", True)),
        "user_id":      current_user_id(),
    }, user_id=current_user_id())
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/api/duplicate-scan/global", methods=["POST"])
@admin_required
def api_duplicate_scan_global():
    """Ставить у чергу пошук дублікатів по всьому архіву."""
    body = flask_request.get_json(silent=True) or {}
    job_id = _core.jobs.create("scan-all",
                               {"threshold": body.get("threshold", 8)},
                               user_id=current_user_id())
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/api/duplicates/similar/<int:file_id>")
@login_required
def api_similar_to_file(file_id):
    """
    Схожі на цей файл: спершу точні копії за md5, далі близькі за pHash.
    Використовується кнопкою «Знайти схожі» в картці файлу.
    """
    threshold = min(int(flask_request.args.get("threshold", 8)), 20)
    db = get_db()
    try:
        me = q(db, """SELECT f.id, f.md5_hash, p.phash FROM files f
                      LEFT JOIN file_phash p ON p.file_id = f.id WHERE f.id=%s""", (file_id,))
        if not me:
            return jsonify({"error": "Файл не знайдено"}), 404

        out, seen = [], {file_id}
        if me["md5_hash"]:
            for r in qq(db, """SELECT id, name, rel_path FROM files
                               WHERE md5_hash=%s AND id<>%s LIMIT 50""", (me["md5_hash"], file_id)):
                seen.add(r["id"])
                out.append({**r, "distance": 0, "exact": True})

        if me["phash"] is not None:
            mine = int(me["phash"])
            # Порівнюємо в базі: вибирати всі хеші в Python на великому
            # архіві було б помітно повільніше.
            for r in qq(db, """SELECT f.id, f.name, f.rel_path,
                                      BIT_COUNT(p.phash ^ %s) AS distance
                               FROM file_phash p JOIN files f ON f.id = p.file_id
                               WHERE p.phash IS NOT NULL AND f.id <> %s
                               HAVING distance <= %s
                               ORDER BY distance LIMIT 50""", (mine, file_id, threshold)):
                if r["id"] in seen:
                    continue
                out.append({**r, "distance": int(r["distance"]), "exact": False})
        return jsonify({"file_id": file_id, "similar": out})
    finally:
        db.close()
