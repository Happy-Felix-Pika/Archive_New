"""
Nextcloud — окремий застосунок. Ядро архіву з Nextcloud не працює: файли
лежать у локальній теці. Тут лише зберігаються параметри підключення
(секції «nextcloud» і «nextcloud_db» у archiver_config.json) і є перевірка,
що з ними сервер Nextcloud відповідає.

Адреси: /api/apps/nextcloud/...
"""

from flask import Blueprint, jsonify, request

from core.auth import admin_required, get_user_settings, merge_user_settings

bp = Blueprint("nextcloud", __name__)
core = None

MASK = "••••••••"
FIELDS = {
    "nextcloud":    ("webdav_url", "username", "password", "base_folder", "share_token", "local_data_path"),
    "nextcloud_db": ("host", "port", "user", "password", "database"),
}


def _settings():
    s = get_user_settings()
    return {sec: dict(s.get(sec) or {}) for sec in FIELDS}


@bp.route("/settings", methods=["GET"])
@admin_required
def get_settings():
    out = {}
    for sec, data in _settings().items():
        row = {k: data.get(k, "") for k in FIELDS[sec]}
        row["has_password"] = bool(data.get("password"))
        row["password"] = ""
        out[sec] = row
    return jsonify(out)


@bp.route("/settings", methods=["POST"])
@admin_required
def save_settings():
    body = request.get_json(silent=True) or {}
    cur = _settings()
    patch = {}
    for sec, keys in FIELDS.items():
        src = body.get(sec)
        if not isinstance(src, dict):
            continue
        row = {}
        for k in keys:
            if k not in src:
                continue
            v = src[k]
            if k == "password" and (v in ("", None, MASK)):
                continue                       # порожній пароль — лишаємо старий
            if k == "port":
                try:
                    v = int(v) if str(v).strip() else ""
                except ValueError:
                    return jsonify({"error": "Порт має бути числом"}), 400
            elif isinstance(v, str):
                v = v.strip()
            row[k] = v
        if src.get("clear_password"):
            row["password"] = ""
        patch[sec] = {**cur.get(sec, {}), **row}
    merge_user_settings(patch)
    return jsonify({"ok": True})


def _test_webdav(nc):
    import requests
    url = (nc.get("webdav_url") or "").strip()
    if not url:
        return {"ok": False, "msg": "Не вказано адресу WebDAV"}
    target = url.rstrip("/") + "/"
    base = (nc.get("base_folder") or "").strip("/")
    if base:
        target += requests.utils.quote(base) + "/"
    try:
        r = requests.request("PROPFIND", target, headers={"Depth": "0"},
                             auth=(nc.get("username") or "", nc.get("password") or ""),
                             timeout=15)
    except Exception as e:
        return {"ok": False, "msg": f"Сервер не відповідає: {e}"}
    if r.status_code in (200, 207):
        return {"ok": True, "msg": "Підключення працює" + (f", тека «{base}» є" if base else "")}
    if r.status_code == 401:
        return {"ok": False, "msg": "Невірний логін або пароль (401)"}
    if r.status_code == 404:
        return {"ok": False, "msg": "Адресу або теку не знайдено (404)"}
    return {"ok": False, "msg": f"Сервер відповів кодом {r.status_code}"}


def _test_db(db):
    if not (db.get("host") and db.get("database")):
        return None
    import pymysql
    try:
        conn = pymysql.connect(host=db["host"], port=int(db.get("port") or 3306),
                               user=db.get("user") or "", password=db.get("password") or "",
                               database=db["database"], connect_timeout=8)
        conn.close()
        return {"ok": True, "msg": "База Nextcloud доступна"}
    except Exception as e:
        return {"ok": False, "msg": f"База Nextcloud недоступна: {e}"}


@bp.route("/test", methods=["POST"])
@admin_required
def test():
    s = _settings()
    return jsonify({"webdav": _test_webdav(s["nextcloud"]), "db": _test_db(s["nextcloud_db"])})


def register(core_api):
    global core
    core = core_api
    core.api.add_routes(bp)
