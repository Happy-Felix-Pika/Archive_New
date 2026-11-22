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


UPLOAD_DIR = ".chat_uploads"
MAX_UPLOAD = 100 * 1024 * 1024


def make_upload_attachment(uid: int, upload_id, base_url: str) -> dict | None:
    """Вкладення з файлу, завантаженого з ПК (лише свого)."""
    try:
        upload_id = int(upload_id)
    except (TypeError, ValueError):
        return None
    db = get_db()
    try:
        u = q(db, "SELECT id, name FROM chat_uploads WHERE id=%s AND owner_user_id=%s", (upload_id, uid))
        if not u:
            return None
        token = secrets.token_urlsafe(24)
        with db.cursor() as cur:
            cur.execute("INSERT INTO chat_shares (token_hash, upload_id, owner_user_id) VALUES (%s,%s,%s)",
                        (_sha(token), upload_id, uid))
        db.commit()
    finally:
        db.close()
    name = u["name"]
    return {"name": name, "ext": name.rsplit(".", 1)[-1].lower() if "." in name else "",
            "url": f"{base_url.rstrip('/')}/api/external-access/chat/file/{token}"}


def attachment_from_request(uid: int, d: dict, base_url: str):
    """(attachment|None, error|None) з {file_id} або {upload_id}."""
    if d.get("upload_id"):
        a = make_upload_attachment(uid, d["upload_id"], base_url)
        return (a, None) if a else (None, "Файл недоступний")
    if d.get("file_id"):
        a = make_attachment(uid, d["file_id"], base_url)
        return (a, None) if a else (None, "Цей файл надіслати не можна")
    return None, None


@bp.route("/api/chat/upload", methods=["POST"])
def api_chat_upload():
    """Файл з ПК → службова тека сховища; повертає upload_id для надсилання."""
    from werkzeug.utils import secure_filename
    from core.files_api import local_path
    uid = current_user_id()
    f = flask_request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Файл не вибрано"}), 400
    name = os.path.basename(f.filename.replace("\\", "/"))[:200] or "file"
    rel = f"{UPLOAD_DIR}/{uid}/{secrets.token_hex(8)}_{secure_filename(name) or 'file'}"
    dest = local_path(rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    f.save(dest)
    if os.path.getsize(dest) > MAX_UPLOAD:
        os.remove(dest)
        return jsonify({"error": "Файл завеликий (до 100 МБ)"}), 413
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("INSERT INTO chat_uploads (owner_user_id, rel_path, name) VALUES (%s,%s,%s)", (uid, rel, name))
            new_id = cur.lastrowid
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "upload_id": new_id, "name": name})


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
        row = q(db, """SELECT COALESCE(f.name, u.name) AS name, COALESCE(f.rel_path, u.rel_path) AS rel_path,
                              s.upload_id
                       FROM chat_shares s
                       LEFT JOIN files f        ON f.id = s.file_id
                       LEFT JOIN chat_uploads u ON u.id = s.upload_id
                       WHERE s.token_hash=%s""", (_sha(token),))
    finally:
        db.close()
    if not row or not row["rel_path"] or (is_service_path(row["rel_path"]) and not row["upload_id"]):
        return jsonify({"error": "Файл більше недоступний"}), 404
    p = local_path(row["rel_path"])
    if not os.path.isfile(p):
        return jsonify({"error": "Файл більше недоступний"}), 404
    mime = mimetypes.guess_type(row["name"])[0] or "application/octet-stream"
    resp = send_file(p, mimetype=mime, conditional=True, download_name=row["name"])
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


PAGE = 60


def _file_out(r):
    rp = r["rel_path"]
    return {"id": r["id"], "name": r["name"], "ext": (r["ext"] or "").lower(),
            "folder": rp.rsplit("/", 1)[0] if "/" in rp else ""}


@bp.route("/api/chat/files")
def api_chat_files():
    """Вибір файлу архіву для чату.
    mode=folder&path=… — теки й файли теки; recent — найновіші; fav — улюблене;
    q=… — пошук за назвою. offset — для «ще»."""
    from core.files_api import local_path
    from core.storage_api import is_service_path
    uid = current_user_id()
    a = flask_request.args
    mode = a.get("mode") or ("search" if a.get("q") else "recent")
    try:
        offset = max(0, int(a.get("offset") or 0))
    except ValueError:
        offset = 0
    hidden = _hidden_for(uid)

    def visible(rp):
        return rp and not is_service_path(rp) and not ("/" in rp and rp.split("/", 1)[0] in hidden)

    if mode == "folder":
        path = "/".join(p for p in (a.get("path") or "").replace("\\", "/").split("/") if p and p != "..")
        if path and not visible(path + "/x"):
            return jsonify({"error": "Тека недоступна"}), 403
        try:
            entries = sorted(os.scandir(local_path(path)), key=lambda e: e.name.lower())
        except OSError:
            return jsonify({"path": path, "folders": [], "files": [], "has_more": False})
        folders = [e.name for e in entries if e.is_dir() and visible(f"{path}/{e.name}/x".lstrip("/"))]
        names = [e.name for e in entries if e.is_file() and visible(f"{path}/{e.name}".lstrip("/"))]
        page = names[offset:offset + PAGE]
        files = []
        if page:
            rels = [f"{path}/{n}".lstrip("/") for n in page]
            db = get_db()
            try:
                rows = qq(db, f"""SELECT id, name, ext, rel_path FROM files WHERE storage_id='default'
                                  AND path_hash IN ({",".join(["MD5(%s)"] * len(rels))})""", rels)
            finally:
                db.close()
            by = {r["rel_path"]: r for r in rows}
            files = [_file_out(by[r]) for r in rels if r in by]
        return jsonify({"path": path, "folders": folders if offset == 0 else [], "files": files,
                        "has_more": len(names) > offset + PAGE})

    db = get_db()
    try:
        if mode == "fav":
            rows = qq(db, """SELECT f.id, f.name, f.ext, f.rel_path FROM user_favorites uf
                             JOIN files f ON f.id = uf.download_id
                             WHERE uf.user_id=%s ORDER BY uf.created_at DESC LIMIT %s OFFSET %s""",
                      (uid, PAGE * 2, offset))
        elif mode == "search":
            rows = qq(db, """SELECT id, name, ext, rel_path FROM files WHERE name LIKE %s
                             ORDER BY id DESC LIMIT %s OFFSET %s""",
                      (f"%{(a.get('q') or '').strip()}%", PAGE * 2, offset))
        else:
            rows = qq(db, "SELECT id, name, ext, rel_path FROM files ORDER BY id DESC LIMIT %s OFFSET %s",
                      (PAGE * 2, offset))
    finally:
        db.close()
    out = [_file_out(r) for r in rows if visible(r["rel_path"])][:PAGE]
    return jsonify({"files": out, "has_more": len(rows) == PAGE * 2, "next_offset": offset + PAGE * 2})


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
            if d.get("file_id") or d.get("upload_id"):
                from .by_url import my_public_url
                a, err = attachment_from_request(uid, d, my_public_url())
                if err:
                    return jsonify({"error": err}), 404
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
