"""
Telegram — канали як джерело архіву.

Кожен користувач архіву входить СВОЇМ акаунтом Telegram (api_id/api_hash
з my.telegram.org + код з Telegram, за потреби — хмарний пароль).
Канал (публічний чи закритий, де користувач учасник) привʼязується до
автора: наявного або нового. Далі архіватор — під час звичайного запуску,
«Перевірити зараз» чи щоденного автооновлення — бере з каналу нові пости
тим самим шляхом, що й з e621/Inkbunny: MD5, pHash, «На перевірку», кошик.

Адреси: /api/apps/telegram/...
"""

import mimetypes
import threading
import time

from flask import Blueprint, jsonify, request, send_file

from . import online, tgcore

bp = Blueprint("telegram", __name__)
core = None

# Незавершені входи: user_id -> {session, phone_code_hash, api_id, api_hash, phone, t}.
# У памʼяті процесу: код живе кілька хвилин, після перезапуску — просто
# надіслати код ще раз.
_pending_login = {}
_pending_lock = threading.Lock()
LOGIN_TTL = 15 * 60


def _uid():
    return core.current_user_id()


def _is_admin():
    u = core.current_user() or {}
    return bool(u.get("is_admin"))


def _err(msg, code=400):
    return jsonify({"error": msg}), code


def _account(db, user_id):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM telegram_accounts WHERE user_id=%s", (user_id,))
        return cur.fetchone()


def _creds_for_user(user_id):
    """Розшифрований доступ користувача — для довгоживучих клієнтів «Онлайн»."""
    db = core._db()
    try:
        return _creds(_account(db, user_id))
    finally:
        db.close()


_pool = online.ClientPool(_creds_for_user)


def _creds(row):
    """(api_id, api_hash, session) розшифровані — або TgError."""
    if not row or not row.get("session_enc"):
        raise tgcore.TgError("Спершу підключи свій акаунт Telegram")
    try:
        return row["api_id"], tgcore.decrypt(row["api_hash_enc"]), tgcore.decrypt(row["session_enc"])
    except Exception:
        raise tgcore.TgError("Не вдалося розшифрувати сесію (змінився секрет сервера?) — увійди ще раз")


# ── Акаунт ────────────────────────────────────────────────────────────

@bp.route("/account")
def account_get():
    db = core.db
    try:
        row = _account(db, _uid())
    finally:
        db.close()
    with _pending_lock:
        waiting = _pending_login.get(_uid())
    return jsonify({
        "connected": bool(row and row.get("session_enc")),
        "api_id": row["api_id"] if row else None,
        "phone": row["phone"] if row else None,
        "tg_name": row["tg_name"] if row else None,
        "awaiting": (waiting or {}).get("stage"),
    })


@bp.route("/account/start", methods=["POST"])
def account_start():
    """Крок 1: api_id, api_hash, телефон → Telegram надсилає код."""
    body = request.get_json(silent=True) or {}
    try:
        api_id = int(str(body.get("api_id") or "").strip())
    except ValueError:
        return _err("api_id — це число з my.telegram.org")
    api_hash = (body.get("api_hash") or "").strip()
    phone = (body.get("phone") or "").replace(" ", "").strip()
    if not api_hash:
        # можна не вводити повторно, якщо вже збережений
        db = core.db
        try:
            row = _account(db, _uid())
        finally:
            db.close()
        if row and row["api_id"] == api_id:
            api_hash = tgcore.decrypt(row["api_hash_enc"])
    if not api_hash or not phone:
        return _err("Потрібні api_id, api_hash і номер телефону")
    try:
        session, code_hash, sent = tgcore.run_sync(tgcore.login_start, api_id, api_hash, phone)
    except Exception as e:
        return _err(f"Telegram не надіслав код: {tgcore.explain_error(e)}")
    return _code_sent(api_id, api_hash, phone, session, code_hash, sent)


def _code_sent(api_id, api_hash, phone, session, code_hash, sent):
    if sent.get("payment_required"):
        return _err("Telegram вимагає оплату за SMS для входу з нового застосунку. "
                    "Увійди спершу в офіційний застосунок Telegram на цьому номері — тоді код прийде туди.")
    if sent.get("email_setup_required"):
        return _err("Telegram вимагає спершу привʼязати пошту для входу. Відкрий офіційний застосунок "
                    "Telegram → Налаштування → Конфіденційність → Пошта для входу, додай пошту і спробуй знову.")
    with _pending_lock:
        _pending_login[_uid()] = {"session": session, "phone_code_hash": code_hash, "api_id": api_id,
                                  "api_hash": api_hash, "phone": phone, "stage": "code", "t": time.time()}
    return jsonify({"ok": True, "stage": "code", "where": sent.get("where"),
                    "next": sent.get("next"), "timeout": sent.get("timeout")})


@bp.route("/account/resend", methods=["POST"])
def account_resend():
    """Код не прийшов — попросити інший спосіб (SMS, дзвінок), якщо Telegram дозволяє."""
    with _pending_lock:
        p = _pending_login.get(_uid())
    if not p or time.time() - p["t"] > LOGIN_TTL:
        return _err("Спершу натисни «Надіслати код»")
    try:
        session, code_hash, sent = tgcore.run_sync(tgcore.login_resend, p["api_id"], p["api_hash"],
                                                   p["session"], p["phone"], p["phone_code_hash"])
    except Exception as e:
        return _err(f"Не вдалося надіслати ще раз: {tgcore.explain_error(e)}")
    return _code_sent(p["api_id"], p["api_hash"], p["phone"], session, code_hash, sent)


@bp.route("/account/verify", methods=["POST"])
def account_verify():
    """Крок 2: код з Telegram (і, якщо попросить, хмарний пароль)."""
    body = request.get_json(silent=True) or {}
    with _pending_lock:
        p = _pending_login.get(_uid())
    if not p or time.time() - p["t"] > LOGIN_TTL:
        return _err("Код застарів — надішли ще раз")
    code = (body.get("code") or "").strip()
    password = body.get("password") or None
    if p["stage"] == "code" and not code:
        return _err("Введи код з Telegram")
    if p["stage"] == "password" and not password:
        return _err("Введи хмарний пароль Telegram")
    try:
        status, session, name = tgcore.run_sync(
            tgcore.login_finish, p["api_id"], p["api_hash"], p["session"], p["phone"],
            p["phone_code_hash"], code=code if p["stage"] == "code" else None,
            password=password if p["stage"] == "password" else None)
    except Exception as e:
        return _err(f"Не вдалося увійти: {tgcore.explain_error(e)}")
    if status == "password":
        with _pending_lock:
            p.update(session=session, stage="password", t=time.time())
        return jsonify({"ok": True, "stage": "password"})

    db = core.db
    try:
        with db.cursor() as cur:
            cur.execute("""INSERT INTO telegram_accounts (user_id, api_id, api_hash_enc, session_enc, phone, tg_name)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           ON DUPLICATE KEY UPDATE api_id=VALUES(api_id), api_hash_enc=VALUES(api_hash_enc),
                             session_enc=VALUES(session_enc), phone=VALUES(phone), tg_name=VALUES(tg_name)""",
                        (_uid(), p["api_id"], tgcore.encrypt(p["api_hash"]), tgcore.encrypt(session),
                         p["phone"], name))
        db.commit()
    finally:
        db.close()
    with _pending_lock:
        _pending_login.pop(_uid(), None)
    _pool.drop(_uid())                 # «Онлайн» підхопить нову сесію
    return jsonify({"ok": True, "stage": "done", "tg_name": name})


@bp.route("/account", methods=["DELETE"])
def account_delete():
    """Вийти: сесію закриваємо в самому Telegram і стираємо в архіві. Канали лишаються."""
    db = core.db
    try:
        row = _account(db, _uid())
        if row and row.get("session_enc"):
            try:
                api_id, api_hash, session = _creds(row)
                tgcore.run_sync(tgcore.logout, api_id, api_hash, session)
            except Exception as e:
                print(f"  ⚠ telegram: вихід з акаунта не вдався ({e}) — сесію все одно стерто")
        with db.cursor() as cur:
            cur.execute("UPDATE telegram_accounts SET session_enc=NULL, tg_name=NULL WHERE user_id=%s", (_uid(),))
        db.commit()
    finally:
        db.close()
    with _pending_lock:
        _pending_login.pop(_uid(), None)
    _pool.drop(_uid())
    return jsonify({"ok": True})


# ── Вхід за QR-кодом ──────────────────────────────────────────────────
#
#  Коли Telegram не доставляє код (часто при вході з сервера: код «надіслано
#  в застосунок», але він так і не приходить), найнадійніше — QR, як у
#  Telegram Desktop: телефон, де ти вже увійшов, сканує код з екрана
#  (Налаштування → Пристрої → Підключити пристрій). Код не потрібен зовсім.
#
#  Клієнт живе у фоновому циклі «Онлайн» (_pool): він мусить чекати, поки
#  QR відскановано, а QR оновлюється кожні ~30 с.

import asyncio as _asyncio

_qr = {}            # user_id -> {state, url, client, error, tg_name, api_id, api_hash, t}
QR_TOTAL = 5 * 60


def _save_account(user_id, api_id, api_hash, session, phone, name):
    db = core._db()
    try:
        with db.cursor() as cur:
            cur.execute("""INSERT INTO telegram_accounts (user_id, api_id, api_hash_enc, session_enc, phone, tg_name)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           ON DUPLICATE KEY UPDATE api_id=VALUES(api_id), api_hash_enc=VALUES(api_hash_enc),
                             session_enc=VALUES(session_enc), phone=VALUES(phone), tg_name=VALUES(tg_name)""",
                        (user_id, api_id, tgcore.encrypt(api_hash), tgcore.encrypt(session), phone, name))
        db.commit()
    finally:
        db.close()


async def _qr_done(uid):
    st = _qr[uid]
    client = st["client"]
    me = await client.get_me()
    name = ("@" + me.username) if getattr(me, "username", None) else \
        " ".join(p for p in [me.first_name, me.last_name] if p)
    phone = ("+" + me.phone) if getattr(me, "phone", None) else None
    _save_account(uid, st["api_id"], st["api_hash"], client.session.save(), phone, name)
    st.update(state="done", tg_name=name, url=None)
    await client.disconnect()
    _pool.drop(uid)


async def _qr_flow(uid):
    from telethon.errors import SessionPasswordNeededError
    st = _qr[uid]
    try:
        client = tgcore.make_client(st["api_id"], st["api_hash"])
        st["client"] = client
        await client.connect()
        qr = await client.qr_login()
        st.update(state="waiting", url=qr.url)
        deadline = time.time() + QR_TOTAL
        while True:
            if st.get("cancel"):
                raise _asyncio.CancelledError()
            try:
                await qr.wait(timeout=20)
                break
            except _asyncio.TimeoutError:
                if time.time() > deadline:
                    st.update(state="expired", url=None)
                    await client.disconnect()
                    return
                await qr.recreate()
                st["url"] = qr.url
            except SessionPasswordNeededError:
                st.update(state="password", url=None)
                return                       # клієнт лишається підключеним до пароля
        await _qr_done(uid)
    except _asyncio.CancelledError:
        st.update(state="cancelled", url=None)
        try: await st["client"].disconnect()
        except Exception: pass
    except Exception as e:
        st.update(state="error", url=None, error=tgcore.explain_error(e))
        try: await st["client"].disconnect()
        except Exception: pass


@bp.route("/account/qr/start", methods=["POST"])
def account_qr_start():
    body = request.get_json(silent=True) or {}
    try:
        api_id = int(str(body.get("api_id") or "").strip())
    except ValueError:
        return _err("api_id — це число з my.telegram.org")
    api_hash = (body.get("api_hash") or "").strip()
    if not api_hash:
        db = core.db
        try:
            row = _account(db, _uid())
        finally:
            db.close()
        if row and row["api_id"] == api_id:
            api_hash = tgcore.decrypt(row["api_hash_enc"])
    if not api_hash:
        return _err("Потрібні api_id і api_hash")
    old = _qr.get(_uid())
    if old and old.get("state") in ("starting", "waiting", "password"):
        old["cancel"] = True
    uid = _uid()
    _qr[uid] = {"state": "starting", "api_id": api_id, "api_hash": api_hash, "t": time.time()}
    _asyncio.run_coroutine_threadsafe(_qr_flow(uid), _pool._ensure_loop())
    return jsonify({"ok": True, "state": "starting"})


@bp.route("/account/qr/status")
def account_qr_status():
    st = _qr.get(_uid()) or {}
    return jsonify({"state": st.get("state", "none"), "has_qr": bool(st.get("url")),
                    "error": st.get("error"), "tg_name": st.get("tg_name"), "url": st.get("url")})


@bp.route("/account/qr.png")
def account_qr_png():
    st = _qr.get(_uid()) or {}
    if not st.get("url"):
        return _err("QR ще немає", 404)
    import io
    import qrcode
    img = qrcode.make(st["url"], box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/account/qr/password", methods=["POST"])
def account_qr_password():
    st = _qr.get(_uid()) or {}
    if st.get("state") != "password":
        return _err("Хмарний пароль зараз не потрібен")
    pw = (request.get_json(silent=True) or {}).get("password") or ""
    if not pw:
        return _err("Введи хмарний пароль")

    async def go():
        await st["client"].sign_in(password=pw)
        await _qr_done(_uid_val)
    _uid_val = _uid()
    try:
        _pool.run(go(), timeout=60)
    except Exception as e:
        return _err(f"Не вдалося увійти: {tgcore.explain_error(e)}")
    return jsonify({"ok": True, "state": "done", "tg_name": st.get("tg_name")})


# ── Файли для вкладки «Онлайн» ────────────────────────────────────────

@bp.route("/media/<int:cid>/<int:mid>")
def media(cid, mid):
    """
    Превʼю (?thumb=1) або сам файл повідомлення — для «Онлайн». Скачується
    акаунтом того, хто дивиться, і кешується на диску окремо для нього.
    """
    thumb = request.args.get("thumb") == "1"
    try:
        path, ext = _pool.run(online.fetch_media(_pool, _uid(), cid, mid, thumb))
    except tgcore.TgError as e:
        return _err(str(e), 404)
    except Exception as e:
        return _err(f"Telegram: {e}", 502)
    mime = mimetypes.guess_type(f"x.{ext}")[0] or "application/octet-stream"
    return send_file(path, mimetype=mime, conditional=True, max_age=86400)


# ── Автори для вибору ─────────────────────────────────────────────────

@bp.route("/artists")
def artists_list():
    db = core.db
    try:
        with db.cursor() as cur:
            cur.execute("SELECT id, artist_name, folder_name FROM artists ORDER BY artist_name")
            return jsonify(cur.fetchall())
    finally:
        db.close()


def _artist_for(db, body):
    """
    Автор з форми: наявний (artist_id) або новий за назвою (artist_name).
    Новий отримує теку «@назва»; якщо така тека вже є — береться той автор.
    """
    aid = body.get("artist_id")
    if aid:
        with db.cursor() as cur:
            cur.execute("SELECT id FROM artists WHERE id=%s", (int(aid),))
            if not cur.fetchone():
                raise tgcore.TgError("Такого автора немає")
        return int(aid)
    name = (body.get("artist_name") or "").strip().lstrip("@").strip()
    if not name:
        raise tgcore.TgError("Обери автора або впиши назву нового")
    if any(c in name for c in '/\\:*?"<>|') or name in (".", ".."):
        raise tgcore.TgError("Назва автора не може містити / \\ : * ? \" < > |")
    folder = f"@{name}"
    with db.cursor() as cur:
        cur.execute("SELECT id FROM artists WHERE folder_name=%s OR artist_name=%s "
                    "ORDER BY (folder_name=%s) DESC LIMIT 1", (folder, name, folder))
        row = cur.fetchone()
        if row:
            return row["id"]
        cur.execute("INSERT INTO artists (folder_name, artist_name) VALUES (%s,%s)", (folder, name))
        return cur.lastrowid


def _set_auto_update(db, artist_id, on):
    """Галочка «автооновлення» автора — та сама, що у вкладці «Автори»."""
    try:
        with db.cursor() as cur:
            cur.execute("UPDATE artists SET auto_update=%s WHERE id=%s", (1 if on else 0, artist_id))
    except Exception as e:
        print(f"  ⚠ telegram: не вдалося змінити автооновлення автора {artist_id}: {e}")


# ── Канали ────────────────────────────────────────────────────────────

_CHANNEL_SQL = """
    SELECT c.id, c.user_id, c.channel_ref, c.channel_id, c.username, c.title, c.artist_id,
           c.only_telegram, c.enabled, c.last_message_id, c.last_checked_at, c.last_error,
           a.artist_name, a.folder_name, a.auto_update,
           (SELECT COUNT(*) FROM furry_posts fp
             WHERE fp.artist_id = c.artist_id AND fp.source = 'telegram'
               AND fp.post_id LIKE CONCAT('tg\\_', c.channel_id, '\\_%%')) AS files
    FROM telegram_channels c JOIN artists a ON a.id = c.artist_id
"""


def _channel_out(r):
    r = dict(r)
    if r.get("last_checked_at"):
        r["last_checked_at"] = r["last_checked_at"].isoformat()
    r["only_telegram"] = bool(r["only_telegram"])
    r["enabled"] = bool(r["enabled"])
    r["auto_update"] = bool(r.get("auto_update"))
    return r


def _own_channel(db, cid):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM telegram_channels WHERE id=%s", (cid,))
        row = cur.fetchone()
    if not row or (row["user_id"] != _uid() and not _is_admin()):
        return None
    return row


@bp.route("/channels")
def channels_list():
    """Свої канали; адмін бачить усі (з позначкою, чий акаунт їх читає)."""
    db = core.db
    try:
        with db.cursor() as cur:
            if _is_admin():
                cur.execute(_CHANNEL_SQL + " ORDER BY a.artist_name, c.title")
            else:
                cur.execute(_CHANNEL_SQL + " WHERE c.user_id=%s ORDER BY a.artist_name, c.title", (_uid(),))
            rows = [_channel_out(r) for r in cur.fetchall()]
    finally:
        db.close()
    me = _uid()
    for r in rows:
        r["mine"] = r["user_id"] == me
    return jsonify({"items": rows, "is_admin": _is_admin()})


@bp.route("/channels", methods=["POST"])
def channels_add():
    body = request.get_json(silent=True) or {}
    raw = (body.get("ref") or "").strip()
    db = core.db
    try:
        api_id, api_hash, session = _creds(_account(db, _uid()))
        ref, info = tgcore.run_sync(tgcore.resolve, api_id, api_hash, session, raw)
        with db.cursor() as cur:
            cur.execute("SELECT id FROM telegram_channels WHERE user_id=%s AND channel_id=%s",
                        (_uid(), info["channel_id"]))
            if cur.fetchone():
                raise tgcore.TgError(f"Канал «{info['title']}» уже додано")
        artist_id = _artist_for(db, body)
        with db.cursor() as cur:
            cur.execute("""INSERT INTO telegram_channels
                           (user_id, channel_ref, channel_id, username, title, artist_id, only_telegram)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (_uid(), raw[:255], info["channel_id"], info["username"], info["title"],
                         artist_id, 1 if body.get("only_telegram") else 0))
            new_id = cur.lastrowid
        if "auto_update" in body:
            _set_auto_update(db, artist_id, bool(body.get("auto_update")))
        db.commit()
        with db.cursor() as cur:
            cur.execute(_CHANNEL_SQL + " WHERE c.id=%s", (new_id,))
            return jsonify({"ok": True, "channel": _channel_out(cur.fetchone())})
    except (tgcore.TgError, ValueError) as e:
        db.rollback()
        return _err(str(e))
    except Exception as e:
        db.rollback()
        return _err(f"Помилка: {e}", 500)
    finally:
        db.close()


@bp.route("/channels/<int:cid>", methods=["PATCH"])
def channels_update(cid):
    """Змінити автора (наявний/новий), «тільки Telegram», увімкнено, автооновлення."""
    body = request.get_json(silent=True) or {}
    db = core.db
    try:
        row = _own_channel(db, cid)
        if not row:
            return _err("Канал не знайдено", 404)
        sets, args = [], []
        if body.get("artist_id") or body.get("artist_name"):
            sets.append("artist_id=%s")
            args.append(_artist_for(db, body))
        if "only_telegram" in body:
            sets.append("only_telegram=%s"); args.append(1 if body["only_telegram"] else 0)
        if "enabled" in body:
            sets.append("enabled=%s"); args.append(1 if body["enabled"] else 0)
        if body.get("rescan"):
            # переглянути канал з початку: уже скачане пропуститься за post_id
            sets.append("last_message_id=0")
        if sets:
            with db.cursor() as cur:
                cur.execute(f"UPDATE telegram_channels SET {', '.join(sets)} WHERE id=%s", args + [cid])
        if "auto_update" in body:
            with db.cursor() as cur:
                cur.execute("SELECT artist_id FROM telegram_channels WHERE id=%s", (cid,))
                _set_auto_update(db, cur.fetchone()["artist_id"], bool(body["auto_update"]))
        db.commit()
        with db.cursor() as cur:
            cur.execute(_CHANNEL_SQL + " WHERE c.id=%s", (cid,))
            return jsonify({"ok": True, "channel": _channel_out(cur.fetchone())})
    except (tgcore.TgError, ValueError) as e:
        db.rollback()
        return _err(str(e))
    finally:
        db.close()


@bp.route("/channels/<int:cid>", methods=["DELETE"])
def channels_delete(cid):
    """Відвʼязати канал. Уже скачані файли лишаються в архіві в автора."""
    db = core.db
    try:
        if not _own_channel(db, cid):
            return _err("Канал не знайдено", 404)
        with db.cursor() as cur:
            cur.execute("DELETE FROM telegram_channels WHERE id=%s", (cid,))
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


def register(core_api):
    global core
    core = core_api
    core.api.add_routes(bp)
    # Джерело вкладки «Онлайн» — так само, як e621 й Inkbunny
    registry = core.services.get("online.sources")
    registry.register(online.build(registry.base, _pool, core.current_user_id,
                                   core._db, core.url_prefix))
