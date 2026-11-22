"""Чат"""


from flask import Blueprint, jsonify, request as flask_request

from core.auth import current_user_id
from core.db import get_db, q, qq

bp = Blueprint("chat", __name__)


@bp.route("/api/chat/conversations")
def api_chat_conversations():
    """Список розмов поточного юзера — остання репліка й непрочитане в кожній."""
    uid = current_user_id()
    db  = get_db()
    rows = qq(db, """
        SELECT peer_label, peer_local_user_id, peer_identity, peer_url,
               body AS last_body, attachment AS last_att, created_at AS last_at,
               EXISTS(SELECT 1 FROM chat_blocked b WHERE b.owner_user_id=%s AND b.peer_url=m1.peer_url) AS blocked,
               (SELECT COUNT(*) FROM chat_messages m2
                WHERE m2.owner_user_id=%s AND m2.direction='in' AND m2.is_read=0
                  AND ((m2.peer_local_user_id <=> m1.peer_local_user_id)
                   AND (m2.peer_url <=> m1.peer_url)
                   AND (m2.peer_identity <=> m1.peer_identity))
               ) AS unread
        FROM chat_messages m1
        WHERE owner_user_id=%s AND id IN (
            SELECT MAX(id) FROM chat_messages WHERE owner_user_id=%s
            GROUP BY peer_local_user_id, peer_url, peer_identity
        )
        ORDER BY last_at DESC
    """, (uid, uid, uid, uid))
    db.close()
    import json as _json
    for r in rows:
        if r["last_at"]: r["last_at"] = r["last_at"].isoformat()
        if not r.get("last_body") and r.get("last_att"):
            try:
                r["last_body"] = "📎 " + (_json.loads(r["last_att"]).get("name") or "файл")
            except ValueError:
                r["last_body"] = "📎 файл"
        r.pop("last_att", None)
    return jsonify(rows)


@bp.route("/api/chat/messages")
def api_chat_messages():
    """Історія переписки з конкретним співрозмовником — позначає вхідні як прочитані."""
    uid = current_user_id()
    peer_local_user_id = flask_request.args.get("peer_local_user_id")
    peer_identity        = flask_request.args.get("peer_identity")
    peer_url             = flask_request.args.get("peer_url")

    where = ["owner_user_id=%s"]
    args  = [uid]
    if peer_url:
        where.append("peer_url=%s AND peer_identity=%s")
        args.extend([peer_url, peer_identity or ""])
    elif peer_local_user_id:
        where.append("peer_local_user_id=%s")
        args.append(int(peer_local_user_id))
    else:
        return jsonify({"error": "Вкажи співрозмовника"}), 400

    db   = get_db()
    rows = qq(db, f"SELECT * FROM chat_messages WHERE {' AND '.join(where)} ORDER BY created_at ASC", args)
    with db.cursor() as cur:
        cur.execute(f"UPDATE chat_messages SET is_read=1 WHERE {' AND '.join(where)} AND direction='in' AND is_read=0", args)
        newly_read = cur.rowcount
        if newly_read and peer_local_user_id:
            # локальний співрозмовник: його вихідні до мене — прочитані
            cur.execute("""UPDATE chat_messages SET read_at=NOW()
                           WHERE owner_user_id=%s AND peer_local_user_id=%s AND direction='out' AND read_at IS NULL""",
                        (int(peer_local_user_id), uid))
    db.commit(); db.close()
    if newly_read and peer_url:
        from .by_url import send_read_receipt
        send_read_receipt(uid, peer_url, peer_identity or "")
    import json as _json
    for r in rows:
        if r["created_at"]: r["created_at"] = r["created_at"].isoformat()
        r.pop("deliver_token", None)
        if r.get("read_at"): r["read_at"] = r["read_at"].isoformat()
        try:
            r["attachment"] = _json.loads(r["attachment"]) if r.get("attachment") else None
        except (ValueError, KeyError):
            r["attachment"] = None
    return jsonify(rows)


@bp.route("/api/chat/unread-count")
def api_chat_unread_count():
    db  = get_db()
    uid = current_user_id()
    row = q(db, "SELECT COUNT(*) AS c FROM chat_messages WHERE owner_user_id=%s AND direction='in' AND is_read=0",
            (uid,))
    latest = None
    if row["c"]:
        latest = q(db, """SELECT id, peer_label, peer_local_user_id, peer_url, peer_identity, body, attachment
                          FROM chat_messages WHERE owner_user_id=%s AND direction='in' AND is_read=0
                          ORDER BY id DESC LIMIT 1""", (uid,))
        if latest:
            if not latest["body"] and latest.get("attachment"):
                latest["body"] = "📎 файл"
            latest.pop("attachment", None)
            latest["body"] = (latest["body"] or "")[:140]
    db.close()
    return jsonify({"count": row["c"], "latest": latest})


@bp.route("/api/chat/send", methods=["POST"])
def api_chat_send():
    body_data           = flask_request.get_json(silent=True) or {}
    msg_text            = (body_data.get("body") or "").strip()
    peer_local_user_id  = body_data.get("peer_local_user_id")
    peer_identity       = (body_data.get("peer_identity") or "").strip()
    uid = current_user_id()
    att, att_json = None, None
    if body_data.get("file_id") or body_data.get("upload_id"):
        from .by_url import my_public_url
        from .extras import attachment_from_request
        att, err = attachment_from_request(uid, body_data, my_public_url())
        if err:
            return jsonify({"error": err}), 404
        import json as _json
        att_json = _json.dumps(att, ensure_ascii=False)
    if not msg_text and not att:
        return jsonify({"error": "Порожнє повідомлення"}), 400
    if body_data.get("peer_url"):
        # будь-який архів за адресою — без взаємного підключення
        from .by_url import MAX_LEN, normalize_url, send_by_url
        url = normalize_url(body_data.get("peer_url"))
        if not url:
            return jsonify({"error": "Невірна адреса архіву (потрібна https://, http — лише в локальній мережі)"}), 400
        if len(msg_text) > MAX_LEN:
            return jsonify({"error": f"Задовге повідомлення (до {MAX_LEN} символів)"}), 400
        ok, warning, real, url = send_by_url(uid, url, peer_identity, msg_text, att)
        return jsonify({"ok": True, **({"warning": warning} if warning else {}), "peer_url": url,
                        **({"peer_identity": real} if real else {})})
    db  = get_db()
    sender = q(db, "SELECT display_name, email FROM users WHERE id=%s", (uid,))
    sender_label = sender["display_name"] or sender["email"]

    if peer_local_user_id:
        recipient = q(db, "SELECT display_name, email FROM users WHERE id=%s", (peer_local_user_id,))
        if not recipient:
            db.close()
            return jsonify({"error": "Юзера не знайдено"}), 404
        recipient_label = recipient["display_name"] or recipient["email"]
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO chat_messages (owner_user_id, direction, peer_label, peer_local_user_id, body, is_read, attachment)
                VALUES (%s,'out',%s,%s,%s,1,%s)
            """, (uid, recipient_label, peer_local_user_id, msg_text, att_json))
            cur.execute("""
                INSERT INTO chat_messages (owner_user_id, direction, peer_label, peer_local_user_id, body, attachment)
                VALUES (%s,'in',%s,%s,%s,%s)
            """, (peer_local_user_id, sender_label, uid, msg_text, att_json))
        db.commit(); db.close()
        return jsonify({"ok": True})

    db.close()
    return jsonify({"error": "Вкажи співрозмовника"}), 400
