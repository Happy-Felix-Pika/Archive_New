"""Онлайн-джерела (e621 / Inkbunny)"""

import hashlib
import json, mimetypes, os, time

from flask import Blueprint, Response, jsonify, request as flask_request, send_file


from core.auth import admin_required, current_user_id, login_required
from core.config import load_config
from core.db import get_db, q, qq, table_exists
from core.imgcache import _IMG_CACHE_DIR

bp = Blueprint("live", __name__)


def get_user_live_credentials(user_id: int) -> dict:
    """
    Персональні credentials для 'Онлайн' — на відміну від спільного
    archiver_config.json (яким користується сам скрипт скачування),
    це зберігається окремо на кожного юзера в users.settings_json.
    """
    try:
        db  = get_db()
        row = q(db, "SELECT settings_json FROM users WHERE id=%s", (user_id,))
        db.close()
        if row and row.get("settings_json"):
            return json.loads(row["settings_json"])
    except Exception:
        pass
    return {}


def get_live_bookmarks(user_id: int) -> list:
    """Список ВСІХ закладок 'де я зупинився' в Онлайн — той самий
    settings_json, окремий ключ. Тепер список, а не одна закладка —
    кожна має свій id, назву й джерело."""
    return get_user_live_credentials(user_id).get("_live_bookmarks") or []


def _save_live_bookmarks_list(user_id: int, bookmarks: list):
    current = get_user_live_credentials(user_id)
    current["_live_bookmarks"] = bookmarks
    db = get_db()
    with db.cursor() as cur:
        cur.execute("UPDATE users SET settings_json=%s WHERE id=%s", (json.dumps(current), user_id))
    db.commit(); db.close()


def add_live_bookmark(user_id: int, bookmark: dict) -> dict:
    bookmarks = get_live_bookmarks(user_id)
    bookmark["id"] = int(time.time() * 1000)  # унікальний і водночас сортується за часом
    bookmark["saved_at"] = time.time()
    bookmarks.append(bookmark)
    _save_live_bookmarks_list(user_id, bookmarks)
    return bookmark


def delete_live_bookmark_by_id(user_id: int, bookmark_id: int):
    bookmarks = [b for b in get_live_bookmarks(user_id) if b.get("id") != bookmark_id]
    _save_live_bookmarks_list(user_id, bookmarks)


@bp.route("/api/live/bookmarks")
def api_live_bookmarks_list():
    """Список закладок — можна відфільтрувати по джерелу через ?source=."""
    source_id = flask_request.args.get("source", "").strip()
    bookmarks = get_live_bookmarks(current_user_id())
    if source_id:
        bookmarks = [b for b in bookmarks if b.get("source") == source_id]
    bookmarks.sort(key=lambda b: b.get("saved_at", 0), reverse=True)
    return jsonify(bookmarks)


@bp.route("/api/live/bookmarks", methods=["POST"])
def api_live_bookmarks_save():
    body = flask_request.get_json(silent=True) or {}
    if not body.get("name", "").strip():
        return jsonify({"error": "Вкажи назву закладки"}), 400
    try:
        saved = add_live_bookmark(current_user_id(), body)
        return jsonify({"ok": True, "bookmark": saved})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/live/bookmarks/<int:bookmark_id>", methods=["DELETE"])
def api_live_bookmarks_delete(bookmark_id):
    try:
        delete_live_bookmark_by_id(current_user_id(), bookmark_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def save_user_live_credentials(user_id: int, patch: dict):
    """Мерджить передані секції (e621/inkbunny) у персональний settings_json юзера."""
    current = get_user_live_credentials(user_id)
    for section, values in patch.items():
        current.setdefault(section, {}).update(values)
    db = get_db()
    with db.cursor() as cur:
        cur.execute("UPDATE users SET settings_json=%s WHERE id=%s", (json.dumps(current), user_id))
    db.commit(); db.close()
    # Скидаємо кешований завантажувач цього юзера — щоб нові credentials
    # підхопились одразу, без перезапуску сервера
    for sid in all_sources():
        _source_instance_cache.pop((sid, user_id), None)


from .sources import all_sources, get_source


def _get_source_instance(source_id: str, user_id: int):
    """
    Повертає інстанс потрібного 'живого' джерела (e621/Inkbunny/будь-яке
    нове зареєстроване) з ЙОГО ВЛАСНИМИ credentials цього конкретного юзера.
    Кешується на (source_id, user_id), щоб не пересоздавати сесію на кожен
    запит — так само, як раніше окремі get_live_e621()/get_live_inkbunny().
    """
    cache_key = (source_id, user_id)
    if cache_key in _source_instance_cache:
        return _source_instance_cache[cache_key]
    creds = get_user_live_credentials(user_id).get(source_id, {})
    src = get_source(source_id, creds)
    if src is not None:
        _source_instance_cache[cache_key] = src
    return src


_source_instance_cache = {}


@bp.route("/api/live/proxy")
def api_live_proxy():
    """
    Проксує картинку/відео з e621/Inkbunny через наш сервер (той самий домен,
    що й дашборд). Потрібно, бо пряме завантаження зі стороннього CDN у
    браузері іноді ловить ERR_BLOCKED_BY_CLIENT — адблок/приватність-розширення
    сприймають прямі посилання на сторонні CDN як трекери/рекламу.
    Кешується на диску так само, як і Nextcloud-прев'ю.
    """
    url = flask_request.args.get("url", "")
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    allowed_suffixes = ("e621.net", "inkbunny.net", "metapix.net")
    if not url or not host or not any(host == d or host.endswith("." + d) for d in allowed_suffixes):
        return jsonify({"error": "Дозволені лише e621/Inkbunny CDN"}), 400

    mime = mimetypes.guess_type(url)[0] or "application/octet-stream"
    is_video = mime.startswith("video/")

    # metapix.net (CDN картинок Inkbunny) схоже вимагає Referer, що вказує
    # на сам inkbunny.net — типовий захист від хотлінкінгу. Без цього
    # заголовка CDN міг віддавати помилку (403), яку ми показували як 500.
    fetch_headers = {"User-Agent": "FurryArchiveDashboard/1.0"}
    if "metapix.net" in host or "inkbunny.net" in host:
        fetch_headers["Referer"] = "https://inkbunny.net/"

    try:
        import requests as req
        if not is_video:
            cache_key  = hashlib.md5(url.encode()).hexdigest()
            cache_path = os.path.join(_IMG_CACHE_DIR, f"live_{cache_key}")
            if not os.path.exists(cache_path):
                r = req.get(url, stream=True, timeout=20, headers=fetch_headers)
                r.raise_for_status()
                with open(cache_path, "wb") as f:
                    for chunk in r.iter_content(65536):
                        if chunk: f.write(chunk)
            return send_file(cache_path, mimetype=mime, conditional=True)

        # Відео — стрімимо напряму, без кешу (можуть бути важкі)
        range_header = flask_request.headers.get("Range")
        if range_header: fetch_headers["Range"] = range_header
        resp = req.get(url, headers=fetch_headers, stream=True, timeout=30)

        def gen():
            for chunk in resp.iter_content(chunk_size=131072):
                if chunk: yield chunk

        resp_headers = {"Content-Type": mime, "Accept-Ranges": "bytes", "Cache-Control": "no-store"}
        if "Content-Length" in resp.headers: resp_headers["Content-Length"] = resp.headers["Content-Length"]
        if "Content-Range" in resp.headers:  resp_headers["Content-Range"]  = resp.headers["Content-Range"]
        return Response(gen(), status=resp.status_code, headers=resp_headers)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/live/tag-autocomplete")
def api_live_tag_autocomplete():
    """Автодоповнення тегів під час вводу — делегує в конкретне джерело
    (для джерел без supports_tags повертає порожньо, як і має бути)."""
    q = flask_request.args.get("q", "").strip()
    source_id = flask_request.args.get("source", "e621")
    if not q:
        return jsonify([])
    src = _get_source_instance(source_id, current_user_id())
    if not src or not src.supports_tags:
        return jsonify([])
    try:
        return jsonify(src.tag_autocomplete(q))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/live/sources")
@login_required
def api_live_sources():
    """
    Опис усіх доступних джерел — усе, що потрібно сторінці, щоб
    намалювати себе: назва, поля для ключів і що джерело вміє.

    Сторінка «Онлайн» одна на всіх і будується з цього опису. Тому нове
    джерело не потребує НІЯКИХ змін у фронтенді: з'явилось у списку —
    з'явилось на сторінці з потрібними кнопками.
    """
    creds_all = get_user_live_credentials(current_user_id())
    out = []
    for sid, cls in sorted(all_sources().items()):
        cfg = creds_all.get(sid, {}) or {}
        fields = []
        for f in cls.credential_fields:
            fields.append({**f, "filled": bool(cfg.get(f["key"]))})
        out.append({
            "id":    sid,
            "label": cls.label or sid,
            "credential_fields": fields,
            "configured": all(cfg.get(f["key"]) for f in cls.credential_fields),
            "supports": {
                "tags":            bool(getattr(cls, "supports_tags", True)),
                "pools":           bool(getattr(cls, "supports_pools", False)),
                "real_favorites":  bool(getattr(cls, "supports_real_favorites", False)),
                "favs_of_others":  bool(getattr(cls, "supports_favs_of_others", False)),
            },
            "custom": bool(getattr(cls, "is_custom", False)),
        })
    return jsonify(out)


@bp.route("/api/live/status")
def api_live_status():
    """Статус підключення для КОЖНОГО зареєстрованого джерела — персонально
    для поточного юзера. Проходить по SOURCES автоматично, тож нове
    зареєстроване джерело з'явиться тут без жодних змін коду."""
    user_id = current_user_id()
    out = {}
    for sid, cls in all_sources().items():
        src = _get_source_instance(sid, user_id)
        configured = src.is_configured() if src else False
        authorized = False
        if configured:
            try:
                authorized = src.is_authorized()
            except Exception:
                authorized = False
        out[sid] = {"enabled": True, "authorized": authorized, "configured": configured,
                    "label": cls.label or sid}
    return jsonify(out)


@bp.route("/api/live/credentials")
def api_live_credentials_get():
    """Повертає стан персональних credentials поточного юзера для КОЖНОГО
    зареєстрованого джерела (без паролю/ключа у відкритому вигляді —
    тільки чи вони вказані)."""
    creds_all = get_user_live_credentials(current_user_id())
    out = {}
    for sid, cls in all_sources().items():
        cfg = creds_all.get(sid, {}) or {}
        status = {}
        for f in cls.credential_fields:
            if f["type"] == "password":
                status[f"has_{f['key']}"] = bool(cfg.get(f["key"]))
            else:
                status[f["key"]] = cfg.get(f["key"], "")
        out[sid] = status
    return jsonify(out)


@bp.route("/api/live/credentials/copy-from-config", methods=["POST"])
@admin_required
def api_live_credentials_copy_from_config():
    """
    Копіює e621/Inkbunny credentials зі спільного Конфігу (те, чим
    користується сам процес скачування) у персональні дані ПОТОЧНОГО
    юзера для 'Онлайн' — щоб не вводити те саме вручну вдруге.
    Секрет ніколи не потрапляє у відповідь фронтенду — читається і
    зберігається тут-таки, на бекенді.
    """
    file_cfg     = load_config()
    e621_cfg     = file_cfg.get("e621", {})
    inkbunny_cfg = file_cfg.get("inkbunny", {})
    patch = {}
    if e621_cfg.get("username") or e621_cfg.get("api_key"):
        patch["e621"] = {"username": e621_cfg.get("username", ""), "api_key": e621_cfg.get("api_key", "")}
    if inkbunny_cfg.get("username") or inkbunny_cfg.get("password"):
        patch["inkbunny"] = {"username": inkbunny_cfg.get("username", ""), "password": inkbunny_cfg.get("password", "")}
    if not patch:
        return jsonify({"error": "У Конфізі ще нічого не вказано для e621/Inkbunny"}), 400
    try:
        save_user_live_credentials(current_user_id(), patch)
        return jsonify({"ok": True, "copied": list(patch.keys())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/live/credentials", methods=["POST"])
def api_live_credentials_save():
    """Зберігає персональні credentials поточного юзера — приймає будь-яке
    зареєстроване джерело, фільтрує поля по тому, що саме джерело оголосило
    у своєму credential_fields."""
    body = flask_request.get_json(silent=True) or {}
    patch = {}
    for sid, cls in all_sources().items():
        if sid in body:
            allowed_keys = {f["key"] for f in cls.credential_fields}
            values = {k: v for k, v in body[sid].items() if k in allowed_keys and v}
            if values:
                patch[sid] = values
    if not patch:
        return jsonify({"error": "Немає даних для збереження"}), 400
    try:
        save_user_live_credentials(current_user_id(), patch)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/live/favorite", methods=["POST"])
def api_live_favorite_toggle():
    """
    Перемикає 'улюблене'. Якщо джерело має власне API улюбленого
    (supports_real_favorites=True, як e621) — працює напряму через нього.
    Інакше — спільна локальна таблиця live_favorites (для будь-якого
    джерела без свого API, як зараз Inkbunny).
    """
    body      = flask_request.get_json(silent=True) or {}
    source_id = body.get("source")
    post_id   = body.get("id")
    if not source_id or not post_id:
        return jsonify({"error": "source/id required"}), 400

    src = _get_source_instance(source_id, current_user_id())
    if not src:
        return jsonify({"error": "Невідоме джерело"}), 400

    if src.supports_real_favorites:
        if not src.is_configured():
            return jsonify({"error": f"Вкажи свої дані для {src.label} у налаштуваннях"}), 400
        try:
            is_fav = src.toggle_favorite(post_id)
            return jsonify({"is_favorite": is_fav})
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    # Джерело без власного API улюбленого — локальна таблиця
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT id FROM live_favorites WHERE source=%s AND post_id=%s", (source_id, post_id))
            existing = cur.fetchone()
            if existing:
                cur.execute("DELETE FROM live_favorites WHERE id=%s", (existing["id"],))
                is_fav = False
            else:
                cur.execute("""
                    INSERT INTO live_favorites (source, post_id, artist_name, thumb_url, file_url, post_url, is_video, ext)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """, (source_id, post_id, body.get("artist"), body.get("thumb_url"),
                      body.get("file_url"), body.get("post_url"),
                      1 if body.get("is_video") else 0, body.get("ext")))
                is_fav = True
        db.commit(); db.close()
        return jsonify({"is_favorite": is_fav})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/live/favorites")
def api_live_favorites_list():
    """Список 'улюбленого'. Без username — своє власне (через реальне API,
    якщо джерело його підтримує, інакше локальна таблиця). З username —
    ЧУЖЕ улюблене (тільки перегляд, публічна інформація), для будь-якого
    джерела з supports_favs_of_others."""
    source_id = flask_request.args.get("source", "")
    page      = int(flask_request.args.get("page", 1))
    tags      = flask_request.args.get("tags", "").strip()
    exclude   = flask_request.args.get("exclude", "").strip()
    sort      = flask_request.args.get("sort", "").strip()
    username  = flask_request.args.get("username", "").strip()

    src = _get_source_instance(source_id, current_user_id()) if source_id else None

    # Якщо не вказано конкретний юзернейм (тобто дивимось "своє"), а джерело
    # не має власного API для КЕРУВАННЯ улюбленим, але вміє ЧИТАТИ чиєсь
    # (як зараз Inkbunny) — просто підставляємо власний юзернейм з
    # налаштувань і йдемо тим самим шляхом, що й перегляд чужого улюбленого.
    # Це набагато надійніше за окрему локальну таблицю.
    if not username and src and not src.supports_real_favorites and src.supports_favs_of_others:
        username = get_user_live_credentials(current_user_id()).get(source_id, {}).get("username", "")

    if username:
        if not src:
            return jsonify({"results": [], "error": "Невідоме джерело"})
        if not src.supports_favs_of_others:
            return jsonify({"results": [], "error": f"{src.label} не підтримує перегляд чужого улюбленого"})
        if not src.is_configured():
            return jsonify({"results": [], "error": f"Вкажи свої дані для {src.label} у налаштуваннях"})
        try:
            results = src.search_favorites(username=username, tags=tags, exclude=exclude, sort=sort, page=page, limit=60)
        except Exception as e:
            return jsonify({"results": [], "error": str(e)})
        for r in results:
            r["is_favorite"] = (username.lower() == get_user_live_credentials(current_user_id()).get(source_id, {}).get("username", "").lower())
        if results:
            post_ids = [f"{src.id_prefix}{r['id']}" for r in results]
            existing_map = _resolve_in_library(post_ids)
            for r in results:
                dl_id = existing_map.get(f"{src.id_prefix}{r['id']}")
                r["in_library"]  = dl_id is not None
                r["download_id"] = dl_id
        return jsonify({"results": results})

    if src and src.supports_real_favorites:
        if not src.is_configured():
            return jsonify({"results": [], "error": f"Вкажи свої дані для {src.label} у налаштуваннях"})
        try:
            results = src.search_favorites(tags=tags, exclude=exclude, sort=sort, page=page, limit=60)
        except Exception as e:
            return jsonify({"results": [], "error": str(e)})
        for r in results:
            r["is_favorite"] = True
        if results:
            post_ids = [f"{src.id_prefix}{r['id']}" for r in results]
            existing_map = _resolve_in_library(post_ids)
            for r in results:
                dl_id = existing_map.get(f"{src.id_prefix}{r['id']}")
                r["in_library"]  = dl_id is not None
                r["download_id"] = dl_id
        return jsonify({"results": results})

    # Джерело без власного API улюбленого — спільна локальна таблиця
    try:
        db = get_db()
        where, args = [], []
        if source_id:
            where.append("source = %s")
            args.append(source_id)
        sql = "SELECT * FROM live_favorites"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC"
        rows = qq(db, sql, args)
        db.close()

        # Якщо плагін підтримує refresh_details — підтягуємо свіжі дані
        # замість того, що збереглось у момент лайку (могло бути застарілим)
        fresh = {}
        if src and rows:
            try:
                fresh = src.refresh_details([r["post_id"] for r in rows])
            except Exception as e:
                print(f"  ⚠ refresh_details для {source_id}: {e}")

        results = []
        for row in rows:
            f = fresh.get(str(row["post_id"]))
            if f:
                file_url, thumb_url, ext = f["file_url"], f["thumb_url"], f["ext"]
            else:
                file_url  = row.get("file_url") or ""
                thumb_url = row.get("thumb_url") or file_url
                ext       = row.get("ext") or (file_url.rsplit(".", 1)[-1].lower() if "." in file_url else "")
            results.append({
                "id":         row["post_id"],
                "source":     row["source"],
                "post_url":   row["post_url"],
                "file_url":   file_url,
                "thumb_url":  thumb_url,
                "ext":        ext,
                "artists":    [row["artist_name"]] if row["artist_name"] else [],
                "tags":       [],
                "pool_id":    None,
                "pool_name":  None,
                "is_video":   bool(row["is_video"]),
                "in_library": False,
                "download_id": None,
                "is_favorite": True,
            })

        if results and source_id in all_sources():
            prefix = all_sources()[source_id].id_prefix
            post_ids = [f"{prefix}{r['id']}" for r in results]
            existing_map = _resolve_in_library(post_ids)
            for r in results:
                dl_id = existing_map.get(f"{prefix}{r['id']}")
                r["in_library"]  = dl_id is not None
                r["download_id"] = dl_id

        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _resolve_in_library(post_ids: list) -> dict:
    """
    Повертає {post_id: download_id} для переданих post_id (у форматі
    'e621_123'/'ib_123'). Враховує не тільки прямий збіг у downloads, а й
    пости, які зникли через об'єднання авторів (виявились візуальним
    дублікатом і були прибрані — merge_duplicate_log) — такі теж мають
    показуватись як 'вже в бібліотеці', вказуючи на файл, який лишили.
    """
    if not post_ids:
        return {}
    db2 = get_db()
    # Позначка «вже в бібліотеці» бере дані з архіватора furry. «Онлайн»
    # від нього не залежить: немає furry — просто нічого не позначаємо.
    if not table_exists(db2, "furry_posts"):
        db2.close()
        return {}
    placeholders = ",".join(["%s"] * len(post_ids))
    existing = qq(db2, f"SELECT post_id, file_id AS download_id FROM furry_posts WHERE post_id IN ({placeholders})", post_ids)
    existing_map = {row["post_id"]: row["download_id"] for row in existing}

    missing_ids = [pid for pid in post_ids if pid not in existing_map]
    if missing_ids:
        mp = ",".join(["%s"] * len(missing_ids))
        merged = qq(db2, f"""
            SELECT removed_post_id, kept_post_id FROM merge_duplicate_log
            WHERE removed_post_id IN ({mp})
        """, missing_ids)
        if merged:
            kept_ids = list({m["kept_post_id"] for m in merged})
            kp = ",".join(["%s"] * len(kept_ids))
            kept_rows = qq(db2, f"SELECT post_id, file_id AS download_id FROM furry_posts WHERE post_id IN ({kp})", kept_ids)
            kept_map = {row["post_id"]: row["download_id"] for row in kept_rows}
            for m in merged:
                dl_id = kept_map.get(m["kept_post_id"])
                if dl_id is not None:
                    existing_map[m["removed_post_id"]] = dl_id
    db2.close()
    return existing_map


@bp.route("/api/live/search")
def api_live_search():
    """
    'Живий' пошук напряму на джерело — без завантаження, без БД. Повністю
    джерело-агностичний: додаток нового джерела в live_sources/ підхопиться
    тут автоматично, без жодних змін цього коду.
    """
    source_id = flask_request.args.get("source", "e621")
    tags    = flask_request.args.get("tags", "").strip()
    exclude = flask_request.args.get("exclude", "").strip()
    artist  = flask_request.args.get("artist", "").strip()
    pool_id = flask_request.args.get("pool_id", None)
    sort    = flask_request.args.get("sort", "").strip()
    page    = int(flask_request.args.get("page", 1))
    limit   = min(int(flask_request.args.get("limit", 60)), 320)

    src = _get_source_instance(source_id, current_user_id())
    if not src:
        return jsonify({"error": "Невідоме джерело"}), 400
    if not src.is_configured():
        needed = ", ".join(f["label"] for f in src.credential_fields)
        return jsonify({"results": [], "error": f"Вкажи свої дані для {src.label} у налаштуваннях ({needed})"})

    try:
        results, pool_name = src.search(
            tags=tags, artist=artist, exclude=exclude, sort=sort,
            pool_id=pool_id, page=page, limit=limit,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    try:
        # Позначаємо ті пости, які вже є в нашій БД — фронтенд покаже їх
        # зі свого сховища замість зовнішнього посилання (включно з тими,
        # що зникли через об'єднання авторів — merge_duplicate_log)
        if results:
            prefix = src.id_prefix
            post_ids = [f"{prefix}{r['id']}" for r in results]
            existing_map = _resolve_in_library(post_ids)

            # Улюблене для джерел без власного API улюбленого — окрема
            # таблиця, не пов'язана з тим, чи робота вже завантажена
            fav_ids = set()
            if not src.supports_real_favorites:
                db2 = get_db()
                fav_placeholders = ",".join(["%s"] * len(results))
                fav_args = [r['id'] for r in results]
                favs = qq(db2, f"SELECT post_id FROM live_favorites WHERE source=%s AND post_id IN ({fav_placeholders})",
                          [source_id] + fav_args)
                db2.close()
                fav_ids = {row["post_id"] for row in favs}

            for r in results:
                dl_id = existing_map.get(f"{prefix}{r['id']}")
                r["in_library"]  = dl_id is not None
                r["download_id"] = dl_id
                if not src.supports_real_favorites:
                    r["is_favorite"] = r['id'] in fav_ids

        return jsonify({"results": results, "pool_name": pool_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────
#  Власні джерела: додати сайт без написання застосунку
# ─────────────────────────────────────────────────────────────────────

@bp.route("/api/live/custom-sources")
@admin_required
def api_custom_sources_list():
    from .custom import KINDS, list_custom
    return jsonify({"kinds": KINDS, "sources": list_custom()})


@bp.route("/api/live/custom-sources", methods=["POST"])
@admin_required
def api_custom_sources_save():
    from .custom import save_custom
    body = flask_request.get_json(silent=True) or {}
    try:
        save_custom(body.get("id", "").strip().lower(), body.get("label", "").strip(),
                    body.get("kind", ""), body.get("config") or {}, current_user_id())
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@bp.route("/api/live/custom-sources/<source_id>", methods=["DELETE"])
@admin_required
def api_custom_sources_delete(source_id):
    from .custom import delete_custom
    delete_custom(source_id)
    return jsonify({"ok": True})


@bp.route("/api/live/custom-sources/probe", methods=["POST"])
@admin_required
def api_custom_sources_probe():
    """Перевірити налаштування до збереження — один пробний запит."""
    from .custom import probe
    body = flask_request.get_json(silent=True) or {}
    return jsonify(probe(body.get("kind", ""), body.get("config") or {},
                         (body.get("query") or "").strip()))
