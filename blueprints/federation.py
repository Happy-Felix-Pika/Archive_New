"""Зовнішні архіви та доступ по ключу"""

import random, secrets

import os

from flask import Blueprint, Response, jsonify, request as flask_request, send_file


from blueprints.catalog import api_catalog_file, api_catalog_list, api_catalog_thumb
from blueprints.gallery import _gallery_ids_query, _resolve_tag_names, api_gallery_meta, api_gallery_stream
from core.auth import admin_required, current_user_id
from core.db import get_db, q, qq

bp = Blueprint("federation", __name__)


import hashlib as _keyhash

def _hash_access_key(raw_key: str) -> str:
    return _keyhash.sha256(raw_key.encode()).hexdigest()


def _validate_external_key(raw_key: str, identity: str = ""):
    """Перевіряє ключ доступу ПРОТИ НАШОЇ ВЛАСНОЇ бази (ми тут — хост, хтось
    інший стукається до НАС). Оновлює 'останній раз використано' відразу.
    Повертає (True, key_id) якщо дійсний, інакше (False, None) — key_id
    потрібен, щоб знати, які папки приховані САМЕ для цього ключа."""
    if not raw_key:
        return False, None
    key_hash = _hash_access_key(raw_key)
    try:
        db  = get_db()
        row = q(db, "SELECT id, is_active FROM external_access_keys WHERE key_hash=%s", (key_hash,))
        if not row or not row["is_active"]:
            db.close()
            return False, None
        with db.cursor() as cur:
            cur.execute("UPDATE external_access_keys SET last_used_at=NOW(), last_used_from=%s WHERE id=%s",
                        (identity or "невідомо", row["id"]))
        db.commit(); db.close()
        return True, row["id"]
    except Exception:
        return False, None


def get_hidden_folders_for_key(key_id: int) -> set:
    """Папки, які власник ключа приховав саме від цього конкретного ключа."""
    if not key_id:
        return set()
    db   = get_db()
    rows = qq(db, "SELECT folder_name FROM key_hidden_folders WHERE key_id=%s", (key_id,))
    db.close()
    return {r["folder_name"] for r in rows}


@bp.route("/api/external-access/gallery/ids")
def api_external_access_gallery_ids():
    """
    Публічний (але захищений ключем) доступ до списку файлів — для когось
    ІНШОГО, хто підключив ТВІЙ архів собі. Теги передаються НАЗВАМИ (не
    ID — вони різні в кожній базі), і резолвляться в наші власні tag_id
    прямо тут, перед тим як іти в спільну логіку фільтрації.
    """
    key = flask_request.headers.get("X-Access-Key", "")
    valid, key_id = _validate_external_key(key, flask_request.headers.get("X-Identity", ""))
    if not valid:
        return jsonify({"error": "Ключ доступу недійсний або деактивований"}), 403

    f         = flask_request.args.get("filter", "all")
    artist_id = flask_request.args.get("artist_id", None)
    subfolder = flask_request.args.get("subfolder", None)
    tag_ids      = _resolve_tag_names(flask_request.args.get("tag_names", ""))
    hide_tag_ids = _resolve_tag_names(flask_request.args.get("hide_tag_names", ""))
    include_post_id = flask_request.args.get("include_post_id", "") == "1"
    hidden = get_hidden_folders_for_key(key_id)
    return _gallery_ids_query(f, artist_id, subfolder, tag_ids, hide_tag_ids, False, 1, include_post_id, hidden,
                              include_file_ids=get_file_exceptions_for_key(key_id))



def _file_storage_folder(download_id):
    """
    Тека файлу у сховищі — те саме, за чим ховаються теки від зовнішніх
    ключів. Рахується зі шляху, а не з автора: теку без автора теж треба
    вміти закрити. Файл у корені = '_root'.
    """
    db  = get_db()
    row = q(db, "SELECT rel_path FROM files WHERE id = %s", (download_id,))
    db.close()
    if not row:
        return None
    rel = row["rel_path"]
    return rel.split("/", 1)[0] if "/" in rel else "_root"


def get_file_exceptions_for_key(key_id: int) -> set:
    """Файли з прихованих тек, які явно відкрито для цього ключа."""
    db = get_db()
    try:
        rows = qq(db, "SELECT file_id FROM key_file_exceptions WHERE key_id=%s", (key_id,))
        return {int(r["file_id"]) for r in rows}
    finally:
        db.close()


def key_can_see_file(key_id: int, download_id: int) -> bool:
    """
    ЄДИНЕ місце, яке вирішує, чи бачить ключ файл.

    Файл видно, якщо його тека не прихована, або якщо він явно відкритий
    для цього ключа як виняток. І список, і пряме посилання питають саме
    тут — щоб знову не вийшло, як колись: у списку файл сховано, а за
    прямим посиланням на id він віддається.
    """
    hidden = get_hidden_folders_for_key(key_id)
    if not hidden:
        return True
    if _file_storage_folder(download_id) not in hidden:
        return True
    return int(download_id) in get_file_exceptions_for_key(key_id)


@bp.route("/api/external-access/gallery/meta/<int:download_id>")
def api_external_access_gallery_meta(download_id):
    key = flask_request.headers.get("X-Access-Key", "")
    valid, key_id = _validate_external_key(key, flask_request.headers.get("X-Identity", ""))
    if not valid:
        return jsonify({"error": "Ключ доступу недійсний або деактивований"}), 403
    if not key_can_see_file(key_id, download_id):
        return jsonify({"error": "Немає доступу до цієї папки"}), 403
    return api_gallery_meta(download_id)


@bp.route("/api/external-access/gallery/stream/<int:download_id>")
def api_external_access_gallery_stream(download_id):
    # Ключ через query-параметр, не заголовок — бо це посилання йде прямо
    # в <img src>/<video src>, а їм заголовки не проставиш
    key = flask_request.args.get("key", "")
    valid, key_id = _validate_external_key(key, flask_request.args.get("identity", ""))
    if not valid:
        return jsonify({"error": "Ключ доступу недійсний або деактивований"}), 403
    if not key_can_see_file(key_id, download_id):
        return jsonify({"error": "Немає доступу до цієї папки"}), 403
    return api_gallery_stream(download_id)


@bp.route("/api/external-access/catalog/list")
def api_external_access_catalog_list():
    """Захищений ключем перегляд Каталогу — для когось ІНШОГО, хто
    підключив твій архів собі. Приховані папки визначаються тим, що
    власник ключа сам налаштував саме для ЦЬОГО ключа."""
    key = flask_request.headers.get("X-Access-Key", "")
    valid, key_id = _validate_external_key(key, flask_request.headers.get("X-Identity", ""))
    if not valid:
        return jsonify({"error": "Ключ доступу недійсний або деактивований"}), 403
    return api_catalog_list(hidden_folders_override=get_hidden_folders_for_key(key_id))


@bp.route("/api/external-access/catalog/file")
def api_external_access_catalog_file():
    key = flask_request.args.get("key", "")
    valid, key_id = _validate_external_key(key, flask_request.args.get("identity", ""))
    if not valid:
        return jsonify({"error": "Ключ доступу недійсний або деактивований"}), 403
    path = flask_request.args.get("path", "")
    hidden = get_hidden_folders_for_key(key_id)
    if hidden and path.split("/", 1)[0] in hidden:
        return jsonify({"error": f"Папка «{path.split('/', 1)[0]}» закрита для цього ключа",
                        "origin": "ключ доступу"}), 403
    return api_catalog_file(external=True)


@bp.route("/api/external-access/catalog/thumb")
def api_external_access_catalog_thumb():
    """
    Мініатюра файлу Каталогу для чужого архіву, який підключив нас.
    Перевірки ті самі, що й у catalog/file: ключ плюс папки, сховані
    саме для цього ключа. Без цього прев'ю було б каналом обходу —
    кадр з відео теж показує вміст.
    """
    key = flask_request.args.get("key", "")
    valid, key_id = _validate_external_key(key, flask_request.args.get("identity", ""))
    if not valid:
        return jsonify({"error": "Ключ доступу недійсний або деактивований"}), 403
    path = flask_request.args.get("path", "")
    hidden = get_hidden_folders_for_key(key_id)
    if hidden and path.split("/", 1)[0] in hidden:
        return jsonify({"error": f"Папка «{path.split('/', 1)[0]}» закрита для цього ключа",
                        "origin": "ключ доступу"}), 403
    return api_catalog_thumb(external=True)


@bp.route("/api/external-access/keys")
@admin_required
def api_external_keys_list():
    """Список усіх ключів доступу, виданих ДО твого архіву — з інформацією,
    хто і коли ним останній раз користувався."""
    try:
        db   = get_db()
        rows = qq(db, """
            SELECT id, label, is_active, created_at, last_used_at, last_used_from
            FROM external_access_keys ORDER BY created_at DESC
        """)
        db.close()
        for r in rows:
            if r["created_at"]:    r["created_at"]    = r["created_at"].isoformat()
            if r["last_used_at"]:  r["last_used_at"]  = r["last_used_at"].isoformat()
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/external-access/keys/<int:key_id>/folders")
@admin_required
def api_external_key_folders(key_id):
    """
    Список ВСІХ папок верхнього рівня (авторів) з позначкою, чи приховані
    вони саме для цього ключа. За замовчуванням видно все — приховані
    лише ті, які явно позначили.
    """
    try:
        db      = get_db()
        artists = qq(db, "SELECT DISTINCT folder_name FROM artists ORDER BY folder_name")
        hidden  = qq(db, "SELECT folder_name FROM key_hidden_folders WHERE key_id=%s", (key_id,))
        db.close()
        hidden_set = {r["folder_name"] for r in hidden}
        return jsonify([{"folder_name": a["folder_name"], "hidden": a["folder_name"] in hidden_set} for a in artists])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/external-access/keys/<int:key_id>/folders/toggle", methods=["POST"])
@admin_required
def api_external_key_folder_toggle(key_id):
    body        = flask_request.get_json(silent=True) or {}
    folder_name = (body.get("folder_name") or "").strip()
    hide        = bool(body.get("hide"))
    if not folder_name:
        return jsonify({"error": "folder_name required"}), 400
    try:
        db = get_db()
        with db.cursor() as cur:
            if hide:
                cur.execute("INSERT IGNORE INTO key_hidden_folders (key_id, folder_name) VALUES (%s,%s)",
                            (key_id, folder_name))
            else:
                cur.execute("DELETE FROM key_hidden_folders WHERE key_id=%s AND folder_name=%s",
                            (key_id, folder_name))
        db.commit(); db.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/external-access/keys/<int:key_id>/folders/bulk", methods=["POST"])
@admin_required
def api_external_key_folders_bulk(key_id):
    """Кнопки 'Показати все' / 'Приховати все' одразу для всіх папок."""
    body   = flask_request.get_json(silent=True) or {}
    action = body.get("action")  # 'show_all' | 'hide_all'
    try:
        db = get_db()
        with db.cursor() as cur:
            if action == "show_all":
                cur.execute("DELETE FROM key_hidden_folders WHERE key_id=%s", (key_id,))
            elif action == "hide_all":
                cur.execute("SELECT DISTINCT folder_name FROM artists")
                names = [r["folder_name"] for r in cur.fetchall()]
                cur.execute("DELETE FROM key_hidden_folders WHERE key_id=%s", (key_id,))
                for name in names:
                    cur.execute("INSERT IGNORE INTO key_hidden_folders (key_id, folder_name) VALUES (%s,%s)",
                                (key_id, name))
            else:
                db.close()
                return jsonify({"error": "Невідома дія"}), 400
        db.commit(); db.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/external-access/keys", methods=["POST"])
@admin_required
def api_external_keys_create():
    """Генерує новий ключ доступу — показується у відкритому вигляді ЛИШЕ
    цього разу (в БД зберігається тільки хеш, як і паролі)."""
    body  = flask_request.get_json(silent=True) or {}
    label = (body.get("label") or "").strip() or "Без назви"
    raw_key = secrets.token_urlsafe(24)
    key_hash = _hash_access_key(raw_key)
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("INSERT INTO external_access_keys (key_hash, label) VALUES (%s,%s)", (key_hash, label))
        db.commit(); db.close()
        return jsonify({"ok": True, "key": raw_key})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/external-access/keys/<int:key_id>/toggle", methods=["POST"])
@admin_required
def api_external_keys_toggle(key_id):
    body    = flask_request.get_json(silent=True) or {}
    enabled = 1 if body.get("enabled") else 0
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("UPDATE external_access_keys SET is_active=%s WHERE id=%s", (enabled, key_id))
        db.commit(); db.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/external-access/keys/<int:key_id>", methods=["DELETE"])
@admin_required
def api_external_keys_delete(key_id):
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("DELETE FROM external_access_keys WHERE id=%s", (key_id,))
        db.commit(); db.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def get_external_archives(user_id: int) -> list:
    """
    Список зовнішніх архівів, ЯКІ ЦЕЙ юзер МОЖЕ бачити — або тому, що сам
    його підключив, або тому, що власник підключення явно дав йому доступ
    (external_archive_access, де власник теж завжди має свій рядок). Поки
    доступ не дано — юзер архіву не бачить ВЗАГАЛІ, навіть у списку.
    """
    db = get_db()
    rows = qq(db, """
        SELECT ea.*, eaa.include_in_combined FROM external_archives ea
        JOIN external_archive_access eaa ON eaa.archive_id = ea.id AND eaa.user_id = %s
        ORDER BY ea.created_at DESC
    """, (user_id,))
    db.close()
    for r in rows:
        if r.get("created_at"): r["created_at"] = r["created_at"].isoformat()
    return rows


def add_external_archive(user_id: int, cfg: dict) -> dict:
    cfg["id"] = secrets.token_hex(8)
    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO external_archives (id, added_by_user_id, label, base_url, access_key, my_identity)
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (cfg["id"], user_id, cfg["label"], cfg["base_url"], cfg["access_key"], cfg.get("my_identity", "")))
        # Той, хто підключив — автоматично бачить сам, без окремого дозволу
        cur.execute("INSERT IGNORE INTO external_archive_access (archive_id, user_id) VALUES (%s,%s)",
                    (cfg["id"], user_id))
    db.commit(); db.close()
    cfg["added_by_user_id"] = user_id
    return cfg


def delete_external_archive(user_id: int, archive_id: str):
    """Видалити підключення може тільки той, хто його додав."""
    db = get_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM external_archives WHERE id=%s AND added_by_user_id=%s", (archive_id, user_id))
    db.commit(); db.close()


def call_external_api(cfg: dict, path: str, stream: bool = False, params: dict = None):
    """
    Замість прямого підключення до чужої БД/Nextcloud — б'ємо просто в
    ЙОГО ВЛАСНИЙ дашборд по HTTP, з ключем у заголовку. Йому вже байдуже,
    локальне сховище в нього чи Nextcloud — його ж /api/gallery/* і так
    це вміє, ми просто проксуємо крізь ключ доступу.
    """
    import requests as req
    base = cfg.get("base_url", "").rstrip("/")
    if not base:
        return None, "Не вказано адресу архіву"
    try:
        r = req.get(f"{base}{path}", headers={
            "X-Access-Key": cfg.get("access_key", ""),
            "X-Identity":   cfg.get("my_identity", ""),
        }, params=params, timeout=15, stream=stream)
        if r.status_code == 403:
            return None, "Ключ доступу недійсний або деактивований власником архіву"
        r.raise_for_status()
        return r, None
    except req.exceptions.RequestException as e:
        return None, f"Не вдалося звернутись до архіву: {e}"


@bp.route("/api/external-archives")
def api_external_archives_list():
    """Список підключених зовнішніх архівів — без чутливих полів (пароль)."""
    archives = get_external_archives(current_user_id())
    safe = [{k: v for k, v in a.items() if k not in ("db_pass", "access_key")} for a in archives]
    return jsonify(safe)


@bp.route("/api/external-archives", methods=["POST"])
@admin_required
def api_external_archives_add():
    body = flask_request.get_json(silent=True) or {}
    required = ("label", "base_url", "access_key")
    if not all(body.get(k) for k in required):
        return jsonify({"error": "Заповни всі поля"}), 400
    base_url = body["base_url"].strip().rstrip("/")

    # Ключ доступу летить у заголовку при КОЖНОМУ запиті — якщо адреса не
    # https, він передається мережею у відкритому вигляді. Дозволяємо http
    # тільки для локальних/приватних адрес (тестування в одній мережі),
    # для будь-чого зовнішнього — вимагаємо https.
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    is_local = host in ("localhost", "127.0.0.1") or host.startswith(("192.168.", "10.")) or host.endswith(".local")
    if parsed.scheme != "https" and not is_local:
        return jsonify({"error": "Адреса має бути https:// — інакше ключ доступу передавався б мережею у відкритому вигляді. http:// дозволено лише для localhost/локальної мережі."}), 400

    cfg = {
        "label":       body["label"].strip(),
        "base_url":    base_url,
        "access_key":  body["access_key"].strip(),
        "my_identity": body.get("my_identity", "").strip(),
    }
    resp, err = call_external_api(cfg, "/api/external-access/gallery/ids")
    if err:
        return jsonify({"error": err}), 400
    saved = add_external_archive(current_user_id(), cfg)
    return jsonify({"ok": True, "id": saved["id"]})


@bp.route("/api/external-archives/<archive_id>", methods=["DELETE"])
@admin_required
def api_external_archives_delete(archive_id):
    delete_external_archive(current_user_id(), archive_id)
    return jsonify({"ok": True})


@bp.route("/api/external-archives/<archive_id>/status")
def api_external_archives_status(archive_id):
    cfg = _get_external_cfg_or_404(archive_id)
    if not cfg:
        return jsonify({"error": "Архів не знайдено"}), 404
    resp, err = call_external_api(cfg, "/api/external-access/gallery/ids")
    if err:
        return jsonify({"active": False, "error": err})
    return jsonify({"active": True})


def _get_external_cfg_or_404(archive_id):
    archives = get_external_archives(current_user_id())
    return next((a for a in archives if a["id"] == archive_id), None)


@bp.route("/api/external-archives/<archive_id>/access")
@admin_required
def api_external_archive_access_list(archive_id):
    """Хто зараз бачить цей архів — тільки власник підключення може дивитись."""
    db  = get_db()
    row = q(db, "SELECT added_by_user_id FROM external_archives WHERE id=%s", (archive_id,))
    if not row or row["added_by_user_id"] != current_user_id():
        db.close()
        return jsonify({"error": "Тільки той, хто підключив архів, може керувати доступом"}), 403
    granted = qq(db, "SELECT user_id FROM external_archive_access WHERE archive_id=%s", (archive_id,))
    db.close()
    return jsonify({"granted_user_ids": [r["user_id"] for r in granted]})


@bp.route("/api/external-archives/<archive_id>/access", methods=["POST"])
@admin_required
def api_external_archive_access_grant(archive_id):
    body    = flask_request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    db  = get_db()
    row = q(db, "SELECT added_by_user_id FROM external_archives WHERE id=%s", (archive_id,))
    if not row or row["added_by_user_id"] != current_user_id():
        db.close()
        return jsonify({"error": "Тільки той, хто підключив архів, може керувати доступом"}), 403
    with db.cursor() as cur:
        cur.execute("INSERT IGNORE INTO external_archive_access (archive_id, user_id) VALUES (%s,%s)",
                    (archive_id, user_id))
    db.commit(); db.close()
    return jsonify({"ok": True})


@bp.route("/api/external-archives/<archive_id>/access/<int:user_id>", methods=["DELETE"])
@admin_required
def api_external_archive_access_revoke(archive_id, user_id):
    db  = get_db()
    row = q(db, "SELECT added_by_user_id FROM external_archives WHERE id=%s", (archive_id,))
    if not row or row["added_by_user_id"] != current_user_id():
        db.close()
        return jsonify({"error": "Тільки той, хто підключив архів, може керувати доступом"}), 403
    if user_id == row["added_by_user_id"]:
        db.close()
        return jsonify({"error": "Не можна забрати доступ у того, хто підключив архів"}), 400
    with db.cursor() as cur:
        cur.execute("DELETE FROM external_archive_access WHERE archive_id=%s AND user_id=%s", (archive_id, user_id))
    db.commit(); db.close()
    return jsonify({"ok": True})


@bp.route("/api/external-archives/<archive_id>/toggle-combined", methods=["POST"])
def api_external_archive_toggle_combined(archive_id):
    """
    Персонально для ПОТОЧНОГО юзера — включати цей архів у 'Усі разом' чи
    ні. Той самий архів може бути виключений в одного юзера й увімкнений
    в іншого, це не глобальна настройка самого підключення.
    """
    body    = flask_request.get_json(silent=True) or {}
    enabled = 1 if body.get("enabled") else 0
    db = get_db()
    with db.cursor() as cur:
        cur.execute("""
            UPDATE external_archive_access SET include_in_combined=%s
            WHERE archive_id=%s AND user_id=%s
        """, (enabled, archive_id, current_user_id()))
    db.commit(); db.close()
    return jsonify({"ok": True})


@bp.route("/api/external-archives/<archive_id>/gallery/ids")
def api_external_gallery_ids(archive_id):
    cfg = _get_external_cfg_or_404(archive_id)
    if not cfg:
        return jsonify({"error": "Архів не знайдено"}), 404
    params = {
        "tag_names":      flask_request.args.get("tag_names", ""),
        "hide_tag_names": flask_request.args.get("hide_tag_names", ""),
        # Чужий сервер (і старий, і новий) ці параметри вже приймає — раніше
        # ми їх просто не передавали, тому все й ішло купою.
        "filter":         flask_request.args.get("filter", "all"),
    }
    for k in ("artist_id", "subfolder"):
        v = flask_request.args.get(k)
        if v:
            params[k] = v
    resp, err = call_external_api(cfg, "/api/external-access/gallery/ids", params=params)
    if err:
        return jsonify({"error": err}), 403
    return jsonify(resp.json())


@bp.route("/api/external-archives/<archive_id>/gallery/meta/<int:remote_id>")
def api_external_gallery_meta(archive_id, remote_id):
    cfg = _get_external_cfg_or_404(archive_id)
    if not cfg:
        return jsonify({"error": "Архів не знайдено"}), 404
    resp, err = call_external_api(cfg, f"/api/external-access/gallery/meta/{remote_id}")
    if err:
        return jsonify({"error": err}), 403
    data = resp.json()
    # Підмінюємо на НАШ проксі-URL, щоб фронтенд не мав напряму звертатись
    # до чужого сервера (і щоб ключ доступу лишався тільки на бекенді)
    data["stream_url"] = f"/api/external-archives/{archive_id}/gallery/stream/{remote_id}"
    return jsonify(data)


@bp.route("/api/external-archives/<archive_id>/gallery/stream/<int:remote_id>")
def api_external_gallery_stream(archive_id, remote_id):
    """
    Проксує реальні байти файлу через ключ доступу — працює НЕЗАЛЕЖНО від
    того, яке сховище в господаря архіву (Nextcloud чи локальне), бо він
    сам це вже знає у СВОЄМУ /api/external-access/gallery/stream.
    """
    cfg = _get_external_cfg_or_404(archive_id)
    if not cfg:
        return jsonify({"error": "Архів не знайдено"}), 404
    import requests as req
    base = cfg.get("base_url", "").rstrip("/")
    try:
        upstream_headers = {
            "X-Access-Key": cfg.get("access_key", ""),
            "X-Identity":   cfg.get("my_identity", ""),
        }
        # Передаємо Range далі — саме через нього браузер перемотує відео
        # (просить конкретний діапазон байтів). Без цього проксі завжди
        # тягнуло файл із самого початку, і перемотка на []  не працювала.
        range_header = flask_request.headers.get("Range")
        if range_header:
            upstream_headers["Range"] = range_header
        r = req.get(f"{base}/api/external-access/gallery/stream/{remote_id}",
                    params={"key": cfg.get("access_key", ""), "identity": cfg.get("my_identity", "")},
                    headers=upstream_headers, stream=True, timeout=30)
        if r.status_code == 403:
            return jsonify({"error": "Ключ доступу недійсний або деактивований"}), 403
        r.raise_for_status()
        mime = r.headers.get("Content-Type", "application/octet-stream")

        def gen():
            for chunk in r.iter_content(chunk_size=131072):
                if chunk: yield chunk

        resp_headers = {"Content-Type": mime, "Cache-Control": "no-store", "Accept-Ranges": "bytes"}
        for h in ("Content-Range", "Content-Length"):
            if h in r.headers:
                resp_headers[h] = r.headers[h]
        return Response(gen(), status=r.status_code, headers=resp_headers)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/external-archives/<archive_id>/catalog/list")
def api_external_catalog_list(archive_id):
    cfg = _get_external_cfg_or_404(archive_id)
    if not cfg:
        return jsonify({"error": "Архів не знайдено"}), 404
    path = flask_request.args.get("path", "")
    resp, err = call_external_api(cfg, "/api/external-access/catalog/list", params={"path": path})
    if err:
        return jsonify({"error": err}), 403
    return jsonify(resp.json())


@bp.route("/api/external-archives/<archive_id>/catalog/file")
def api_external_catalog_file(archive_id):
    """Проксує реальні байти файлу з Каталогу зовнішнього архіву — з
    підтримкою Range для перемотки відео/аудіо, так само як і gallery/stream."""
    cfg = _get_external_cfg_or_404(archive_id)
    if not cfg:
        return jsonify({"error": "Архів не знайдено"}), 404
    path = flask_request.args.get("path", "")
    import requests as req
    base = cfg.get("base_url", "").rstrip("/")
    try:
        upstream_headers = {}
        range_header = flask_request.headers.get("Range")
        if range_header:
            upstream_headers["Range"] = range_header
        r = req.get(f"{base}/api/external-access/catalog/file",
                    params={"path": path, "key": cfg.get("access_key", ""), "identity": cfg.get("my_identity", "")},
                    headers=upstream_headers, stream=True, timeout=30)
        if r.status_code == 403:
            return jsonify({"error": "Ключ доступу недійсний або деактивований"}), 403
        r.raise_for_status()
        mime = r.headers.get("Content-Type", "application/octet-stream")

        def gen():
            for chunk in r.iter_content(chunk_size=131072):
                if chunk: yield chunk

        resp_headers = {"Content-Type": mime, "Cache-Control": "no-store", "Accept-Ranges": "bytes"}
        for h in ("Content-Range", "Content-Length"):
            if h in r.headers:
                resp_headers[h] = r.headers[h]
        return Response(gen(), status=r.status_code, headers=resp_headers)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/external-archives/<archive_id>/catalog/thumb")
def api_external_catalog_thumb(archive_id):
    """
    Мініатюра з Каталогу підключеного архіву.

    Віддається як є, без Range: це маленький JPEG, а не потік. Якщо на
    тому боці стоїть стара версія без цього ендпоінта, повертається 404
    і фронт сам відкочується на повний файл (для картинок) або іконку
    (для відео) — саме для цього там обробник onerror.
    """
    cfg = _get_external_cfg_or_404(archive_id)
    if not cfg:
        return jsonify({"error": "Архів не знайдено"}), 404
    path = flask_request.args.get("path", "")
    import requests as req
    base = cfg.get("base_url", "").rstrip("/")
    try:
        params = {"path": path, "key": cfg.get("access_key", ""),
                  "identity": cfg.get("my_identity", "")}
        # ?debug=1 проходить наскрізь: інакше причину, чому прев'ю немає
        # саме на ЧУЖОМУ файлі, довелось би з'ясовувати з його ключем у руках.
        if flask_request.args.get("debug") == "1":
            params["debug"] = "1"
        r = req.get(f"{base}/api/external-access/catalog/thumb",
                    params=params, stream=True, timeout=40)
        if r.status_code == 403:
            # Пояснення беремо з того боку, а не вигадуємо своє: причин
            # рівно дві — недійсний ключ або папка, закрита для цього
            # ключа, — і плутати їх при розборі дорого.
            try:
                why = (r.json() or {}).get("error") or "доступ закрито"
            except Exception:
                why = "доступ закрито"
            return jsonify({"error": why, "origin": "віддалений архів"}), 403
        if r.status_code == 200:
            def gen():
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk: yield chunk
            return Response(gen(), status=200, headers={
                "Content-Type":  r.headers.get("Content-Type", "image/jpeg"),
                "Cache-Control": "public, max-age=86400",
            })
    except Exception as e:
        if flask_request.args.get("debug") == "1":
            return jsonify({"error": f"архів недоступний: {e}"}), 404
        pass   # той бік недоступний або не вміє — пробуємо зробити самі

    if flask_request.args.get("debug") == "1":
        return jsonify({"error": "той бік не віддав мініатюру і не пояснив причину"}), 404
    return _thumb_from_remote_original(cfg, path)


def _thumb_from_remote_original(cfg, path):
    """
    Запасний шлях: підключений архів мініатюру не віддав (стара версія,
    формат якого він не знає, або в нього не вийшов кадр). Робимо її
    самі з оригіналу, який той архів віддати вміє — ендпоінт
    catalog/file є навіть у найстарших збірках.

    Відео НЕ качаємо: ffmpeg читає той самий URL по HTTP і забирає лише
    потрібні шматки. Картинку качаємо, вона маленька.

    Результат кешуємо на нашому боці, тож платимо за це один раз.
    """
    from blueprints.catalog import (_THUMB_IMAGE_EXT, _THUMB_VIDEO_EXT,
                                    _make_image_thumb, _make_video_thumb,
                                    _thumb_cache_path)
    name = path.rsplit("/", 1)[-1]
    ext  = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in _THUMB_IMAGE_EXT and ext not in _THUMB_VIDEO_EXT:
        return jsonify({"error": "Мініатюра недоступна"}), 404

    base = cfg.get("base_url", "").rstrip("/")
    from urllib.parse import urlencode
    qs  = urlencode({"path": path, "key": cfg.get("access_key", ""),
                     "identity": cfg.get("my_identity", "")})
    url = f"{base}/api/external-access/catalog/file?{qs}"

    out = _thumb_cache_path(f"{base}|{path}", "remote")
    if os.path.exists(out):
        return send_file(out, mimetype="image/jpeg", conditional=True, max_age=86400)

    if ext in _THUMB_VIDEO_EXT:
        if not _make_video_thumb(url, out):
            return jsonify({"error": "Мініатюра недоступна"}), 404
        return send_file(out, mimetype="image/jpeg", conditional=True, max_age=86400)

    import requests as req, tempfile
    tmp = None
    try:
        rr = req.get(url, stream=True, timeout=40)
        rr.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as fh:
            tmp = fh.name
            for chunk in rr.iter_content(65536):
                if chunk:
                    fh.write(chunk)
        if not _make_image_thumb(tmp, out):
            return jsonify({"error": "Мініатюра недоступна"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 404
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return send_file(out, mimetype="image/jpeg", conditional=True, max_age=86400)


@bp.route("/api/external-archives/<archive_id>/favorite", methods=["POST"])
def api_external_favorite_toggle(archive_id):
    cfg = _get_external_cfg_or_404(archive_id)
    if not cfg:
        return jsonify({"error": "Архів не знайдено"}), 404
    # Перевіряємо, що ключ ще дійсний, ПЕРЕД тим, як зберігати фаворит у себе
    resp, err = call_external_api(cfg, "/api/external-access/gallery/ids")
    if err:
        return jsonify({"error": err}), 403

    body = flask_request.get_json(silent=True) or {}
    remote_id = body.get("download_id")
    if not remote_id:
        return jsonify({"error": "download_id required"}), 400
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("""
                SELECT id FROM external_favorites WHERE user_id=%s AND archive_id=%s AND remote_download_id=%s
            """, (current_user_id(), archive_id, remote_id))
            existing = cur.fetchone()
            if existing:
                cur.execute("DELETE FROM external_favorites WHERE id=%s", (existing["id"],))
                is_fav = False
            else:
                cur.execute("""
                    INSERT INTO external_favorites (user_id, archive_id, remote_download_id) VALUES (%s,%s,%s)
                """, (current_user_id(), archive_id, remote_id))
                is_fav = True
        db.commit(); db.close()
        return jsonify({"is_favorite": is_fav})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/external-favorites")
def api_external_favorites_list():
    """
    Улюблене з усіх підключених зовнішніх архівів — але ТІЛЬКИ тих, чий
    ключ ЗАРАЗ активний. Для архіву з деактивованим ключем — не показуємо
    взагалі нічого (ні назви, ні кількості), він просто мовчки пропускається.
    """
    user_id  = current_user_id()
    db       = get_db()
    fav_rows = qq(db, "SELECT archive_id, remote_download_id FROM external_favorites WHERE user_id=%s", (user_id,))
    db.close()

    by_archive = {}
    for r in fav_rows:
        by_archive.setdefault(r["archive_id"], []).append(r["remote_download_id"])

    archives = {a["id"]: a for a in get_external_archives(user_id)}
    results = []
    for archive_id, remote_ids in by_archive.items():
        cfg = archives.get(archive_id)
        if not cfg:
            continue  # архів видалено з підключень — пропускаємо мовчки
        for remote_id in remote_ids:
            resp, err = call_external_api(cfg, f"/api/external-access/gallery/meta/{remote_id}")
            if err:
                # Ключ неактивний/архів недоступний — пропускаємо мовчки, як
                # і просив: жодного натяку, що там щось було
                break
            row = resp.json()
            if row.get("error"):
                continue
            results.append({
                "archive_id": archive_id, "archive_label": cfg["label"],
                "id": row["id"], "filename": row["filename"], "file_ext": row["file_ext"],
                "size_bytes": row.get("size_bytes"), "artist": row.get("artist"),
                "stream_url": f"/api/external-archives/{archive_id}/gallery/stream/{remote_id}",
            })
    return jsonify(results)


@bp.route("/api/gallery/ids-combined")
def api_gallery_ids_combined():
    """
    'Усі разом' — локальна бібліотека + всі зовнішні архіви, до яких є
    доступ, перемішані в одну купу. Без штучних обмежень — так само, як і
    звичайна бібліотека (тягнемо все, перемішуємо на Python, а не в SQL).
    Теги: локальна частина фільтрується по звичайних tag_id, а для
    зовнішніх архівів ті самі обрані теги передаються НАЗВАМИ (їхні ID
    все одно інші в чужій базі).

    Дедублікація: якщо той самий реальний пост (той самий post_id, типу
    "e621_123456") є одразу в кількох архівах — бо кілька людей завантажили
    собі одну й ту саму роботу — показуємо його лише ОДИН раз, а не по
    копії з кожного архіву. post_id — надійний ключ для цього, бо він
    формується з реального ID поста на e621/Inkbunny, тому однаковий
    незалежно від того, у чиїй базі лежить.
    """
    user_id      = current_user_id()
    tag_ids      = flask_request.args.getlist("tag_id")
    hide_tag_ids = flask_request.args.getlist("hide_tag_id")
    tag_names_param      = flask_request.args.get("tag_names", "")
    hide_tag_names_param = flask_request.args.get("hide_tag_names", "")
    combined      = []
    seen_post_ids = set()

    db = get_db()
    where = ["f.name IS NOT NULL"]
    args  = []
    for tid in tag_ids:
        where.append("EXISTS (SELECT 1 FROM download_tags dt WHERE dt.download_id=f.id AND dt.tag_id=%s)")
        args.append(int(tid))
    for tid in hide_tag_ids:
        where.append("NOT EXISTS (SELECT 1 FROM download_tags dt WHERE dt.download_id=f.id AND dt.tag_id=%s)")
        args.append(int(tid))
    local_rows = qq(db, f"""SELECT f.id, fp.post_id FROM files f
                            LEFT JOIN furry_posts fp ON fp.file_id = f.id
                            WHERE {' AND '.join(where)}""", args)
    db.close()
    for r in local_rows:
        if r["post_id"]:
            seen_post_ids.add(r["post_id"])
        combined.append({"source": "local", "id": r["id"]})

    ext_params = {"tag_names": tag_names_param, "hide_tag_names": hide_tag_names_param, "include_post_id": "1"}
    for a in get_external_archives(user_id):
        if not a.get("include_in_combined", True):
            continue  # цей юзер сам виключив цей архів з "Усі разом"
        resp, err = call_external_api(a, "/api/external-access/gallery/ids", params=ext_params)
        if err:
            continue  # архів недоступний/ключ деактивовано — просто пропускаємо
        for item in (resp.json().get("ids") or []):
            # З include_post_id=1 хост повертає {"id":.., "post_id":..}
            # замість голих ID — саме post_id і використовуємо для дедублікації
            rid = item.get("id") if isinstance(item, dict) else item
            pid = item.get("post_id") if isinstance(item, dict) else None
            if pid:
                if pid in seen_post_ids:
                    continue  # той самий реальний пост уже є з іншого джерела
                seen_post_ids.add(pid)
            combined.append({"source": a["id"], "id": rid})

    random.shuffle(combined)
    return jsonify({"items": combined})


@bp.route("/api/external-access/keys/<int:key_id>/exceptions", methods=["GET"])
@admin_required
def api_key_exceptions_list(key_id):
    db = get_db()
    try:
        rows = qq(db, """SELECT e.file_id, f.name, f.rel_path, e.created_at
                         FROM key_file_exceptions e JOIN files f ON f.id = e.file_id
                         WHERE e.key_id=%s ORDER BY e.created_at DESC""", (key_id,))
        for r in rows:
            if r.get("created_at"):
                r["created_at"] = r["created_at"].isoformat()
        return jsonify(rows)
    finally:
        db.close()


@bp.route("/api/external-access/keys/<int:key_id>/exceptions/<int:file_id>", methods=["POST", "DELETE"])
@admin_required
def api_key_exception_toggle(key_id, file_id):
    """POST — відкрити файл для ключа, DELETE — знову закрити."""
    db = get_db()
    try:
        with db.cursor() as cur:
            if flask_request.method == "POST":
                cur.execute("INSERT IGNORE INTO key_file_exceptions (key_id, file_id) VALUES (%s,%s)",
                            (key_id, file_id))
            else:
                cur.execute("DELETE FROM key_file_exceptions WHERE key_id=%s AND file_id=%s",
                            (key_id, file_id))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@bp.route("/api/files/<int:file_id>/key-access")
@admin_required
def api_file_key_access(file_id):
    """Для картки файлу: які ключі бачать цей файл і чому."""
    db = get_db()
    try:
        keys = qq(db, "SELECT id, label, is_active FROM external_access_keys ORDER BY id")
    finally:
        db.close()
    folder = _file_storage_folder(file_id)
    out = []
    for k in keys:
        out.append({
            "key_id":        k["id"],
            "label":         k["label"],
            "active":        bool(k["is_active"]),
            "folder_hidden": folder in get_hidden_folders_for_key(k["id"]),
            "exception":     file_id in get_file_exceptions_for_key(k["id"]),
            "visible":       key_can_see_file(k["id"], file_id),
        })
    return jsonify({"folder": folder, "keys": out})
