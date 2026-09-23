"""Чат"""


from flask import Blueprint, jsonify, request as flask_request


from blueprints.federation import get_external_archives
from core.auth import current_user_id
from core.db import get_db, q, qq

bp = Blueprint("chat", __name__)


@bp.route("/api/chat/conversations")
def api_chat_conversations():
    """Список розмов поточного юзера — остання репліка й непрочитане в кожній."""
    uid = current_user_id()
    db  = get_db()
    rows = qq(db, """
        SELECT peer_label, peer_local_user_id, peer_archive_id, peer_identity,
               body AS last_body, created_at AS last_at,
               (SELECT COUNT(*) FROM chat_messages m2
                WHERE m2.owner_user_id=%s AND m2.direction='in' AND m2.is_read=0
                  AND ((m2.peer_local_user_id <=> m1.peer_local_user_id)
                   AND (m2.peer_archive_id <=> m1.peer_archive_id)
                   AND (m2.peer_identity <=> m1.peer_identity))
               ) AS unread
        FROM chat_messages m1
        WHERE owner_user_id=%s AND id IN (
            SELECT MAX(id) FROM chat_messages WHERE owner_user_id=%s
            GROUP BY peer_local_user_id, peer_archive_id, peer_identity
        )
        ORDER BY last_at DESC
    """, (uid, uid, uid))
    db.close()
    for r in rows:
        if r["last_at"]: r["last_at"] = r["last_at"].isoformat()
    return jsonify(rows)


@bp.route("/api/chat/messages")
def api_chat_messages():
    """Історія переписки з конкретним співрозмовником — позначає вхідні як прочитані."""
    uid = current_user_id()
    peer_local_user_id = flask_request.args.get("peer_local_user_id")
    peer_archive_id     = flask_request.args.get("peer_archive_id")
    peer_identity        = flask_request.args.get("peer_identity")

    where = ["owner_user_id=%s"]
    args  = [uid]
    if peer_local_user_id:
        where.append("peer_local_user_id=%s")
        args.append(int(peer_local_user_id))
    elif peer_archive_id and peer_identity:
        where.append("peer_archive_id=%s AND peer_identity=%s")
        args.extend([peer_archive_id, peer_identity])
    else:
        return jsonify({"error": "Вкажи співрозмовника"}), 400

    db   = get_db()
    rows = qq(db, f"SELECT * FROM chat_messages WHERE {' AND '.join(where)} ORDER BY created_at ASC", args)
    with db.cursor() as cur:
        cur.execute(f"UPDATE chat_messages SET is_read=1 WHERE {' AND '.join(where)} AND direction='in'", args)
    db.commit(); db.close()
    for r in rows:
        if r["created_at"]: r["created_at"] = r["created_at"].isoformat()
    return jsonify(rows)


@bp.route("/api/chat/unread-count")
def api_chat_unread_count():
    db  = get_db()
    row = q(db, "SELECT COUNT(*) AS c FROM chat_messages WHERE owner_user_id=%s AND direction='in' AND is_read=0",
            (current_user_id(),))
    db.close()
    return jsonify({"count": row["c"]})


@bp.route("/api/chat/send", methods=["POST"])
def api_chat_send():
    body_data           = flask_request.get_json(silent=True) or {}
    msg_text            = (body_data.get("body") or "").strip()
    peer_local_user_id  = body_data.get("peer_local_user_id")
    peer_archive_id     = body_data.get("peer_archive_id")
    peer_identity       = (body_data.get("peer_identity") or "").strip()
    if not msg_text:
        return jsonify({"error": "Порожнє повідомлення"}), 400

    uid = current_user_id()
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
                INSERT INTO chat_messages (owner_user_id, direction, peer_label, peer_local_user_id, body, is_read)
                VALUES (%s,'out',%s,%s,%s,1)
            """, (uid, recipient_label, peer_local_user_id, msg_text))
            cur.execute("""
                INSERT INTO chat_messages (owner_user_id, direction, peer_label, peer_local_user_id, body)
                VALUES (%s,'in',%s,%s,%s)
            """, (peer_local_user_id, sender_label, uid, msg_text))
        db.commit(); db.close()
        return jsonify({"ok": True})

    elif peer_archive_id and peer_identity:
        archives = get_external_archives(uid)
        cfg = next((a for a in archives if a["id"] == peer_archive_id), None)
        if not cfg:
            db.close()
            return jsonify({"error": "Архів не знайдено"}), 404
        peer_label = f"{peer_identity}@{cfg['label']}"
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO chat_messages (owner_user_id, direction, peer_label, peer_archive_id, peer_identity, body, is_read)
                VALUES (%s,'out',%s,%s,%s,%s,1)
            """, (uid, peer_label, peer_archive_id, peer_identity, msg_text))
        db.commit(); db.close()

        # Реле — реально надсилаємо на сервер співрозмовника
        import requests as req
        base = cfg.get("base_url", "").rstrip("/")
        my_base_url = flask_request.host_url.rstrip("/")
        try:
            r = req.post(f"{base}/api/external-access/chat/deliver",
                         headers={"X-Access-Key": cfg.get("access_key", ""), "X-Identity": cfg.get("my_identity", "")},
                         json={"sender_base_url": my_base_url, "to_nick": peer_identity, "body": msg_text},
                         timeout=15)
            if r.status_code != 200:
                return jsonify({"ok": True, "warning": r.json().get("error", "Не вдалося доставити")})
        except Exception as e:
            return jsonify({"ok": True, "warning": f"Збережено локально, але не вдалося доставити: {e}"})
        return jsonify({"ok": True})

    return jsonify({"error": "Вкажи співрозмовника"}), 400
