"""Галерея"""

import hashlib
import mimetypes, os, random, threading

from flask import Blueprint, Response, jsonify, request as flask_request, send_file


from core.auth import current_user_id
from core import events
from core.warmcache import WarmCache
from core.db import table_exists, get_db, q, qq
from core.imgcache import _IMG_CACHE_DIR
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
    #
    # З фільтрами (теги, автор, улюблене) — те саме, без проходу по всій
    # таблиці файлів: кожен фільтр — це короткий запит по індексу
    # («файли з цим тегом», «файли цього автора»), а перетин і тип файлу
    # рахуються в пам'яті. Час залежить від розміру фільтра, а не архіву.
    if not (subfolder or include_post_id or exclude_folder_names or include_file_ids):
        try:
            if not (artist_id or tag_ids or hide_tag_ids or favorites_only):
                return _plain_response(f)
            if hide_tag_ids and not (artist_id or tag_ids or favorites_only):
                # лише «сховати теги» — з уже перемішаного готового списку
                return _plain_response(f, hide_tag_ids)
            ids = _fast_ids(f, artist_id, tag_ids, hide_tag_ids, favorites_only, user_id)
            random.shuffle(ids)
            return jsonify({"ids": ids})
        except Exception as e:
            print(f"  ⚠ Галерея: швидкий шлях не вдався ({e}) — рахую наживо")

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

    if artist_id == "fav":
        # роботи всіх улюблених авторів цього користувача
        where.append("fp.artist_id IN (SELECT artist_id FROM furry_favorite_artists WHERE user_id=%s)")
        args.append(user_id)
    elif artist_id:
        where.append("fp.artist_id = %s")
        args.append(int(artist_id))

    pool_join = ""
    if subfolder:
        # Пул — таблиця сторінок furry_pool_items (і файли з теки пулу, і ті,
        # що привʼязані до нього додатково). Назва могла прийти будь-яка з
        # назв пулу (e621 / Inkbunny / тека) — шукаємо серед усіх.
        pdb = get_db()
        one_artist = artist_id if artist_id and artist_id != "fav" else None
        pargs = [subfolder] + ([int(one_artist)] if one_artist else [])
        pool_ids = [r["pool_id"] for r in qq(pdb, "SELECT DISTINCT pool_id FROM furry_pool_names WHERE name=%s"
                                              + (" AND artist_id=%s" if one_artist else ""), pargs)]
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


_KIND_CODE = {**{e: 1 for e in ('png', 'jpg', 'jpeg', 'gif', 'webp')},
              **{e: 2 for e in ('webm', 'mp4')}, 'swf': 3}
_FILTER_CODES = {"image": (1,), "video": (2,), "flash": (3,)}


def _compute_all_ids(db):
    """
    Усі id файлів галереї, розкладені за типом — один прохід по таблиці.
    "kind" — тип кожного файлу за його id (1 байт на id: фото/відео/флеш),
    щоб відфільтровані списки перевіряти миттєво, не питаючи базу.
    """
    out = {"all": [], "image": [], "video": [], "flash": []}
    import pymysql
    rows = []
    with db.cursor(pymysql.cursors.Cursor) as cur:      # кортежі — помітно швидше за словники
        cur.execute("SELECT id, ext FROM files WHERE name IS NOT NULL "
                    "AND ext IN ('png','jpg','jpeg','gif','webp','webm','mp4','swf')")
        rows = cur.fetchall()
    kind = bytearray((max((r[0] for r in rows), default=0)) + 1)
    for fid, ext in rows:
        code = _KIND_CODE.get(ext, 0)
        kind[fid] = code
        if code == 1:
            out["image"].append(fid); out["all"].append(fid)
        elif code == 2:
            out["video"].append(fid); out["all"].append(fid)
        elif code == 3:
            out["flash"].append(fid)
    out["kind"] = kind
    return out


# Готова відповідь «усе/фото/відео/флеш»: вже перемішана й перетворена в
# JSON. Віддається миттєво, а наступна (з новим порядком) готується у фоні.
_plain_ready = {}            # kind -> (перемішаний список, bytes)
_plain_lock = threading.Lock()


def _build_plain(kind, data):
    import json
    ids = list(data.get(kind, []))
    random.shuffle(ids)
    ready = (ids, json.dumps({"ids": ids}, separators=(",", ":")).encode())
    _plain_ready[kind] = ready
    return ready


def _plain_response(f, hide_tag_ids=None):
    """
    Готовий перемішаний список віддається одразу, а новий порядок для
    наступного відкриття готується у фоні. Якщо щойно щось додали чи
    видалили — у цій відповіді ще може бути попередній список (секунди),
    у наступній уже новий.
    """
    import json
    from flask import Response
    kind = f if f in ("image", "video", "flash") else "all"
    data = _all_ids_cache.get()
    with _plain_lock:
        ready = _plain_ready.pop(kind, None)
    if not ready:
        ready = _build_plain(kind, data)
        _plain_ready.pop(kind, None)
    threading.Thread(target=_build_plain, args=(kind, _all_ids_cache.get()), daemon=True).start()
    if not hide_tag_ids:
        return Response(ready[1], mimetype="application/json")
    db = get_db()
    try:
        ph = ",".join(["%s"] * len(hide_tag_ids))
        hidden = set(_col(db, f"SELECT download_id FROM download_tags WHERE tag_id IN ({ph})",
                          [int(t) for t in hide_tag_ids]))
    finally:
        db.close()
    ids = [i for i in ready[0] if i not in hidden]
    return Response(json.dumps({"ids": ids}, separators=(",", ":")), mimetype="application/json")


def _col(db, sql, args=()):
    """Перший стовпчик результату — списком (кортежі швидші за словники)."""
    import pymysql
    with db.cursor(pymysql.cursors.Cursor) as cur:
        cur.execute(sql, args)
        return [r[0] for r in cur.fetchall()]


def _fast_ids(f, artist_id, tag_ids, hide_tag_ids, favorites_only, user_id) -> list:
    cache = _all_ids_cache.get()
    codes = _FILTER_CODES.get(f, (1, 2))
    filters = []          # (sql, args) — множини id, які треба перетнути
    for tid in tag_ids:
        filters.append(("SELECT download_id FROM download_tags WHERE tag_id=%s", (int(tid),)))
    if favorites_only:
        filters.append(("SELECT download_id FROM user_favorites WHERE user_id=%s", (user_id,)))
    if artist_id == "fav":
        filters.append(("""SELECT fp.file_id FROM furry_favorite_artists fa
                           JOIN furry_posts fp ON fp.artist_id = fa.artist_id
                           WHERE fa.user_id=%s""", (user_id,)))
    elif artist_id:
        filters.append(("SELECT file_id FROM furry_posts WHERE artist_id=%s", (int(artist_id),)))
    if not filters and not hide_tag_ids:
        return list(cache.get(f if f in ("image", "video", "flash") else "all", []))

    db = get_db()
    try:
        if filters:
            cand = None
            for sql, args in filters:
                got = _col(db, sql, args)
                cand = set(got) if cand is None else cand.intersection(got)
                if not cand:
                    return []
            kind = cache["kind"]
            n = len(kind)
            ids = [i for i in cand if i < n and kind[i] in codes]
            newer = [i for i in cand if i >= n]       # додане після останнього підрахунку
            if newer:
                ph = ",".join(["%s"] * len(newer))
                for fid, ext in qq_tuples(db, f"SELECT id, ext FROM files WHERE name IS NOT NULL AND id IN ({ph})", newer):
                    if _KIND_CODE.get(ext, 0) in codes:
                        ids.append(fid)
        else:
            ids = list(cache.get(f if f in ("image", "video", "flash") else "all", []))
        if hide_tag_ids:
            ph = ",".join(["%s"] * len(hide_tag_ids))
            hidden = set(_col(db, f"SELECT download_id FROM download_tags WHERE tag_id IN ({ph})",
                              [int(t) for t in hide_tag_ids]))
            if hidden:
                ids = [i for i in ids if i not in hidden]
        return ids
    finally:
        db.close()


def qq_tuples(db, sql, args=()):
    import pymysql
    with db.cursor(pymysql.cursors.Cursor) as cur:
        cur.execute(sql, args)
        return cur.fetchall()


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
        from core.storage_api import is_service_path
        if not row or not row["filename"] or is_service_path(row["rel_path"]):
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
    """Віддає файл зі сховища (локальна тека)."""
    try:
        db  = get_db()
        # Читаємо з ядра: шлях до файлу більше не збирається з автора.
        row = get_file(download_id, db)
        db.close()
        from core.storage_api import is_service_path
        if not row or not row["name"] or is_service_path(row["rel_path"]):
            return jsonify({"error": "not found"}), 404

        mime = mimetypes.guess_type(row["name"])[0] or "application/octet-stream"

        fpath = local_path(row["rel_path"])
        if not os.path.isfile(fpath):
            return jsonify({"error": "Файл не знайдено на диску"}), 404
        return send_file(fpath, mimetype=mime, conditional=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/gallery/thumb/<int:download_id>")
def api_gallery_thumb(download_id):
    """Мініатюра файлу за id — для плиток (улюблене тощо)."""
    from blueprints.catalog import api_catalog_thumb
    db = get_db()
    row = get_file(download_id, db)
    db.close()
    from core.storage_api import is_service_path
    if not row or is_service_path(row["rel_path"]):
        return jsonify({"error": "not found"}), 404
    resp = api_catalog_thumb(rel_path=row["rel_path"])
    return resp


# Псевдонім для старого ендпоінту (щоб не ламати старі запити)
@bp.route("/api/gallery/file/<int:download_id>")
def api_gallery_file(download_id):
    return api_gallery_stream(download_id)
