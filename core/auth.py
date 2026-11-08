"""auth"""
from core.config import load_config, save_config
from core.db import get_db, q


from functools import wraps

from flask import jsonify, session


# ── Auth ──────────────────────────────────────────────────────────────────

def current_user():
    """Повертає словник поточного користувача з сесії або None."""
    uid = session.get("user_id")
    if not uid:
        return None
    try:
        db = get_db()
        user = q(db, "SELECT id, email, display_name, is_admin, setup_done, role FROM users WHERE id=%s", (uid,))
        db.close()
        return user
    except Exception:
        return None


def login_required(f):
    """Декоратор: вимагає активної сесії, інакше 401."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Потрібна авторизація"}), 401
        return f(*args, **kwargs)
    return wrapper


def is_current_user_admin() -> bool:
    if not session.get("user_id"):
        return False
    try:
        db   = get_db()
        user = q(db, "SELECT is_admin FROM users WHERE id=%s", (session["user_id"],))
        db.close()
        return bool(user and user.get("is_admin"))
    except Exception:
        return False


def admin_required(f):
    """Декоратор: вимагає активної сесії І прав адміністратора, інакше 403."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Потрібна авторизація"}), 401
        try:
            db   = get_db()
            user = q(db, "SELECT is_admin FROM users WHERE id=%s", (session["user_id"],))
            db.close()
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        if not user or not user.get("is_admin"):
            return jsonify({"error": "Потрібні права адміністратора"}), 403
        return f(*args, **kwargs)
    return wrapper


# ── Ролі ──────────────────────────────────────────────────────────────────
#
#   user       — перегляд, улюблене, «Онлайн», чат; теги лише ПРОПОНУЄ.
#   moderator  — завантаження, переміщення, видалення в кошик, теги, черга
#                «На перевірку» (по одній парі), пули, запуск архіватора для
#                одного автора. Не стирає назавжди, не має масових дій і
#                налаштувань. Усе, що він робить, пишеться в журнал (core.modlog).
#   admin      — усе.

ROLES = ("user", "moderator", "admin")


def current_role() -> str:
    """Роль поточного користувача ('user' без сесії теж)."""
    uid = session.get("user_id")
    if not uid:
        return "user"
    try:
        db = get_db()
        u = q(db, "SELECT is_admin, role FROM users WHERE id=%s", (uid,))
        db.close()
    except Exception:
        return "user"
    if not u:
        return "user"
    if u.get("is_admin"):
        return "admin"
    return u.get("role") if u.get("role") in ROLES else "user"


def is_current_user_moderator() -> bool:
    """Модератор АБО адмін — тобто може модерувати."""
    return current_role() in ("moderator", "admin")


def moderator_required(f):
    """Декоратор: модератор або адмін, інакше 403."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Потрібна авторизація"}), 401
        if current_role() not in ("moderator", "admin"):
            return jsonify({"error": "Потрібні права модератора"}), 403
        return f(*args, **kwargs)
    return wrapper


def any_users_exist() -> bool:
    """Перевіряє чи є хоч один зареєстрований користувач (для логіки першого запуску)."""
    try:
        db  = get_db()
        row = q(db, "SELECT COUNT(*) AS c FROM users")
        db.close()
        return (row["c"] if row else 0) > 0
    except Exception:
        return False


def current_user_id() -> int:
    """ID поточного залогіненого юзера. 1 (адмін) якщо auth відсутній (наприклад CLI/скрипти)."""
    return session.get("user_id") or 1


def get_user_settings(user_id: int | None = None) -> dict:
    """
    Повертає ВСІ налаштування (nextcloud, nextcloud_db, e621, inkbunny, download)
    з archiver_config.json (все, крім секції 'database'). Параметр user_id
    лишений для сумісності сигнатур виклику — налаштування тепер спільні для всіх.
    """
    file_cfg = load_config()
    return {k: v for k, v in file_cfg.items() if k != "database"}


def save_user_settings(settings: dict, user_id: int | None = None):
    """Зберігає весь блок налаштувань у archiver_config.json, не чіпаючи секцію 'database'."""
    file_cfg = load_config()
    new_cfg = {"database": file_cfg.get("database", {})}
    new_cfg.update(settings)
    save_config(new_cfg)


def merge_user_settings(patch: dict, user_id: int | None = None):
    """Часткове оновлення — мерджить тільки передані секції/ключі, решта лишається."""
    current = get_user_settings()
    for section, values in patch.items():
        if isinstance(values, dict) and isinstance(current.get(section), dict):
            current[section].update(values)
        else:
            current[section] = values
    save_user_settings(current)


def get_user_nc_config(user_id: int | None = None) -> dict:
    """Повертає Nextcloud-конфіг зі спільного файлового конфігу."""
    return get_user_settings().get("nextcloud", {})
