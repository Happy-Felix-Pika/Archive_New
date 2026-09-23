"""Приймання повідомлень від чужих архівів.

Раніше жило у федерації ядра, але пише в таблицю чату — отже, це
частина застосунку. Адреса лишається тією ж, бо її вже знають інші
архіви.
"""

import random, secrets
from flask import Blueprint, Response, jsonify, request as flask_request
from blueprints.catalog import api_catalog_file, api_catalog_list
from blueprints.gallery import _gallery_ids_query, _resolve_tag_names, api_gallery_meta, api_gallery_stream
from core.auth import admin_required, current_user_id
from core.db import get_db, q, qq
import hashlib as _keyhash

bp = Blueprint("chat_federation", __name__)


@bp.route("/api/external-access/chat/deliver", methods=["POST"])
def api_external_access_chat_deliver():
    """
    Приймає повідомлення від ІНШОГО архіву — НЕЗАЛЕЖНО від того, чи активний
    ключ доступу до контенту (той можна деактивувати окремо, не рвучи чат).
    Єдина умова — взаємність: у НАС САМИХ має бути запис про підключення до
    адреси відправника. Ключ тут навмисно НЕ перевіряється — інакше
    деактивація ключа доступу до контенту автоматично ламала б і чат теж.
    """
    sender_identity = flask_request.headers.get("X-Identity", "")

    body            = flask_request.get_json(silent=True) or {}
    sender_base_url = (body.get("sender_base_url") or "").rstrip("/")
    to_nick         = (body.get("to_nick") or "").strip()
    msg_body        = (body.get("body") or "").strip()
    if not sender_base_url or not to_nick or not msg_body:
        return jsonify({"error": "Некоректні дані"}), 400

    db = get_db()
    mutual = q(db, "SELECT id, label FROM external_archives WHERE base_url=%s", (sender_base_url,))
    if not mutual:
        db.close()
        return jsonify({"error": "Немає взаємного підключення — цей архів має спершу підключити і твій теж"}), 403

    recipient = q(db, "SELECT id FROM users WHERE display_name=%s OR email=%s", (to_nick, to_nick))
    if not recipient:
        db.close()
        return jsonify({"error": f"Юзера '{to_nick}' у нас не знайдено"}), 404

    peer_label = f"{sender_identity}@{mutual['label']}"
    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO chat_messages (owner_user_id, direction, peer_label, peer_archive_id, peer_identity, body)
            VALUES (%s,'in',%s,%s,%s,%s)
        """, (recipient["id"], peer_label, mutual["id"], sender_identity, msg_body))
    db.commit(); db.close()
    return jsonify({"ok": True})


