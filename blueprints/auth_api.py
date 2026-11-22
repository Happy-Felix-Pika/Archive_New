"""Авторизація, користувачі, коди допуску, налаштування БД"""

import json, os, secrets

import pymysql, pymysql.cursors
from flask import Blueprint, jsonify, request as flask_request, session
from werkzeug.security import check_password_hash, generate_password_hash


from core.auth import admin_required, any_users_exist, current_user, get_user_settings, merge_user_settings
from core.config import load_config, save_config
from core.db import get_db, q, qq
from core.db_schema import ensure_tables

bp = Blueprint("auth_api", __name__)


@bp.route("/api/auth/register", methods=["POST"])
def api_auth_register():
    body   = flask_request.get_json(silent=True) or {}
    email  = (body.get("email") or "").strip().lower()
    pwd    = body.get("password") or ""
    name   = (body.get("display_name") or "").strip() or email.split("@")[0]
    code   = (body.get("access_code") or "").strip()

    if not email or "@" not in email:
        return jsonify({"error": "Вкажіть коректний email"}), 400
    if len(pwd) < 8:
        return jsonify({"error": "Пароль має містити щонайменше 8 символів"}), 400

    try:
        db = get_db()
        existing = q(db, "SELECT id FROM users WHERE email=%s", (email,))
        if existing:
            db.close()
            return jsonify({"error": "Користувач з таким email вже існує"}), 409

        # Перший зареєстрований користувач автоматично стає адміністратором
        # і не потребує коду допуску. Кожна наступна реєстрація — тільки за кодом.
        is_first  = not any_users_exist()
        code_row  = None
        if not is_first:
            if not code:
                db.close()
                return jsonify({"error": "Потрібен код допуску"}), 400
            code_row = q(db, """
                SELECT id FROM access_codes
                WHERE code=%s AND used_by IS NULL AND is_revoked=0
            """, (code,))
            if not code_row:
                db.close()
                return jsonify({"error": "Невірний або вже використаний код допуску"}), 403

        pwd_hash = generate_password_hash(pwd)

        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO users (email, password_hash, display_name, is_admin, setup_done, last_login_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
            """, (email, pwd_hash, name, 1 if is_first else 0, 1 if not is_first else 0))
            user_id = cur.lastrowid
            if code_row:
                cur.execute("""
                    UPDATE access_codes SET used_by=%s, used_at=NOW() WHERE id=%s
                """, (user_id, code_row["id"]))
        db.commit()
        db.close()

        session.permanent = True
        session["user_id"] = user_id
        return jsonify({"ok": True, "user_id": user_id, "is_admin": is_first,
                        "setup_done": not is_first})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    body  = flask_request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    pwd   = body.get("password") or ""

    if not email or not pwd:
        return jsonify({"error": "Вкажіть email і пароль"}), 400

    try:
        db   = get_db()
        user = q(db, "SELECT id, email, display_name, password_hash, is_admin, setup_done, role FROM users WHERE email=%s", (email,))
        if not user or not check_password_hash(user["password_hash"], pwd):
            db.close()
            return jsonify({"error": "Невірний email або пароль"}), 401

        with db.cursor() as cur:
            cur.execute("UPDATE users SET last_login_at=NOW() WHERE id=%s", (user["id"],))
        db.commit()
        db.close()

        session.permanent = True
        session["user_id"] = user["id"]
        return jsonify({"ok": True, "user_id": user["id"],
                        "email": user["email"], "display_name": user["display_name"],
                        "is_admin": bool(user["is_admin"]),
                        "role": "admin" if user["is_admin"] else (user.get("role") or "user"),
                        "is_owner": user["id"] == 1,
                        "setup_done": bool(user["setup_done"])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    session.clear()
    return jsonify({"ok": True})


@bp.route("/api/auth/me")
def api_auth_me():
    user = current_user()
    if not user:
        return jsonify({"authenticated": False, "any_users_exist": any_users_exist()})
    return jsonify({
        "authenticated": True,
        "id":            user["id"],
        "user_id":       user["id"],
        "email":         user["email"],
        "display_name":  user["display_name"],
        "is_admin":      bool(user["is_admin"]),
        "role":          "admin" if user["is_admin"] else (user.get("role") or "user"),
        "is_owner":      user["id"] == 1,
        "setup_done":    bool(user["setup_done"]),
    })


@bp.route("/api/auth/complete-setup", methods=["POST"])
@admin_required
def api_auth_complete_setup():
    """Зберігає вибір сховища (own/shared) і позначає що онбординг пройдено."""
    try:
        body         = flask_request.get_json(silent=True) or {}
        storage_mode = body.get("storage_mode", "shared")
        if storage_mode not in ("own", "shared"):
            storage_mode = "shared"

        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                "UPDATE users SET setup_done=1, storage_mode=%s WHERE id=%s",
                (storage_mode, session["user_id"])
            )
        db.commit()
        db.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/user-settings", methods=["GET"])
@admin_required
def api_user_settings_get():
    """
    Повертає ВСІ налаштування поточного юзера (e621, inkbunny, download, а
    також секції застосунків — напр. nextcloud) — замінює собою старий /api/config для все, окрім
    підключення до основної БД (те лишається в archiver_config.json/env).
    Паролі/ключі маскуються зірочками.
    """
    settings = get_user_settings()
    safe = json.loads(json.dumps(settings))  # deep copy
    for section in ("nextcloud", "nextcloud_db", "inkbunny", "mail"):
        if section in safe and safe[section].get("password"):
            safe[section]["password"] = "••••••••"
    if "e621" in safe and safe["e621"].get("api_key"):
        safe["e621"]["api_key"] = "••••••••"
    return jsonify(safe)


@bp.route("/api/user-settings", methods=["POST"])
@admin_required
def api_user_settings_save():
    """
    Зберігає налаштування юзера. Приймає частковий патч — мерджиться з існуючим,
    тому фронтенд може надсилати тільки змінену секцію.
    Поля паролів з порожнім значенням НЕ затирають вже збережений пароль
    (той самий патерн що був у старому /api/config).
    """
    try:
        patch   = flask_request.get_json(silent=True) or {}
        current = get_user_settings()

        for section in ("nextcloud", "nextcloud_db", "inkbunny", "mail"):
            if section in patch and isinstance(patch[section], dict):
                if not patch[section].get("password"):
                    patch[section]["password"] = current.get(section, {}).get("password", "")

        if "e621" in patch and isinstance(patch["e621"], dict):
            if not patch["e621"].get("api_key"):
                patch["e621"]["api_key"] = current.get("e621", {}).get("api_key", "")

        merge_user_settings(patch)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Перше налаштування БД (до будь-якої авторизації) ──────────────────────

def _db_is_configured() -> bool:
    """Перевіряє чи можемо взагалі підключитись до БД (env-змінні або конфіг)."""
    try:
        get_db().close()
        return True
    except Exception:
        return False


@bp.route("/api/db/status")
def api_db_status():
    """Публічний ендпоінт — фронтенд викликає його ще до екрану логіну,
    щоб зрозуміти чи показувати форму підключення до БД."""
    env_locked = bool(os.environ.get("DB_HOST"))
    return jsonify({
        "configured": _db_is_configured(),
        "env_locked": env_locked,  # якщо True — конфіг задається через env, форму ховаємо
    })


@bp.route("/api/db/setup", methods=["POST"])
def api_db_setup():
    """Публічний ендпоінт для одноразового підключення БД через UI,
    якщо адмін не задав DB_HOST через змінні оточення контейнера."""
    if os.environ.get("DB_HOST"):
        return jsonify({"error": "БД налаштована через змінні оточення — змінити з інтерфейсу не можна"}), 403
    if _db_is_configured():
        return jsonify({"error": "БД вже підключена"}), 409

    body = flask_request.get_json(silent=True) or {}
    host = (body.get("host") or "").strip()
    port = body.get("port") or 3306
    user = (body.get("user") or "").strip()
    pwd  = body.get("password") or ""
    name = (body.get("database") or "").strip()

    if not host or not user or not name:
        return jsonify({"error": "Заповніть хост, користувача і назву бази"}), 400

    # Пробуємо підключитись з цими даними ДО збереження — щоб не записати биту конфігурацію
    try:
        test_conn = pymysql.connect(
            host=host, port=int(port), user=user, password=pwd,
            database=name, charset="utf8mb4", connect_timeout=5,
        )
        test_conn.close()
    except Exception as e:
        return jsonify({"error": f"Не вдалося підключитись: {e}"}), 400

    cfg = load_config()
    cfg["database"] = {"host": host, "port": int(port), "user": user, "password": pwd, "database": name}
    save_config(cfg)

    # Створюємо таблиці одразу після успішного підключення
    ensure_tables()

    # Застосунки на старті не завантажились (бази не було) — перезапускаємо
    # процес сам себе, щоб вони підхопились. Відповідь встигає піти.
    from core.app_manager import manager
    restarting = not manager.apps
    if restarting:
        import sys, threading, time

        def _reexec():
            time.sleep(1.5)
            print("  ↻ База підключена — перезапуск, щоб завантажити застосунки")
            # werkzeug лишає слухаючий сокет у спадок новому процесу —
            # закриваємо, інакше новий не зможе зайняти той самий порт
            fd = os.environ.pop("WERKZEUG_SERVER_FD", None)
            if fd:
                try:
                    os.close(int(fd))
                except OSError:
                    pass
            os.execv(sys.executable, getattr(sys, "orig_argv", None) or [sys.executable] + sys.argv)
        threading.Thread(target=_reexec, daemon=True).start()
    return jsonify({"ok": True, "restarting": restarting})


# ── Коди допуску (тільки адмін) ────────────────────────────────────────────

@bp.route("/api/access-codes", methods=["GET"])
@admin_required
def api_access_codes_list():
    try:
        db   = get_db()
        rows = qq(db, """
            SELECT ac.id, ac.code, ac.created_at, ac.used_at, ac.is_revoked,
                   u.email AS used_by_email
            FROM access_codes ac
            LEFT JOIN users u ON u.id = ac.used_by
            ORDER BY ac.created_at DESC
        """)
        db.close()
        for r in rows:
            if r["created_at"]: r["created_at"] = r["created_at"].isoformat()
            if r["used_at"]:    r["used_at"]    = r["used_at"].isoformat()
            r["is_revoked"] = bool(r["is_revoked"])
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/access-codes", methods=["POST"])
@admin_required
def api_access_codes_create():
    try:
        code = secrets.token_urlsafe(9)  # короткий, зручний для передачі код
        db = get_db()
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO access_codes (code, created_by) VALUES (%s, %s)
            """, (code, session["user_id"]))
            code_id = cur.lastrowid
        db.commit()
        db.close()
        return jsonify({"id": code_id, "code": code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/access-codes/<int:code_id>", methods=["DELETE"])
@admin_required
def api_access_codes_revoke(code_id):
    """Відкликає (деактивує) код допуску. Використані коди теж можна відкликати
    заднім числом — просто прибирається сам запис якщо ще не використаний,
    інакше позначається revoked для історії."""
    try:
        db = get_db()
        row = q(db, "SELECT used_by FROM access_codes WHERE id=%s", (code_id,))
        if not row:
            db.close()
            return jsonify({"error": "not found"}), 404
        with db.cursor() as cur:
            if row["used_by"]:
                cur.execute("UPDATE access_codes SET is_revoked=1 WHERE id=%s", (code_id,))
            else:
                cur.execute("DELETE FROM access_codes WHERE id=%s", (code_id,))
        db.commit()
        db.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
