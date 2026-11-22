"""
Переписка з будь-яким архівом за його адресою — без взаємного підключення.

Як це працює:
  * мій архів кладе повідомлення собі (out) з одноразовим токеном і шле його
    на /api/external-access/chat/inbox архіву-адресата;
  * адресат, перш ніж прийняти, питає МОЮ адресу (/api/external-access/chat/verify
    з тим самим токеном), чи справді я це надіслав. Так ніхто не може
    підписатись чужим архівом: відповісти «так» може лише справжній сервер
    за тією адресою;
  * відповідь іде тим самим шляхом у зворотний бік.
Від спаму: не більше MAX_PER_HOUR повідомлень з однієї адреси на годину,
довжина до MAX_LEN, і кожен може заблокувати архів у себе.
"""

import hashlib
import ipaddress
import json
import secrets
import socket
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request as flask_request

from core.auth import current_user_id
from core.db import get_db, q

bp = Blueprint("chat_by_url", __name__)

MAX_LEN = 4000
MAX_PER_HOUR = 60
TOKEN_MINUTES = 10


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _is_private_host(host: str) -> bool:
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".lan"):
        return True
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    try:
        return all(ipaddress.ip_address(a).is_private or ipaddress.ip_address(a).is_loopback for a in addrs)
    except ValueError:
        return False


def normalize_url(raw: str) -> str | None:
    """Адреса архіву → «https://host[:port]». http — лише для локальної мережі."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        # без схеми: у локальній мережі — http, назовні — https
        host = raw.split("/")[0].rsplit(":", 1)[0] if raw.count(":") <= 1 else raw.split("/")[0]
        raw = ("http://" if _is_private_host(host) else "https://") + raw
    try:
        u = urlsplit(raw)
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    scheme = u.scheme
    if scheme == "http" and not _is_private_host(u.hostname):
        # публічний архів за проксі (Cloudflare/nginx) часто «бачить» себе як http —
        # назовні він усе одно https, тож не відкидаємо, а виправляємо схему
        scheme = "https"
    host = u.hostname + (f":{u.port}" if u.port and not (scheme == "https" and u.scheme == "http") else "")
    return f"{scheme}://{host}".lower()


def my_public_url() -> str:
    """Адреса цього архіву ззовні: з «Конфігу» (Пошта → адреса архіву) або з запиту."""
    try:
        from core.auth import get_user_settings
        base = (get_user_settings().get("mail") or {}).get("base_url")
        if base:
            return base.rstrip("/")
    except Exception:
        pass
    proto = (flask_request.headers.get("X-Forwarded-Proto") or flask_request.scheme).split(",")[0].strip()
    host = flask_request.headers.get("X-Forwarded-Host") or flask_request.host
    return normalize_url(f"{proto}://{host.split(',')[0].strip()}") or f"{proto}://{host}"


def _host_label(url: str) -> str:
    return urlsplit(url).netloc


def _my_name(db, uid) -> str:
    me = q(db, "SELECT display_name, email FROM users WHERE id=%s", (uid,))
    return (me["display_name"] or me["email"].split("@")[0]) if me else "user"


def send_by_url(uid: int, peer_url: str, peer_identity: str, body: str, attachment: dict | None = None):
    """Надіслати в архів за адресою. Повертає (ok, warning|None, справжній нік адресата|None)."""
    import requests as req
    token = secrets.token_urlsafe(24)
    db = get_db()
    try:
        name = _my_name(db, uid)
        label = f"{peer_identity or 'власник'}@{_host_label(peer_url)}"
        with db.cursor() as cur:
            cur.execute("""INSERT INTO chat_messages (owner_user_id, direction, peer_label, peer_url,
                                                      peer_identity, body, is_read, deliver_token, attachment)
                           VALUES (%s,'out',%s,%s,%s,%s,1,%s,%s)""",
                        (uid, label, peer_url, peer_identity or "", body, _sha(token),
                         json.dumps(attachment, ensure_ascii=False) if attachment else None))
        db.commit()
    finally:
        db.close()
    me_url = my_public_url()
    me_host = urlsplit(me_url).hostname or ""
    if (me_host in ("localhost", "127.0.0.1", "::1") or _is_private_host(me_host)) \
            and not _is_private_host(urlsplit(peer_url).hostname or ""):
        return True, ("Не доставлено: твій архів не знає своєї зовнішньої адреси (бачить себе як "
                      f"{me_url}). Вкажи її в «Конфіг → Пошта → Адреса архіву»"), None
    try:
        r = req.post(f"{peer_url}/api/external-access/chat/inbox",
                     json={"sender_base_url": me_url, "sender_name": name,
                           "to_nick": peer_identity or "", "body": body, "token": token,
                           **({"attachment": {k: attachment[k] for k in ("name", "ext", "url")}} if attachment else {})},
                     timeout=20)
    except Exception as e:
        return True, f"Збережено, але архів не відповідає: {e}", None
    if r.status_code == 404 and "json" not in (r.headers.get("Content-Type") or ""):
        return True, "Той архів старої версії — він ще не вміє приймати повідомлення за адресою", None
    if r.status_code != 200:
        try:
            err = r.json().get("error")
        except ValueError:
            err = None
        return True, err or f"Не доставлено (код {r.status_code})", None
    # «власнику» — тепер знаємо його нік: розмова одна в обидва боки
    real = (r.json() or {}).get("recipient") if "json" in (r.headers.get("Content-Type") or "") else None
    if real and real != (peer_identity or ""):
        db = get_db()
        try:
            with db.cursor() as cur:
                cur.execute("""UPDATE chat_messages SET peer_identity=%s, peer_label=%s
                               WHERE owner_user_id=%s AND peer_url=%s AND peer_identity=%s""",
                            (real, f"{real}@{_host_label(peer_url)}", uid, peer_url, peer_identity or ""))
            db.commit()
        finally:
            db.close()
    return True, None, real


@bp.route("/api/external-access/chat/verify")
def api_chat_verify():
    """Архів-адресат питає: «це ти надіслав?» Так — якщо токен наш і свіжий (одноразовий)."""
    token = flask_request.args.get("token") or ""
    if not token or len(token) > 200:
        return jsonify({"ok": False}), 404
    db = get_db()
    try:
        row = q(db, f"""SELECT id, owner_user_id FROM chat_messages
                        WHERE deliver_token=%s AND direction='out'
                          AND created_at > NOW() - INTERVAL {TOKEN_MINUTES} MINUTE""", (_sha(token),))
        if not row:
            return jsonify({"ok": False}), 404
        name = _my_name(db, row["owner_user_id"])
        with db.cursor() as cur:
            cur.execute("UPDATE chat_messages SET deliver_token=NULL WHERE id=%s", (row["id"],))
        db.commit()
        return jsonify({"ok": True, "sender_name": name})
    finally:
        db.close()


@bp.route("/api/external-access/chat/inbox", methods=["POST"])
def api_chat_inbox():
    """Повідомлення від будь-якого архіву — після перевірки, що воно справді звідти."""
    import requests as req
    d = flask_request.get_json(silent=True) or {}
    sender = normalize_url(d.get("sender_base_url"))
    name = (d.get("sender_name") or "").strip()[:100]
    to_nick = (d.get("to_nick") or "").strip()
    body = (d.get("body") or "").strip()
    token = d.get("token") or ""
    from .extras import clean_incoming_attachment
    att = clean_incoming_attachment(d.get("attachment"), sender) if sender else None
    if not sender:
        return jsonify({"error": "Архів-відправник не знає своєї зовнішньої адреси — "
                                 "вкажи її в «Конфіг → Пошта → Адреса архіву»"}), 400
    if not name or not (body or att) or not token:
        return jsonify({"error": "Некоректні дані"}), 400
    if len(body) > MAX_LEN:
        return jsonify({"error": f"Задовге повідомлення (до {MAX_LEN} символів)"}), 400

    db = get_db()
    try:
        if to_nick:
            rcpt = q(db, """SELECT id FROM users WHERE LOWER(display_name)=LOWER(%s)
                            OR (COALESCE(display_name,'')='' AND LOWER(SUBSTRING_INDEX(email,'@',1))=LOWER(%s))
                            ORDER BY id LIMIT 1""", (to_nick, to_nick))
        else:
            rcpt = q(db, "SELECT id FROM users ORDER BY id LIMIT 1")      # власник архіву
        if rcpt:
            rcpt["name"] = _my_name(db, rcpt["id"])
        if not rcpt:
            return jsonify({"error": f"Користувача «{to_nick}» у цьому архіві немає"}), 404
        if q(db, "SELECT 1 AS x FROM chat_blocked WHERE owner_user_id=%s AND peer_url=%s", (rcpt["id"], sender)):
            return jsonify({"error": "Отримувач заблокував повідомлення з цього архіву"}), 403
        recent = q(db, """SELECT COUNT(*) AS n FROM chat_messages
                          WHERE peer_url=%s AND direction='in' AND created_at > NOW() - INTERVAL 1 HOUR""",
                   (sender,))["n"]
        if recent >= MAX_PER_HOUR:
            return jsonify({"error": "Забагато повідомлень з цього архіву — спробуй пізніше"}), 429
    finally:
        db.close()

    # «Це справді ти?» — питаємо сам архів-відправник за його адресою
    try:
        v = req.get(f"{sender}/api/external-access/chat/verify", params={"token": token}, timeout=10)
        ok = v.status_code == 200 and v.json().get("ok") and v.json().get("sender_name") == name
    except Exception:
        ok = False
    if not ok:
        return jsonify({"error": "Не вдалося підтвердити відправника — адреса архіву-відправника недоступна або не його"}), 403

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""INSERT INTO chat_messages (owner_user_id, direction, peer_label, peer_url, peer_identity, body, attachment)
                           VALUES (%s,'in',%s,%s,%s,%s,%s)""",
                        (rcpt["id"], f"{name}@{_host_label(sender)}", sender, name, body, att))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "recipient": rcpt["name"]})


@bp.route("/api/chat/block", methods=["POST"])
def api_chat_block():
    """{"peer_url": "...", "block": true|false} — не приймати повідомлень з цього архіву."""
    d = flask_request.get_json(silent=True) or {}
    url = normalize_url(d.get("peer_url")) or (d.get("peer_url") or "").strip().lower()
    if not url:
        return jsonify({"error": "Вкажи адресу"}), 400
    db = get_db()
    try:
        with db.cursor() as cur:
            if d.get("block", True):
                cur.execute("INSERT IGNORE INTO chat_blocked (owner_user_id, peer_url) VALUES (%s,%s)",
                            (current_user_id(), url[:255]))
            else:
                cur.execute("DELETE FROM chat_blocked WHERE owner_user_id=%s AND peer_url=%s",
                            (current_user_id(), url[:255]))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})
