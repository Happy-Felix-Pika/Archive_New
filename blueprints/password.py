"""
Паролі: відновлення забутого, зміна свого, новий пароль від адміна.

Відновлення («Забули пароль?» на екрані входу):
  * якщо в «Конфігу» налаштована пошта — на адресу акаунта йде лист з
    одноразовим посиланням (діє 1 годину);
  * якщо пошти немає (або лист не відправився) — запит потрапляє до
    адміна у вкладку користувачів, і адмін задає новий пароль сам.
Відповідь на запит однакова, чи є такий акаунт, чи ні — щоб форму не
можна було використати для перевірки, хто зареєстрований.
"""

import hashlib
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from flask import Blueprint, jsonify, request as flask_request, session
from werkzeug.security import check_password_hash, generate_password_hash

from core.auth import OWNER_ID, admin_required, current_user_id, get_user_settings, is_owner, login_required
from core.config import load_config
from core.db import get_db, q, qq

bp = Blueprint("password", __name__)

TOKEN_HOURS = 1
MAX_PER_HOUR = 3            # стільки запитів на одну адресу за годину, далі — мовчки ігноруємо
MIN_LEN = 8


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _mail_cfg() -> dict:
    m = get_user_settings().get("mail") or {}
    return m if m.get("host") and m.get("from_addr") else {}


def _client_ip():
    return (flask_request.headers.get("CF-Connecting-IP")
            or (flask_request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            or flask_request.remote_addr or "")[:64]


def _base_url() -> str:
    m = get_user_settings().get("mail") or {}
    if m.get("base_url"):
        return m["base_url"].rstrip("/")
    proto = flask_request.headers.get("X-Forwarded-Proto") or flask_request.scheme
    return f"{proto}://{flask_request.host}"


def _title() -> str:
    try:
        return (load_config().get("ui") or {}).get("title") or "Archive"
    except Exception:
        return "Archive"


def send_mail(to_addr: str, subject: str, body: str, cfg: dict | None = None):
    """Лист через SMTP з «Конфігу». Помилка — виняток (хай вирішує той, хто кличе)."""
    cfg = cfg or _mail_cfg()
    if not cfg:
        raise RuntimeError("Пошта не налаштована")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg.get("from_name") or _title(), cfg["from_addr"]))
    msg["To"] = to_addr
    msg.set_content(body)
    port = int(cfg.get("port") or 587)
    security = cfg.get("security") or ("ssl" if port == 465 else "starttls")
    ctx = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(cfg["host"], port, timeout=20, context=ctx)
    else:
        server = smtplib.SMTP(cfg["host"], port, timeout=20)
    try:
        if security == "starttls":
            server.starttls(context=ctx)
        if cfg.get("user"):
            server.login(cfg["user"], cfg.get("password") or "")
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def _check_new_password(pwd) -> str | None:
    if not isinstance(pwd, str) or len(pwd) < MIN_LEN:
        return f"Пароль має містити щонайменше {MIN_LEN} символів"
    return None


def _set_password(db, user_id: int, pwd: str):
    with db.cursor() as cur:
        cur.execute("UPDATE users SET password_hash=%s WHERE id=%s", (generate_password_hash(pwd), user_id))


# ── «Забули пароль?» ─────────────────────────────────────────────────

@bp.route("/api/auth/forgot", methods=["POST"])
def api_forgot():
    body = flask_request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "Вкажіть коректний email"}), 400
    cfg = _mail_cfg()
    mode = "email" if cfg else "admin"
    db = get_db()
    try:
        user = q(db, "SELECT id, email, display_name FROM users WHERE email=%s", (email,))
        if user:
            recent = q(db, """SELECT COUNT(*) AS n FROM password_resets
                              WHERE user_id=%s AND created_at > NOW() - INTERVAL 1 HOUR""", (user["id"],))["n"]
            if recent < MAX_PER_HOUR:
                sent = False
                if cfg:
                    token = secrets.token_urlsafe(32)
                    link = f"{_base_url()}/?reset={token}"
                    try:
                        send_mail(user["email"], f"{_title()}: відновлення пароля",
                                  f"Привіт, {user['display_name'] or user['email']}!\n\n"
                                  f"Хтось (сподіваємось, ти) попросив скинути пароль до архіву «{_title()}».\n"
                                  f"Щоб задати новий пароль, відкрий посилання (діє {TOKEN_HOURS} год.):\n\n"
                                  f"{link}\n\n"
                                  f"Якщо це був не ти — просто нічого не роби, пароль лишиться старим.\n", cfg)
                        sent = True
                    except Exception as e:
                        print(f"  ⚠ Відновлення пароля: лист на {user['email']} не надіслано: {e}")
                with db.cursor() as cur:
                    if sent:
                        cur.execute("""INSERT INTO password_resets (user_id, token_hash, status, ip, expires_at)
                                       VALUES (%s,%s,'emailed',%s, NOW() + INTERVAL %s HOUR)""",
                                    (user["id"], _sha(token), _client_ip(), TOKEN_HOURS))
                    else:
                        # пошти немає або лист не пішов — запит чекає адміна
                        cur.execute("""INSERT INTO password_resets (user_id, status, ip)
                                       VALUES (%s,'pending',%s)""", (user["id"], _client_ip()))
                db.commit()
    finally:
        db.close()
    if mode == "email":
        msg = ("Якщо такий акаунт є, на його пошту надіслано лист із посиланням для нового пароля. "
               "Перевір і папку «Спам». Якщо лист так і не прийде — звернись до адміністратора архіву.")
    else:
        msg = ("Запит надіслано адміністратору архіву. Коли він задасть новий пароль — "
               "він повідомить його тобі.")
    return jsonify({"ok": True, "mode": mode, "message": msg})


def _valid_token_row(db, token):
    if not token or len(token) > 200:
        return None
    return q(db, """SELECT r.id, r.user_id, u.email FROM password_resets r
                    JOIN users u ON u.id = r.user_id
                    WHERE r.token_hash=%s AND r.status='emailed' AND r.expires_at > NOW()""", (_sha(token),))


@bp.route("/api/auth/reset/check")
def api_reset_check():
    db = get_db()
    try:
        row = _valid_token_row(db, flask_request.args.get("token") or "")
    finally:
        db.close()
    if not row:
        return jsonify({"ok": False, "error": "Посилання недійсне або вже використане. Попроси нове — «Забули пароль?»"}), 400
    e = row["email"]
    name, _, dom = e.partition("@")
    masked = (name[:2] + "•••@" + dom) if dom else e
    return jsonify({"ok": True, "email": masked})


@bp.route("/api/auth/reset", methods=["POST"])
def api_reset():
    body = flask_request.get_json(silent=True) or {}
    err = _check_new_password(body.get("password"))
    if err:
        return jsonify({"error": err}), 400
    db = get_db()
    try:
        row = _valid_token_row(db, body.get("token") or "")
        if not row:
            return jsonify({"error": "Посилання недійсне або вже використане. Попроси нове — «Забули пароль?»"}), 400
        _set_password(db, row["user_id"], body["password"])
        with db.cursor() as cur:
            # усі незакриті запити цього акаунта — закриті
            cur.execute("""UPDATE password_resets SET status='done', resolved_at=NOW()
                           WHERE user_id=%s AND status IN ('pending','emailed')""", (row["user_id"],))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


# ── Свій пароль ──────────────────────────────────────────────────────

@bp.route("/api/auth/change-password", methods=["POST"])
@login_required
def api_change_password():
    body = flask_request.get_json(silent=True) or {}
    err = _check_new_password(body.get("new_password"))
    if err:
        return jsonify({"error": err}), 400
    db = get_db()
    try:
        u = q(db, "SELECT password_hash FROM users WHERE id=%s", (session["user_id"],))
        if not u or not check_password_hash(u["password_hash"], body.get("old_password") or ""):
            return jsonify({"error": "Поточний пароль невірний"}), 403
        _set_password(db, session["user_id"], body["new_password"])
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


# ── Адмін ────────────────────────────────────────────────────────────

def _may_reset(target_id: int) -> str | None:
    """Хто кому може задати пароль: як і з видаленням — адмінам лише Творець."""
    if target_id == OWNER_ID and current_user_id() != OWNER_ID:
        return "Пароль Творця може змінити лише він сам"
    db = get_db()
    try:
        row = q(db, "SELECT is_admin FROM users WHERE id=%s", (target_id,))
    finally:
        db.close()
    if not row:
        return "Користувача не знайдено"
    if row["is_admin"] and target_id != current_user_id() and not is_owner():
        return "Пароль іншого адміна може змінити лише Творець"
    return None


def _admin_set(target_id: int, pwd: str, request_id: int | None = None):
    db = get_db()
    try:
        _set_password(db, target_id, pwd)
        with db.cursor() as cur:
            cur.execute("""UPDATE password_resets SET status='done', resolved_by=%s, resolved_at=NOW()
                           WHERE user_id=%s AND status IN ('pending','emailed')""",
                        (current_user_id(), target_id))
        db.commit()
    finally:
        db.close()


@bp.route("/api/users/<int:user_id>/password", methods=["POST"])
@admin_required
def api_admin_set_password(user_id):
    body = flask_request.get_json(silent=True) or {}
    err = _may_reset(user_id) or _check_new_password(body.get("password"))
    if err:
        return jsonify({"error": err}), 400 if "символів" in err else 403
    _admin_set(user_id, body["password"])
    return jsonify({"ok": True})


@bp.route("/api/password-requests")
@admin_required
def api_password_requests():
    """Запити, що чекають адміна (пошта не налаштована або лист не пішов)."""
    db = get_db()
    try:
        rows = qq(db, """SELECT r.id, r.user_id, u.email, u.display_name, r.created_at, r.ip
                         FROM password_resets r JOIN users u ON u.id = r.user_id
                         WHERE r.status='pending' ORDER BY r.created_at DESC""")
    finally:
        db.close()
    seen, out = set(), []
    for r in rows:                      # кілька запитів від одного — показуємо останній
        if r["user_id"] in seen:
            continue
        seen.add(r["user_id"])
        r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
        out.append(r)
    return jsonify(out)


@bp.route("/api/password-requests/<int:req_id>", methods=["POST"])
@admin_required
def api_password_request_resolve(req_id):
    """{"action": "set", "password": "..."} або {"action": "reject"}."""
    body = flask_request.get_json(silent=True) or {}
    db = get_db()
    try:
        row = q(db, "SELECT user_id FROM password_resets WHERE id=%s AND status='pending'", (req_id,))
    finally:
        db.close()
    if not row:
        return jsonify({"error": "Запит уже розглянуто"}), 404
    if body.get("action") == "reject":
        db = get_db()
        try:
            with db.cursor() as cur:
                cur.execute("""UPDATE password_resets SET status='rejected', resolved_by=%s, resolved_at=NOW()
                               WHERE user_id=%s AND status='pending'""", (current_user_id(), row["user_id"]))
            db.commit()
        finally:
            db.close()
        return jsonify({"ok": True})
    err = _may_reset(row["user_id"]) or _check_new_password(body.get("password"))
    if err:
        return jsonify({"error": err}), 400
    _admin_set(row["user_id"], body["password"], req_id)
    return jsonify({"ok": True})


@bp.route("/api/mail/test", methods=["POST"])
@admin_required
def api_mail_test():
    """Тестовий лист на адресу поточного адміна — перевірити налаштування пошти."""
    db = get_db()
    try:
        me = q(db, "SELECT email FROM users WHERE id=%s", (current_user_id(),))
    finally:
        db.close()
    try:
        send_mail(me["email"], f"{_title()}: тестовий лист",
                  "Пошта архіву налаштована правильно — листи для відновлення пароля доходитимуть.\n")
    except Exception as e:
        return jsonify({"error": f"Не вдалося надіслати: {e}"}), 400
    return jsonify({"ok": True, "to": me["email"]})
