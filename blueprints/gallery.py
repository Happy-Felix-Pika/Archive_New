"""Галерея"""

import hashlib
import mimetypes, os, random, threading

from flask import Blueprint, Response, jsonify, request as flask_request, send_file


from core.auth import current_user_id, get_user_nc_config
from core import events
from core.warmcache import WarmCache
from core.db import table_exists, get_db, q, qq
from core.imgcache import _IMG_CACHE_DIR
from core.nextcloud import nc_share_url
from core.files_api import get_file, local_path
from core.storage import get_storage_mode, local_file_path

bp = Blueprint("gallery", __name__)


# ── Gallery ───────────────────────────────────────────────────────────────

_gallery_lock: threading.Lock = threading.Lock()


def _gallery_ids_query(f, artist_id, subfolder, tag_ids, hide_tag_ids, favorites_only, user_id, include_post_id=False, exclude_folder_names=None, include_file_ids=None):
    """
    Витягнута логіка з api_gallery_ids(), щоб можна було викликати і зі
    звичайного ендпоінта (по tag_id), і з зовнішнього доступу (де теги
    передаються НАЗВАМИ, бо ID тегів різні в кожній базі, і резолвляться
    в tag_id тут-таки, ще до виклику цієї функції).
    exclude_folder_names — папки (folder_name), які приховані для
    конкретного ключа доступу; не стосується звичайного локального виклику.
    """
    IMAGE_EXTS = ('png','jpg','jpeg','gif','webp')
    VIDEO_EXTS = ('webm','mp4')
    FLASH_EXTS = ('swf',)

    if f == "image":   exts = IMAGE_EXTS
    elif f == "video": exts = VIDEO_EXTS
    elif f == "flash": exts = FLASH_EXTS
    else:              exts = IMAGE_EXTS + VIDEO_EXTS

    # Найчастіше — «усе / фото / відео / флеш» без жодних фільтрів. Цей
    # список (сотні тисяч id) рахується заздалегідь у фоні; тут лише
    # перемішується готовий.
    plain = not (artist_id or subfolder or tag_ids or hide_tag_ids or favorites_only
                 or include_post_id or exclude_folder_names or include_file_ids)
    if plain:
        try:
            ids = list(_all_ids_cache.get().get(f if f in ("image", "video", "flash") else "all", []))
            random.shuffle(ids)
            return jsonify({"ids": ids})
        except Exception:
            pass          # бази нема чи кеш не порахувався — нижче наживо

    # Без застосунку furry таблиці furry_posts немає — тоді галерея просто
    # показує файли, без фільтрів за автором і серією.
    has_furry = table_exists(name="furry_posts")   # відповідь кешується
    if not has_furry:
        artist_id, subfolder = None, None

    placeholders = ",".join(["%s"] * len(exts))
    args = list(exts)
    # Основа — таблиця ядра. furry_posts приєднуємо НЕОБОВ'ЯЗКОВО:
    # файл може не мати ані автора, ані поста, і все одно має бути в галереї.
    where = [
        "f.name IS NOT NULL",
        f"f.ext IN ({placeholders})",
    ]
    join_artists = ""
    if exclude_folder_names:
        # Ховаємо за ТЕКОЮ У СХОВИЩІ, а не за автором: тека може не мати
        # ніякого автора (власні документи, фото), і її теж треба вміти
        # приховати. Файл у корені сховища рахується за теку '_root' —
        # так само, як це позначалось раніше.
        ph = ",".join(["%s"] * len(exclude_folder_names))
        folder_ok = ("(CASE WHEN f.rel_path LIKE '%%/%%' "
                     "THEN SUBSTRING_INDEX(f.rel_path, '/', 1) ELSE '_root' END) "
                     f"NOT IN ({ph})")
        args.extend(exclude_folder_names)
        # Винятки: окремі файли з прихованої теки, явно відкриті для ключа.
        # Тека лишається закритою, видно лише ці файли.
        if include_file_ids:
            ids = [int(i) for i in include_file_ids]
            eph = ",".join(["%s"] * len(ids))
            where.append(f"({folder_ok} OR f.id IN ({eph}))")
            args.extend(ids)
        else:
            where.append(folder_ok)

    if artist_id:
        where.append("fp.artist_id = %s")
        args.append(int(artist_id))

    pool_join = ""
    if subfolder:
        # Пул — таблиця сторінок furry_pool_items (і файли з теки пулу, і ті,
        # що привʼязані до нього додатково). Назва могла прийти будь-яка з
        # назв пулу (e621 / Inkbunny / тека) — шукаємо серед усіх.
        pdb = get_db()
        pargs = [subfolder] + ([int(artist_id)] if artist_id else [])
        pool_ids = [r["pool_id"] for r in qq(pdb, "SELECT DISTINCT pool_id FROM furry_pool_names WHERE name=%s"
                                              + (" AND artist_id=%s" if artist_id else ""), pargs)]
        if not pool_ids:
            return jsonify({"ids": []})
        pool_join = (" JOIN furry_pool_items pi ON pi.file_id = f.id AND pi.pool_id IN ("
                     + ",".join(str(int(i)) for i in pool_ids) + ")")

    if favorites_only:
        where.append("EXISTS (SELECT 1 FROM user_favorites uf WHERE uf.user_id=%s AND uf.download_id=f.id)")
        args.append(user_id)

    for tid in tag_ids:
        where.append("EXISTS (SELECT 1 FROM download_tags dt WHERE dt.download_id=f.id AND dt.tag_id=%s)")
        args.append(int(tid))

    for tid in hide_tag_ids:
        where.append("NOT EXISTS (SELECT 1 FROM download_tags dt WHERE dt.download_id=f.id AND dt.tag_id=%s)")
        args.append(int(tid))

    order_args = []
    # номер сторінки — свій у кожному пулі
    order_by = "pi.page IS NULL, pi.page ASC, f.id ASC" if subfolder else "f.id ASC"
    select_cols = "f.id, fp.post_id" if include_post_id else "f.id"
    # furry_posts приєднуємо лише коли він справді потрібен (автор, пул,
    # post_id, приховані теки) — на сотнях тисяч файлів зайве приєднання
    # подвоює час запиту
    need_fp = bool(artist_id or include_post_id or join_artists)
    if has_furry and (need_fp or pool_join):
        joins = ("LEFT JOIN furry_posts fp ON fp.file_id = f.id " if need_fp else "") + join_artists + pool_join
    else:
        joins = ""
        select_cols = select_cols.replace(", fp.post_id", ", NULL AS post_id")
    sql = f"SELECT {select_cols} FROM files f {joins} WHERE {' AND '.join(where)} ORDER BY {order_by}"
    try:
        db   = get_db()
        rows = qq(db, sql, args + order_args)
        db.close()
        if include_post_id:
            items = [{"id": r["id"], "post_id": r["post_id"]} for r in rows]
            if not subfolder:
                random.shuffle(items)
            return jsonify({"ids": items})
        ids = [r["id"] for r in rows]
        if not subfolder:
            random.shuffle(ids)
        return jsonify({"ids": ids})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


def _compute_all_ids(db):
    """Усі id файлів галереї, розкладені за типом — один прохід по таблиці."""
    image = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    video = {'webm', 'mp4'}
    out = {"all": [], "image": [], "video": [], "flash": []}
    import pymysql
    with db.cursor(pymysql.cursors.Cursor) as cur:      # кортежі — помітно швидше за словники
        cur.execute("SELECT id, ext FROM files WHERE name IS NOT NULL "
                    "AND ext IN ('png','jpg','jpeg','gif','webp','webm','mp4','swf')")
        for fid, ext in cur.fetchall():
            if ext in image:
                out["image"].append(fid); out["all"].append(fid)
            elif ext in video:
                out["video"].append(fid); out["all"].append(fid)
            else:
                out["flash"].append(fid)
    return out


# Перераховується після змін в архіві й раз на 10 хв (нове від архіватора)
_all_ids_cache = WarmCache("gallery-ids", _compute_all_ids, interval=600)


def _resolve_tag_names(names_csv: str) -> list:
    """Перетворює список імен тегів (через кому) у ID тегів У НАШІЙ ВЛАСНІЙ
    базі — потрібно, бо той, хто звертається ззовні, не знає (і не може
    знати) наші внутрішні числові tag_id."""
    names = [n.strip().lower() for n in (names_csv or "").split(",") if n.strip()]
    if not names:
        return []
    db = get_db()
    placeholders = ",".join(["%s"] * len(names))
    rows = qq(db, f"SELECT id FROM tags WHERE name IN ({placeholders})", names)
    db.close()
    return [str(r["id"]) for r in rows]


@bp.route("/api/gallery/ids")
def api_gallery_ids():
    f            = flask_request.args.get("filter",    "all")
    artist_id    = flask_request.args.get("artist_id", None)
    subfolder    = flask_request.args.get("subfolder", None)  # фільтр по pool
    tag_ids      = flask_request.args.getlist("tag_id")       # показати тільки ці
    hide_tag_ids = flask_request.args.getlist("hide_tag_id")  # виключити ці
    favorites_only = flask_request.args.get("favorites", "") == "1"
    return _gallery_ids_query(f, artist_id, subfolder, tag_ids, hide_tag_ids, favorites_only, current_user_id())


@bp.route("/api/gallery/meta/<int:download_id>")
def api_gallery_meta(download_id):
    """
    Картка файлу.

    Ядро віддає тільки те, що є в будь-якого файлу: ім'я, розміри, теги,
    улюблене. Автора, джерело, серію тощо дописують застосунки через
    фільтр 'file.meta' — ядро про ці поля не знає взагалі.
    """
    user_id = current_user_id()
    try:
        db  = get_db()
        row = q(db, """
            SELECT f.id, f.name AS filename, f.ext AS file_ext, f.rel_path,
                   f.width, f.height, f.size_bytes,
                   EXISTS(SELECT 1 FROM user_favorites uf
                           WHERE uf.user_id=%s AND uf.download_id=f.id) AS is_favorite
            FROM files f
            WHERE f.id = %s
        """, (user_id, download_id))
        if not row or not row["filename"]:
            db.close()
            return jsonify({"error": "not found"}), 404
        tags = qq(db, """
            SELECT t.id, t.name, t.category
            FROM tags t JOIN download_tags dt ON dt.tag_id=t.id
            WHERE dt.download_id=%s ORDER BY t.name
        """, (download_id,))

        mime = mimetypes.guess_type(row["filename"])[0] or "application/octet-stream"
        data = {
            "id":          download_id,
            "filename":    row["filename"],
            "file_ext":    row["file_ext"],
            "rel_path":    row["rel_path"],
            "width":       row["width"],
            "height":      row["height"],
            "size_bytes":  row["size_bytes"],
            "is_favorite": bool(row["is_favorite"]),
            "mime":        mime,
            "is_video":    mime.startswith("video/"),
            "stream_url":  f"/api/gallery/stream/{download_id}",
            "tags":        list(tags),
        }
        # Невирішені пропозиції тегів: користувач бачить свої, модератор — усі
        try:
            from core.auth import is_current_user_moderator
            from core.tagops import for_file
            data["tag_suggestions"] = for_file(download_id, None if is_current_user_moderator() else user_id)
        except Exception:
            data["tag_suggestions"] = []
        data = events.apply_filters("file.meta", data, file_id=download_id, db=db)
        db.close()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/gallery/stream/<int:download_id>")
def api_gallery_stream(download_id):
    """Віддає файл з активного сховища (Nextcloud або локальний диск)."""
    try:
        db  = get_db()
        # Читаємо з ядра: шлях до файлу більше не збирається з автора.
        row = get_file(download_id, db)
        db.close()
        if not row or not row["name"]:
            return jsonify({"error": "not found"}), 404

        mime = mimetypes.guess_type(row["name"])[0] or "application/octet-stream"

        if get_storage_mode() == "local":
            fpath = local_path(row["rel_path"])
            if not os.path.isfile(fpath):
                return jsonify({"error": "Файл не знайдено на диску"}), 404
            return send_file(fpath, mimetype=mime, conditional=True)

        nc_cfg      = get_user_nc_config()
        base_folder = nc_cfg.get("base_folder", "").strip("/")
        webdav_url  = nc_cfg.get("webdav_url", "").rstrip("/")
        # rel_path — це і є шлях усередині сховища (@автор/Пул/файл),
        # тобто рівно те, що раніше збиралося з folder_name + subfolder + filename.
        nc_path     = f"{base_folder}/{row['rel_path']}" if base_folder else row["rel_path"]
        file_url    = f"{webdav_url}/{nc_path}"
        is_video    = mime.startswith("video/")

        import requests as req

        # ── Зображення: диск-кеш (швидко при повторному перегляді)
        if not is_video:
            cache_key  = hashlib.md5(f"{download_id}:{row['name']}".encode()).hexdigest()
            cache_path = os.path.join(_IMG_CACHE_DIR, cache_key)

            if not os.path.exists(cache_path):
                # Завантажуємо і кешуємо
                r = req.get(file_url,
                            auth=(nc_cfg.get("username",""), nc_cfg.get("password","")),
                            stream=True, timeout=30)
                r.raise_for_status()
                with open(cache_path, "wb") as f:
                    for chunk in r.iter_content(65536):
                        if chunk: f.write(chunk)

            # Віддаємо з кешу з підтримкою Range
            file_size    = os.path.getsize(cache_path)
            range_header = flask_request.headers.get("Range")

            if range_header:
                import re as _re
                m = _re.match(r"bytes=(\d+)-(\d*)", range_header)
                start = int(m.group(1))
                end   = int(m.group(2)) if m.group(2) else file_size - 1
                length = end - start + 1
                def gen_range():
                    with open(cache_path, "rb") as f:
                        f.seek(start)
                        remaining = length
                        while remaining:
                            chunk = f.read(min(65536, remaining))
                            if not chunk: break
                            remaining -= len(chunk)
                            yield chunk
                return Response(gen_range(), status=206, headers={
                    "Content-Type":   mime,
                    "Content-Range":  f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(length),
                    "Accept-Ranges":  "bytes",
                    "Cache-Control":  "public, max-age=3600",
                })
            else:
                def gen_full():
                    with open(cache_path, "rb") as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk: break
                            yield chunk
                return Response(gen_full(), status=200, headers={
                    "Content-Type":   mime,
                    "Content-Length": str(file_size),
                    "Accept-Ranges":  "bytes",
                    "Cache-Control":  "public, max-age=3600",
                })

        # ── Відео: стрім напряму (без кешу — файли великі)
        range_header = flask_request.headers.get("Range")
        headers = {}
        if range_header:
            headers["Range"] = range_header

        nc_resp = req.get(file_url,
                          auth=(nc_cfg.get("username",""), nc_cfg.get("password","")),
                          headers=headers, stream=True, timeout=30)

        status_code = nc_resp.status_code
        def gen_video():
            for chunk in nc_resp.iter_content(chunk_size=131072):
                if chunk: yield chunk

        resp_headers = {
            "Content-Type":  mime,
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
        }
        if "Content-Length" in nc_resp.headers:
            resp_headers["Content-Length"] = nc_resp.headers["Content-Length"]
        if "Content-Range" in nc_resp.headers:
            resp_headers["Content-Range"] = nc_resp.headers["Content-Range"]

        return Response(gen_video(), status=status_code, headers=resp_headers)

    except req.exceptions.HTTPError as e:
        # Файл реально відсутній на Nextcloud (видалили/перенесли вручну,
        # чи pool-реорганізація рознесла шляхи) — це не помилка сервера,
        # а очікуваний стан, тому 404, а не 500.
        status = e.response.status_code if e.response is not None else 502
        if status == 404:
            return jsonify({"error": "Файл не знайдено на Nextcloud (можливо видалений/перенесений)"}), 404
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Псевдонім для старого ендпоінту (щоб не ламати старі запити)
@bp.route("/api/gallery/file/<int:download_id>")
def api_gallery_file(download_id):
    return api_gallery_stream(download_id)
