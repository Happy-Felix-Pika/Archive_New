"""Керування користувачами"""


from flask import Blueprint, jsonify, request as flask_request


from core.auth import OWNER_ID, ROLES, admin_required, current_user_id, is_owner
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
        SELECT u.id, u.email, u.display_name, u.is_admin, u.role, u.created_at, u.last_login_at,
               (SELECT ac.code FROM access_codes ac WHERE ac.used_by = u.id
                 ORDER BY ac.used_at DESC LIMIT 1) AS reg_code
        FROM users u ORDER BY u.id
    """)
    db.close()
    for r in rows:
        r["role"] = "admin" if r["is_admin"] else (r.get("role") or "user")
        r["is_owner"] = r["id"] == OWNER_ID
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
    if user_id == OWNER_ID:
        return jsonify({"error": "Творця архіву видалити не можна"}), 403
    try:
        db  = get_db()
        row = q(db, "SELECT is_admin, display_name FROM users WHERE id=%s", (user_id,))
        if not row:
            db.close()
            return jsonify({"error": "Юзера не знайдено"}), 404
        if row["is_admin"] and not is_owner():
            return jsonify({"error": "Видалити адміна може лише Творець"}), 403
        if row["is_admin"]:
            admin_count = q(db, "SELECT COUNT(*) AS c FROM users WHERE is_admin=1")["c"]
            if admin_count <= 1:
                db.close()
                return jsonify({"error": "Не можна видалити останнього адміна"}), 400
        with db.cursor() as cur:
            # код, яким людина зареєструвалась, іде разом з нею
            cur.execute("DELETE FROM access_codes WHERE used_by=%s", (user_id,))
            cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
        db.commit(); db.close()
        return jsonify({"ok": True, "removed": row["display_name"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/users/<int:user_id>/role", methods=["POST"])
@admin_required
def api_users_set_role(user_id):
    """
    Роль користувача: user / moderator / admin. Свою роль змінити не можна
    (щоб випадково не позбавити себе адмінки), і архів не лишиться без адміна.
    """
    role = ((flask_request.get_json(silent=True) or {}).get("role") or "").strip()
    if role not in ROLES:
        return jsonify({"error": "Роль: user, moderator або admin"}), 400
    if user_id == current_user_id():
        return jsonify({"error": "Свою роль змінити не можна"}), 400
    if user_id == OWNER_ID:
        return jsonify({"error": "Творець завжди адмін — його роль не змінюється"}), 403
    db = get_db()
    row = q(db, "SELECT is_admin, display_name, email FROM users WHERE id=%s", (user_id,))
    if not row:
        return jsonify({"error": "Користувача не знайдено"}), 404
    # Адмінів призначає й знімає лише Творець; звичайні адміни керують
    # модераторами й користувачами.
    if (role == "admin" or row["is_admin"]) and not is_owner():
        return jsonify({"error": "Призначати й знімати адмінів може лише Творець"}), 403
    if row["is_admin"] and role != "admin":
        n = q(db, "SELECT COUNT(*) AS c FROM users WHERE is_admin=1")["c"]
        if n <= 1:
            return jsonify({"error": "Не можна лишити архів без жодного адміна"}), 400
    with db.cursor() as cur:
        cur.execute("UPDATE users SET role=%s, is_admin=%s WHERE id=%s",
                    (role, 1 if role == "admin" else 0, user_id))
    db.commit()
    return jsonify({"ok": True, "role": role})


# ── Журнал модераторів ────────────────────────────────────────────────

@bp.route("/api/modlog")
@admin_required
def api_modlog_list():
    from core import modlog
    args = flask_request.args
    items, total = modlog.list_entries(limit=min(int(args.get("limit", 100)), 500),
                                       offset=int(args.get("offset", 0)),
                                       user_id=args.get("user_id") or None,
                                       only_active=args.get("active") == "1")
    db = get_db()
    mods = qq(db, """SELECT DISTINCT u.id, COALESCE(u.display_name, u.email) AS name
                     FROM mod_actions m JOIN users u ON u.id = m.user_id ORDER BY name""")
    return jsonify({"items": items, "total": total, "moderators": mods})


@bp.route("/api/modlog/<int:entry_id>/undo", methods=["POST"])
@admin_required
def api_modlog_undo(entry_id):
    from core import modlog
    try:
        return jsonify(modlog.undo(entry_id, current_user_id()))
    except modlog.UndoError as e:
        return jsonify({"error": str(e)}), 409
