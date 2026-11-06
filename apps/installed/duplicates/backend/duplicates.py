"""Дублікати та pending-review"""

import hashlib
import base64, mimetypes, os

from flask import Blueprint, jsonify, request as flask_request


from core.auth import admin_required, get_user_nc_config, get_user_settings, current_user_id, login_required
from core.db import get_db, q, qq
from core.imgcache import _IMG_CACHE_DIR
from core.nextcloud import get_file_nc_real_path, get_nc_db, nc_share_url
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
            cur.execute("""INSERT IGNORE INTO rejected_duplicates (artist_id, post_id, page_num, md5_hash)
                           VALUES (%s,%s,%s,%s)""",
                        (row["artist_id"], row["post_id"], row["page_num"], row["md5_hash"]))
        cur.execute("DELETE FROM pending_duplicates WHERE id=%s", (row["id"],))
    return rel


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
        _store_new(db, row, artist_id=row["artist_id"], folder=row["folder_name"],
                   sub=sub, order=row.get("pool_order"))
        db.commit()
        try: os.remove(row["local_path"])
        except Exception: pass
        return {"ok": True, "filename": row["filename"], "pool": sub}
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

        if old:
            # старий md5 — у відхилені, щоб скачування не підхопило його знову
            with db.cursor() as cur:
                cur.execute("""INSERT IGNORE INTO rejected_duplicates
                                   (artist_id, post_id, page_num, md5_hash)
                               VALUES (%s,%s,%s,%s)""",
                            (old["artist_id"], old["post_id"], old["page_num"], old["md5_hash"]))
            db.commit()
            trash_file(old["id"], user_id)                 # 1. старий — у кошик

        try:
            _store_new(db, row, artist_id=artist_id, folder=folder, sub=sub, order=order)  # 2. новий
            db.commit()
        except PendingError as e:
            db.rollback()
            raise PendingError(f"{e}. Старий файл — у кошику, його можна відновити.")
        try: os.remove(lp)
        except Exception: pass
        return {"ok": True, "filename": row["filename"],
                "replaced": old["name"] if old else None,
                "pool": sub, "pool_order": order if sub else None}
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
@admin_required
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
@admin_required
def api_pending_preview(pending_id, which):
    try:
        db  = get_db()
        row = q(db, """
            SELECT p.*, a.folder_name FROM pending_duplicates p
            JOIN artists a ON a.id = p.artist_id WHERE p.id=%s
        """, (pending_id,))
        db.close()
        if not row:
            return jsonify({"error": "not found"}), 404

        if which == "new":
            lp = row["local_path"]

            # Якщо файл відсутній — намагаємось відновити з Nextcloud
            if not lp or not os.path.exists(lp):
                recovered = _recover_pending_file(row)
                if recovered:
                    lp = recovered
                    # Оновлюємо local_path в БД
                    db2 = get_db()
                    with db2.cursor() as cur:
                        cur.execute("UPDATE pending_duplicates SET local_path=%s WHERE id=%s",
                                    (lp, pending_id))
                    db2.commit(); db2.close()
                else:
                    return jsonify({"error": "Файл не знайдено і не вдалося відновити"}), 404

            mime     = mimetypes.guess_type(lp)[0] or "application/octet-stream"
            info     = _get_image_info(lp)
            with open(lp, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            return jsonify({"mime": mime, "data": b64,
                            "filename": row["filename"], **info})

        elif which == "matched":
            mfn = row["matched_filename"]
            if not mfn:
                return jsonify({"error": "matched_filename відсутній"}), 404

            # matched_folder_name зберігається при крос-авторських дублікатах
            matched_folder = row.get("matched_folder_name") or row["folder_name"]
            mime        = mimetypes.guess_type(mfn)[0] or "application/octet-stream"

            # ЛОКАЛЬНЕ СХОВИЩЕ. Раніше цієї гілки не було взагалі: код одразу
            # йшов у Nextcloud і падав з KeyError 'webdav_url', бо в
            # локальному режимі цих налаштувань просто немає. Через це
            # прев'ю вже збереженого файлу не показувалось ніколи.
            if get_storage_mode() == "local":
                rel = mfn if matched_folder == "_root" else f"{matched_folder}/{mfn}"
                db4 = get_db()
                try:
                    mrow = _matched_file(db4, row)    # справжнє місце — з урахуванням pool
                finally:
                    db4.close()
                if mrow:
                    rel = mrow["rel_path"]
                lp2 = local_path(rel)
                if not os.path.isfile(lp2):
                    # Файл міг переїхати в підпапку (pool) — шукаємо в ядрі за іменем.
                    # З'єднання вище вже закрите, тому відкриваємо своє.
                    db3 = get_db()
                    frow = q(db3, """SELECT rel_path FROM files
                                     WHERE name = %s ORDER BY id LIMIT 1""", (mfn,))
                    db3.close()
                    if frow:
                        lp2 = local_path(frow["rel_path"])
                if not os.path.isfile(lp2):
                    return jsonify({"error": "Файл не знайдено на диску"}), 404
                with open(lp2, "rb") as f2:
                    b64m = base64.b64encode(f2.read()).decode()
                return jsonify({"mime": mime, "data": b64m, "filename": mfn,
                                "folder": matched_folder, **_get_image_info(lp2)})

            # Беремо реальний шлях файлу напряму з NC БД — без здогадок про base_folder
            file_info = get_file_nc_real_path(matched_folder, mfn)

            # Розмір файлу з oc_filecache
            info = {}
            try:
                nc_conn = get_nc_db()
                if file_info:
                    full_dir, fname = file_info
                    nc_path_db = f"files/{full_dir}/{fname}"
                    r = q(nc_conn, "SELECT size FROM oc_filecache WHERE path = %s LIMIT 1", (nc_path_db,))
                else:
                    r = q(nc_conn, "SELECT size FROM oc_filecache WHERE name = %s LIMIT 1", (mfn,))
                nc_conn.close()
                if r and r.get("size"):
                    info["file_size"] = int(r["size"])
            except Exception as e:
                info["_nc_err"] = str(e)[:120]

            # Повертаємо share_url якщо є — браузер завантажить напряму
            share_url = nc_share_url(matched_folder, mfn)
            if share_url:
                return jsonify({"mime": mime, "filename": mfn,
                                "folder": matched_folder,
                                "share_url": share_url, **info})

            # Без share token — WebDAV кеш (повільно але працює)
            user_nc     = get_user_nc_config()
            base_folder = user_nc.get("base_folder", "").strip("/")
            nc_path    = f"{base_folder}/{matched_folder}/{mfn}"
            cache_key  = hashlib.md5(nc_path.encode()).hexdigest()
            cache_path = os.path.join(_IMG_CACHE_DIR, f"pending_{cache_key}")
            if not os.path.exists(cache_path):
                try:
                    from webdav3.client import Client
                    client = Client({
                        "webdav_hostname": user_nc["webdav_url"],
                        "webdav_login":    user_nc["username"],
                        "webdav_password": user_nc["password"],
                        "disable_check":   True,
                    })
                    client.download_sync(remote_path=nc_path, local_path=cache_path)
                except Exception as e:
                    return jsonify({"error": f"WebDAV: {e}"}), 500
            cache_info = _get_image_info(cache_path)
            info.update(cache_info)
            with open(cache_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            return jsonify({"mime": mime, "data": b64,
                            "filename": mfn, **info})
        else:
            return jsonify({"error": "bad which"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/pending/<int:pending_id>/replace", methods=["POST"])
@admin_required
def api_pending_replace(pending_id):
    """«Замінити старий» — див. replace_pending: з успадкуванням pool і сторінки."""
    try:
        return jsonify(replace_pending(pending_id, current_user_id()))
    except PendingError as e:
        return jsonify({"error": str(e)}), 404 if str(e) == "not found" else 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/pending/<int:pending_id>/approve", methods=["POST"])
@admin_required
def api_pending_approve(pending_id):
    """«Зберегти новий»: не дублікат — у теку автора, у свій pool, якщо є."""
    try:
        return jsonify(approve_pending(pending_id))
    except PendingError as e:
        return jsonify({"error": str(e)}), 404 if str(e) == "not found" else 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/pending/<int:pending_id>/reject", methods=["POST"])
@admin_required
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
        try:
            lp = row["local_path"]
            if lp and os.path.exists(lp): os.remove(lp)
        except: pass
        with db.cursor() as cur:
            cur.execute("""
                INSERT IGNORE INTO rejected_duplicates
                    (artist_id, post_id, page_num, md5_hash)
                VALUES (%s, %s, %s, %s)
            """, (row["artist_id"], row["post_id"], row["page_num"], row["md5_hash"]))
            cur.execute("DELETE FROM pending_duplicates WHERE id=%s", (pending_id,))
        db.commit()
        db.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/pending/reject-all", methods=["POST"])
@admin_required
def api_pending_reject_all():
    try:
        db   = get_db()
        rows = qq(db, """
            SELECT p.id, p.local_path, p.artist_id, p.post_id, p.page_num, p.md5_hash
            FROM pending_duplicates p
            JOIN artists a ON a.id = p.artist_id
        """)
        for r in rows:
            try:
                if r["local_path"] and os.path.exists(r["local_path"]): os.remove(r["local_path"])
            except: pass
        with db.cursor() as cur:
            for r in rows:
                cur.execute("""
                    INSERT IGNORE INTO rejected_duplicates
                        (artist_id, post_id, page_num, md5_hash)
                    VALUES (%s, %s, %s, %s)
                """, (r["artist_id"], r["post_id"], r["page_num"], r["md5_hash"]))
                cur.execute("DELETE FROM pending_duplicates WHERE id=%s", (r["id"],))
        db.commit()
        db.close()
        return jsonify({"ok": True, "deleted": len(rows)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/pending/distances")
@admin_required
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
                cur.execute("""INSERT IGNORE INTO rejected_duplicates
                                   (artist_id, post_id, page_num, md5_hash)
                               VALUES (%s,%s,%s,%s)""",
                            (r["artist_id"], r["post_id"], r["page_num"], r["md5_hash"]))
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
@admin_required
def api_duplicate_scan_resolve(result_id):
    """Вирішує знайдену пару: видалити один з двох файлів (реально, з
    диску й БД — той самий механізм, що й звичайне видалення файлу),
    або просто прибрати пару зі списку, якщо це не дублікат насправді."""
    body   = flask_request.get_json(silent=True) or {}
    action = body.get("action")  # 'delete_a' | 'delete_b' | 'dismiss'
    try:
        db  = get_db()
        row = q(db, "SELECT download_id_a, download_id_b FROM duplicate_scan_results WHERE id=%s", (result_id,))
        if not row:
            db.close()
            return jsonify({"error": "Пару не знайдено"}), 404

        if action in ("delete_a", "delete_b"):
            target_id = row["download_id_a"] if action == "delete_a" else row["download_id_b"]
            # У кошик: і файл, і запис. Раніше тут стирався лише файл, а запис
            # лишався — у галереї висіла картка без файлу.
            try:
                trash_file(target_id, current_user_id())
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

        else:
            db.close()
            return jsonify({"error": f"Невідома дія: {action}"}), 400

        with db.cursor() as cur:
            cur.execute("DELETE FROM duplicate_scan_results WHERE id=%s", (result_id,))
        db.commit(); db.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _recover_pending_file(row: dict, user_id: int | None = None) -> str | None:
    """
    Якщо файл з pending_review зник — відновлює його.
    Спочатку пробує скачати з e621/Inkbunny по прямому URL файлу,
    якщо не виходить — через WebDAV з Nextcloud (nc_target_path).
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

    # 2. Fallback — качаємо з Nextcloud (файл там вже є як "matched" або новий не завантажили)
    try:
        nc_cfg      = get_user_nc_config()
        folder_name = row.get("folder_name", "")
        base_folder = nc_cfg.get("base_folder", "").strip("/")
        nc_path     = f"{base_folder}/{folder_name}/{filename}"

        from webdav3.client import Client
        client = Client({
            "webdav_hostname": nc_cfg.get("webdav_url", ""),
            "webdav_login":    nc_cfg.get("username", ""),
            "webdav_password": nc_cfg.get("password", ""),
            "disable_check":   True,
        })
        if client.check(nc_path):
            client.download_sync(remote_path=nc_path, local_path=local_path)
            print(f"  ✓ Відновлено з Nextcloud: {filename}")
            return local_path
    except Exception as e:
        print(f"  ⚠ Не вдалося відновити {filename} з Nextcloud: {e}")

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
