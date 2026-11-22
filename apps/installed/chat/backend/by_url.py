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
    """Адреса архіву → «схема://host[:port]». Без схеми: локальна мережа — http, решта — https.
    Явно вказану схему й порт не чіпаємо: архів може бути й на http://ip:порт."""
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
    host = u.hostname + (f":{u.port}" if u.port else "")
    return f"{u.scheme}://{host}".lower()


def _base_of(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}".lower()


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


def _move_peer(uid: int, old: str, new: str):
    """Архів насправді живе за іншою адресою (редирект) — переносимо розмову туди."""
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""UPDATE chat_messages SET peer_url=%s,
                                  peer_label=CONCAT(SUBSTRING_INDEX(peer_label,'@',1),'@',%s)
                           WHERE owner_user_id=%s AND peer_url=%s""",
                        (new, urlsplit(new).netloc, uid, old))
        db.commit()
    finally:
        db.close()


def send_by_url(uid: int, peer_url: str, peer_identity: str, body: str, attachment: dict | None = None):
    """Надіслати в архів за адресою. Повертає (ok, warning|None, справжній нік|None, справжня адреса)."""
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
                      f"{me_url}). Вкажи її в «Конфіг → Пошта → Адреса архіву»"), None, peer_url
    try:
        # http → https редирект на POST губить тіло — спершу дізнаємось справжню адресу
        try:
            probe = req.get(f"{peer_url}/api/external-access/chat/verify", params={"token": "-"}, timeout=10)
            real_base = _base_of(probe.url)
            if real_base != peer_url:
                _move_peer(uid, peer_url, real_base)
                peer_url = real_base
        except Exception:
            pass
        r = req.post(f"{peer_url}/api/external-access/chat/inbox",
                     json={"sender_base_url": me_url, "sender_name": name,
                           "to_nick": peer_identity or "", "body": body, "token": token,
                           **({"attachment": {k: attachment[k] for k in ("name", "ext", "url")}} if attachment else {})},
                     timeout=20)
    except Exception as e:
        return True, f"Збережено, але архів не відповідає: {e}", None, peer_url
    if r.status_code == 404 and "json" not in (r.headers.get("Content-Type") or ""):
        return True, "Той архів старої версії — він ще не вміє приймати повідомлення за адресою", None, peer_url
    if r.status_code != 200:
        try:
            err = r.json().get("error")
        except ValueError:
            err = None
        return True, err or f"Не доставлено (код {r.status_code})", None, peer_url
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
    return True, None, real, peer_url


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
        if row:
            with db.cursor() as cur:
                cur.execute("UPDATE chat_messages SET deliver_token=NULL WHERE id=%s", (row["id"],))
        else:
            row = q(db, f"""SELECT owner_user_id FROM chat_verify_tokens WHERE token_hash=%s
                            AND created_at > NOW() - INTERVAL {TOKEN_MINUTES} MINUTE""", (_sha(token),))
            if not row:
                return jsonify({"ok": False}), 404
            with db.cursor() as cur:
                cur.execute("DELETE FROM chat_verify_tokens WHERE token_hash=%s", (_sha(token),))
        db.commit()
        return jsonify({"ok": True, "sender_name": _my_name(db, row["owner_user_id"])})
    finally:
        db.close()


def _verify_sender(sender: str, token: str, name: str):
    """«Це справді ти?» — питаємо сам архів-відправник за його адресою.
    Архів за проксі може не знати своєї схеми: http → пробуємо й https;
    після редиректів беремо справжню адресу. → (ok, справжня адреса, причина)."""
    import requests as req
    candidates = [sender]
    if sender.startswith("http://"):
        candidates.append("https://" + sender[7:])
    why = ""
    for base in candidates:
        try:
            v = req.get(f"{base}/api/external-access/chat/verify", params={"token": token}, timeout=10)
            j = v.json() if "json" in (v.headers.get("Content-Type") or "") else {}
            if v.status_code == 200 and j.get("ok") and j.get("sender_name") == name:
                return True, _base_of(v.url), ""
            why = f"{base}: код {v.status_code}"
        except Exception as e:
            why = f"{base}: {type(e).__name__}"
    return False, sender, why


# ── «Прочитано» між архівами ─────────────────────────────────────────

def send_read_receipt(uid: int, peer_url: str, peer_identity: str):
    """Я відкрив розмову — кажемо архіву співрозмовника, що його повідомлення прочитані.
    Тихо й у фоні: не вийшло — ну й гаразд, лишаться «✓»."""
    import threading
    me_url = my_public_url()

    def run():
        import requests as req
        token = secrets.token_urlsafe(24)
        db = get_db()
        try:
            name = _my_name(db, uid)
            with db.cursor() as cur:
                cur.execute("INSERT INTO chat_verify_tokens (token_hash, owner_user_id) VALUES (%s,%s)",
                            (_sha(token), uid))
                cur.execute(f"DELETE FROM chat_verify_tokens WHERE created_at < NOW() - INTERVAL {TOKEN_MINUTES} MINUTE")
            db.commit()
        finally:
            db.close()
        try:
            req.post(f"{peer_url}/api/external-access/chat/read",
                     json={"reader_base_url": me_url, "reader_name": name,
                           "to_nick": peer_identity or "", "token": token}, timeout=15)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


@bp.route("/api/external-access/chat/read", methods=["POST"])
def api_chat_read_receipt():
    d = flask_request.get_json(silent=True) or {}
    reader = normalize_url(d.get("reader_base_url"))
    name = (d.get("reader_name") or "").strip()[:100]
    to_nick = (d.get("to_nick") or "").strip()
    token = d.get("token") or ""
    if not reader or not name or not token:
        return jsonify({"error": "Некоректні дані"}), 400
    ok, real, why = _verify_sender(reader, token, name)
    if not ok:
        return jsonify({"error": f"Не підтверджено ({why})"}), 403
    host = urlsplit(real).netloc
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""UPDATE chat_messages m JOIN users u ON u.id = m.owner_user_id
                           SET m.read_at = NOW()
                           WHERE m.direction='out' AND m.read_at IS NULL AND m.peer_identity=%s
                             AND SUBSTRING_INDEX(m.peer_url,'://',-1)=%s
                             AND (LOWER(u.display_name)=LOWER(%s)
                                  OR (COALESCE(u.display_name,'')='' AND LOWER(SUBSTRING_INDEX(u.email,'@',1))=LOWER(%s)))""",
                        (name, host, to_nick, to_nick))
            n = cur.rowcount
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True, "marked": n})


@bp.route("/api/external-access/chat/inbox", methods=["POST"])
def api_chat_inbox():
    """Повідомлення від будь-якого архіву — після перевірки, що воно справді звідти."""
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

    ok, real, why = _verify_sender(sender, token, name)
    if ok:
        sender = real
    if not ok:
        print(f"  ✗ Чат: не підтверджено відправника {name} ({why})")
        return jsonify({"error": f"Не вдалося підтвердити відправника — його архів недоступний з боку адресата ({why}). "
                                 "Перевір «Конфіг → Пошта → Адреса архіву» у відправника"}), 403

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
