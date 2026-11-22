"""Налаштування архіватора"""

import json

from flask import Blueprint, jsonify, request as flask_request


from core.auth import admin_required, get_user_settings, save_user_settings
from core.config import load_config, save_config

bp = Blueprint("settings_api", __name__)


# ── Config ────────────────────────────────────────────────────────────────

@bp.route("/api/config", methods=["GET"])
@admin_required
def api_config_get():
    """
    Застарілий ендпоінт — лишений для зворотної сумісності фронтенду.
    'database' (підключення до основної БД) береться з archiver_config.json/env,
    решта секцій (e621, inkbunny, download, mail…) — з персональних
    налаштувань поточного юзера в БД (/api/user-settings).
    """
    file_cfg = load_config()
    safe = {"database": dict(file_cfg.get("database", {}))}
    if safe["database"].get("password"):
        safe["database"]["password"] = "••••••••"

    user_settings = get_user_settings()
    safe.update(json.loads(json.dumps(user_settings)))
    safe["ui"] = {"title": (file_cfg.get("ui") or {}).get("title") or "Archive"}
    for s in ("inkbunny", "mail"):
        if s in safe and safe[s].get("password"):
            safe[s]["password"] = "••••••••"
    if "e621" in safe and safe["e621"].get("api_key"):
        safe["e621"]["api_key"] = "••••••••"
    return jsonify(safe)

@bp.route("/api/config", methods=["POST"])
@admin_required
def api_config_save():
    """
    Застарілий ендпоінт — лишений для зворотної сумісності.
    Секція 'database' йде у файл (вона потрібна до того як БД доступна),
    решта — делегується в персональні налаштування юзера.
    """
    data = flask_request.get_json(silent=True) or {}

    if "database" in data and isinstance(data["database"], dict):
        file_cfg = load_config()
        db_section = file_cfg.get("database", {})
        new_db = data["database"]
        if not new_db.get("password") or new_db.get("password") == "••••••••":
            new_db["password"] = db_section.get("password", "")
        file_cfg["database"] = new_db
        save_config(file_cfg)

    # Назва архіву — спільна для всіх і потрібна ще до входу (екран логіну),
    # тому, як і база, живе у файлі, а не в особистих налаштуваннях.
    if "ui" in data and isinstance(data["ui"], dict):
        file_cfg = load_config()
        ui = file_cfg.get("ui", {})
        title = (data["ui"].get("title") or "").strip()[:60]
        ui["title"] = title or "Archive"
        file_cfg["ui"] = ui
        save_config(file_cfg)

    rest = {k: v for k, v in data.items() if k not in ("database", "ui")}
    if rest:
        current = get_user_settings()
        SECRET_KEYS = {"password", "api_key"}
        MASKED      = "••••••••"

        def merge(base, new):
            for k, v in new.items():
                if isinstance(v, dict):
                    if k not in base or not isinstance(base[k], dict):
                        base[k] = {}
                    merge(base[k], v)
                elif k in SECRET_KEYS:
                    if v and v != MASKED:
                        base[k] = v
                else:
                    if v is not None and v != "" and v != MASKED:
                        base[k] = v

        merge(current, rest)
        save_user_settings(current)

    return jsonify({"ok": True})


# ── Process ───────────────────────────────────────────────────────────────



@bp.route("/api/branding")
def api_branding():
    """
    Назва архіву для інтерфейсу. Публічна: екран входу показує її ще до
    того, як користувач залогінився. Нічого секретного тут немає.
    """
    try:
        title = (load_config().get("ui") or {}).get("title") or "Archive"
    except Exception:
        title = "Archive"
    return jsonify({"title": title})
