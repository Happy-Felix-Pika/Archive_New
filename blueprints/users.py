"""Керування користувачами"""


from flask import Blueprint, jsonify


from core.auth import admin_required, current_user_id
from core.db import get_db, q, qq

bp = Blueprint("users", __name__)


@bp.route("/api/users/list")
def api_users_list_simple():
    """Список локальних юзерів (для видачі доступу до підключеного архіву)."""
    db   = get_db()
    rows = qq(db, "SELECT id, email, display_name, is_admin FROM users ORDER BY id")
    db.close()
    return jsonify(rows)


@bp.route("/api/users/list-full")
@admin_required
def api_users_list_full():
    """Повний список юзерів з датами — тільки для адміна."""
    db   = get_db()
    rows = qq(db, """
        SELECT id, email, display_name, is_admin, created_at, last_login_at
        FROM users ORDER BY id
    """)
    db.close()
    for r in rows:
        if r["created_at"]:     r["created_at"]     = r["created_at"].isoformat()
        if r["last_login_at"]:  r["last_login_at"]  = r["last_login_at"].isoformat()
    return jsonify(rows)


@bp.route("/api/users/<int:user_id>", methods=["DELETE"])
@admin_required
def api_users_delete(user_id):
    """Видаляє обліковий запис юзера. Не можна видалити самого себе, і не
    можна лишити архів взагалі без жодного адміна."""
    if user_id == current_user_id():
        return jsonify({"error": "Не можна видалити самого себе"}), 400
    try:
        db  = get_db()
        row = q(db, "SELECT is_admin, display_name FROM users WHERE id=%s", (user_id,))
        if not row:
            db.close()
            return jsonify({"error": "Юзера не знайдено"}), 404
        if row["is_admin"]:
            admin_count = q(db, "SELECT COUNT(*) AS c FROM users WHERE is_admin=1")["c"]
            if admin_count <= 1:
                db.close()
                return jsonify({"error": "Не можна видалити останнього адміна"}), 400
        with db.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
        db.commit(); db.close()
        return jsonify({"ok": True, "removed": row["display_name"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
