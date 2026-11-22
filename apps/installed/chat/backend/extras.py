"""
Чат: файли з архіву, нотатки, видалення розмови.

Файл у повідомленні — це посилання на файл архіву відправника з
неосяжним токеном (/api/external-access/chat/file/<токен>). Так його бачить
і співрозмовник з іншого архіву, без ключів доступу; решту архіву токен
не відкриває.
"""

import hashlib
import json
import mimetypes
import os
import secrets

from flask import Blueprint, jsonify, request as flask_request, send_file

from core.auth import current_user_id, is_current_user_admin
from core.db import get_db, q, qq

bp = Blueprint("chat_extras", __name__)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _hidden_for(uid) -> set:
    if is_current_user_admin():
        return set()
    try:
        from blueprints.catalog import get_hidden_catalog_folders
        return get_hidden_catalog_folders(uid)
    except Exception:
        return set()


def make_attachment(uid: int, file_id, base_url: str) -> dict | None:
    """Вкладення для повідомлення: {name, ext, url} — або None, якщо файл недоступний."""
    from core.storage_api import is_service_path
    try:
        file_id = int(file_id)
    except (TypeError, ValueError):
        return None
    db = get_db()
    try:
        f = q(db, "SELECT id, name, ext, rel_path FROM files WHERE id=%s", (file_id,))
        if not f or is_service_path(f["rel_path"]):
            return None
        top = f["rel_path"].split("/", 1)[0]
        if "/" in f["rel_path"] and top in _hidden_for(uid):
            return None
        token = secrets.token_urlsafe(24)
        with db.cursor() as cur:
            cur.execute("INSERT INTO chat_shares (token_hash, file_id, owner_user_id) VALUES (%s,%s,%s)",
                        (_sha(token), file_id, uid))
        db.commit()
    finally:
        db.close()
    return {"name": f["name"], "ext": (f["ext"] or "").lower(), "file_id": file_id,
            "url": f"{base_url.rstrip('/')}/api/external-access/chat/file/{token}"}


def clean_incoming_attachment(att, sender_base: str) -> str | None:
    """Вкладення з чужого архіву: лише посилання на сам той архів (не на будь-що)."""
    if not isinstance(att, dict):
        return None
    url = str(att.get("url") or "")
    if not url.startswith(sender_base.rstrip("/") + "/api/external-access/chat/file/"):
        return None
    return json.dumps({"name": str(att.get("name") or "")[:255], "ext": str(att.get("ext") or "")[:10],
                       "url": url[:1000]}, ensure_ascii=False)


@bp.route("/api/external-access/chat/file/<token>")
def api_chat_file(token):
    """Файл, надісланий у чат, — за токеном з повідомлення."""
    from core.files_api import local_path
    from core.storage_api import is_service_path
    if not token or len(token) > 200:
        return jsonify({"error": "not found"}), 404
    db = get_db()
    try:
        row = q(db, """SELECT f.name, f.rel_path FROM chat_shares s JOIN files f ON f.id = s.file_id
                       WHERE s.token_hash=%s""", (_sha(token),))
    finally:
        db.close()
    if not row or is_service_path(row["rel_path"]):
        return jsonify({"error": "Файл більше недоступний"}), 404
    p = local_path(row["rel_path"])
    if not os.path.isfile(p):
        return jsonify({"error": "Файл більше недоступний"}), 404
    mime = mimetypes.guess_type(row["name"])[0] or "application/octet-stream"
    resp = send_file(p, mimetype=mime, conditional=True, download_name=row["name"])
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@bp.route("/api/chat/files")
def api_chat_files():
    """Пошук файлу архіву для надсилання в чат (за назвою; без запиту — найновіші)."""
    from core.storage_api import is_service_path
    uid = current_user_id()
    term = (flask_request.args.get("q") or "").strip()
    db = get_db()
    try:
        if term:
            rows = qq(db, """SELECT id, name, ext, rel_path FROM files WHERE name LIKE %s
                             ORDER BY id DESC LIMIT 60""", (f"%{term}%",))
        else:
            rows = qq(db, "SELECT id, name, ext, rel_path FROM files ORDER BY id DESC LIMIT 60")
    finally:
        db.close()
    hidden = _hidden_for(uid)
    out = [{"id": r["id"], "name": r["name"], "ext": (r["ext"] or "").lower(), "folder": r["rel_path"].rsplit("/", 1)[0] if "/" in r["rel_path"] else ""}
           for r in rows if not is_service_path(r["rel_path"])
           and not ("/" in r["rel_path"] and r["rel_path"].split("/", 1)[0] in hidden)]
    return jsonify(out[:40])


# ── Видалення розмови ────────────────────────────────────────────────

@bp.route("/api/chat/conversation", methods=["DELETE"])
def api_chat_delete_conversation():
    """Прибрати розмову в себе (у співрозмовника вона лишається)."""
    uid = current_user_id()
    a = flask_request.args
    where, args = ["owner_user_id=%s"], [uid]
    if a.get("peer_url"):
        where.append("peer_url=%s AND peer_identity=%s"); args += [a["peer_url"], a.get("peer_identity") or ""]
    elif a.get("peer_local_user_id"):
        where.append("peer_local_user_id=%s"); args.append(int(a["peer_local_user_id"]))
    else:
        return jsonify({"error": "Вкажи співрозмовника"}), 400
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(f"DELETE FROM chat_messages WHERE {' AND '.join(where)}", args)
            n = cur.rowcount
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "deleted": n})


# ── Нотатки ──────────────────────────────────────────────────────────

def _note_out(r):
    r = dict(r)
    if r.get("created_at"):
        r["created_at"] = r["created_at"].isoformat()
    try:
        r["attachment"] = json.loads(r["attachment"]) if r.get("attachment") else None
    except ValueError:
        r["attachment"] = None
    return r


@bp.route("/api/chat/notes")
def api_chat_notes():
    db = get_db()
    try:
        rows = qq(db, "SELECT * FROM chat_notes WHERE owner_user_id=%s ORDER BY created_at ASC, id ASC",
                  (current_user_id(),))
    finally:
        db.close()
    return jsonify([_note_out(r) for r in rows])


@bp.route("/api/chat/notes", methods=["POST"])
def api_chat_note_add():
    """{"message_id": 123} — зберегти повідомлення; або {"body": "...", "file_id": ...} — свій запис."""
    uid = current_user_id()
    d = flask_request.get_json(silent=True) or {}
    db = get_db()
    try:
        if d.get("message_id"):
            m = q(db, "SELECT body, attachment, peer_label, direction FROM chat_messages WHERE id=%s AND owner_user_id=%s",
                  (int(d["message_id"]), uid))
            if not m:
                return jsonify({"error": "Повідомлення не знайдено"}), 404
            body, att = m["body"], m["attachment"]
            label = ("від " if m["direction"] == "in" else "до ") + m["peer_label"]
        else:
            body = (d.get("body") or "").strip()
            att = None
            if d.get("file_id"):
                from .by_url import my_public_url
                a = make_attachment(uid, d["file_id"], my_public_url())
                if not a:
                    return jsonify({"error": "Файл недоступний"}), 404
                att = json.dumps(a, ensure_ascii=False)
            if not body and not att:
                return jsonify({"error": "Порожня нотатка"}), 400
            label = None
        with db.cursor() as cur:
            cur.execute("INSERT INTO chat_notes (owner_user_id, body, attachment, source_label) VALUES (%s,%s,%s,%s)",
                        (uid, body or "", att, label))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


@bp.route("/api/chat/notes/<int:note_id>", methods=["DELETE"])
def api_chat_note_delete(note_id):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM chat_notes WHERE id=%s AND owner_user_id=%s", (note_id, current_user_id()))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})
