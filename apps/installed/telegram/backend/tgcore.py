"""
Telegram: спільне для веб-застосунку і архіватора.

Цей модуль НЕ імпортує нічого з core.*: його вантажить і веб-сервер
(через застосунок), і окремий процес furry_archiver.py (за шляхом до
файлу). Тому тут лише те, що потрібно обом:

  * шифрування сесії й api_hash (ключ — той самий секрет сервера, що й
    для підпису сесій: /data/.secret_key);
  * розбір посилання на канал;
  * читання каналу пост за постом (альбом = один багатосторінковий пост).

Доступ — через Telethon під акаунтом користувача: так видно оригінали
(файли без стиснення), відео і закриті канали, де користувач учасник.
"""

import asyncio
import base64
import hashlib
import os
import re

# ── Шифрування ────────────────────────────────────────────────────────
#
#  Рядок сесії Telethon — це повний доступ до акаунта. У базі він лежить
#  лише зашифрованим. Ключ не в базі, а у файлі секрету на томі /data:
#  дамп бази сам по собі сесію не віддасть.

SECRET_KEY_FILE = "/data/.secret_key" if os.path.isdir("/data") else "./.secret_key"


def _fernet():
    from cryptography.fernet import Fernet
    with open(SECRET_KEY_FILE, "r") as f:
        secret = f.read().strip()
    if not secret:
        raise RuntimeError("Секрет сервера порожній")
    key = base64.urlsafe_b64encode(hashlib.sha256(("telegram:" + secret).encode()).digest())
    return Fernet(key)


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


# ── Посилання на канал ────────────────────────────────────────────────

def parse_ref(raw: str) -> dict:
    """
    Що ввів користувач → як шукати канал.
      @name, name, t.me/name, https://t.me/name/123  → {'username': 'name'}
      t.me/+HASH, t.me/joinchat/HASH                 → {'invite': 'HASH'}
      t.me/c/1234567890/55, -1001234567890           → {'channel_id': 1234567890}
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("Порожнє посилання")
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^(www\.)?(t\.me|telegram\.me)/", "tme/", s)
    m = re.match(r"^tme/(?:\+|joinchat/)([\w-]+)", s)
    if m:
        return {"invite": m.group(1)}
    m = re.match(r"^tme/c/(\d+)", s)
    if m:
        return {"channel_id": int(m.group(1))}
    m = re.match(r"^-100(\d+)$", s)
    if m:
        return {"channel_id": int(m.group(1))}
    m = re.match(r"^(?:tme/|@)?([A-Za-z][\w]{3,})(?:/\d+)?/?$", s)
    if m and m.group(1).lower() not in ("joinchat", "addstickers", "share"):
        return {"username": m.group(1)}
    raise ValueError("Не схоже на посилання на канал: очікую @назва, t.me/назва або t.me/+запрошення")


def post_url(channel_id: int, username: str | None, msg_id: int) -> str:
    if username:
        return f"https://t.me/{username}/{msg_id}"
    return f"https://t.me/c/{channel_id}/{msg_id}"


# ── Клієнт ────────────────────────────────────────────────────────────

class TgError(Exception):
    """Зрозуміла причина (показується користувачу)."""


def make_client(api_id: int, api_hash: str, session: str = ""):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(session or ""), int(api_id), api_hash,
                          device_model="Archive", app_version="1.0",
                          flood_sleep_threshold=120)


async def _find_in_dialogs(client, channel_id: int):
    """Канал серед діалогів акаунта: свіжа сесія не знає access_hash за самим id."""
    async for d in client.iter_dialogs():
        ent = d.entity
        if getattr(ent, "id", None) == channel_id:
            return ent
    return None


async def resolve_channel(client, ref: dict, fallback_id: int | None = None):
    """
    Сутність каналу за розібраним посиланням. Для закритого — лише якщо ти
    учасник. fallback_id — уже відомий id каналу: якщо посилання-запрошення
    відкликали, а ти досі в каналі, знайдемо його серед твоїх діалогів.
    """
    from telethon.tl.types import PeerChannel
    try:
        if "username" in ref:
            ent = await client.get_entity(ref["username"])
        elif "invite" in ref:
            from telethon.tl.functions.messages import CheckChatInviteRequest
            from telethon.tl.types import ChatInviteAlready
            info = await client(CheckChatInviteRequest(ref["invite"]))
            if isinstance(info, ChatInviteAlready):
                ent = info.chat
            else:
                raise TgError("Ти ще не учасник цього каналу. Вступи в нього в Telegram і спробуй знову.")
        else:
            try:
                ent = await client.get_entity(PeerChannel(ref["channel_id"]))
            except Exception:
                ent = await _find_in_dialogs(client, ref["channel_id"])
                if ent is None:
                    raise TgError("Канал не знайдено серед твоїх каналів — перевір, що ти в ньому")
    except TgError as e:
        ent = await _find_in_dialogs(client, fallback_id) if fallback_id else None
        if ent is None:
            raise e
    except Exception as e:
        ent = await _find_in_dialogs(client, fallback_id) if fallback_id else None
        if ent is None:
            raise TgError(f"Канал не знайдено або немає доступу: {e}")
    if getattr(ent, "broadcast", None) is None and getattr(ent, "megagroup", None) is None:
        raise TgError("Це не канал і не група")
    return ent


def channel_info(ent) -> dict:
    return {"channel_id": int(ent.id), "username": getattr(ent, "username", None),
            "title": getattr(ent, "title", None) or getattr(ent, "username", None) or str(ent.id)}


# ── Що з повідомлення беремо ──────────────────────────────────────────

_MEDIA_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif",
              "image/bmp": "bmp", "image/tiff": "tiff", "image/avif": "avif", "image/heic": "heic",
              "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
              "video/x-matroska": "mkv"}


def media_ext(msg) -> str | None:
    """
    Розширення медіа, яке архівуємо, або None (пропустити).
    Беремо: фото, картинки й відео, надіслані файлом (оригінали без
    стиснення), GIF (у Telegram це mp4). Не беремо: стікери, голосові,
    кружечки-відео, превʼю посилань, будь-які інші документи.
    """
    if getattr(msg, "photo", None):
        return "jpg"
    doc = getattr(msg, "document", None)
    if not doc or getattr(msg, "sticker", None) or getattr(msg, "voice", None):
        return None
    if getattr(msg, "video_note", None):
        return None
    mime = (getattr(doc, "mime_type", "") or "").lower()
    if not (mime.startswith("image/") or mime.startswith("video/")):
        return None
    name = None
    f = getattr(msg, "file", None)
    if f is not None:
        name = getattr(f, "name", None)
    if name and "." in name:
        ext = re.sub(r"[^a-z0-9]", "", name.rsplit(".", 1)[-1].lower())[:10]
        if ext:
            return ext
    return _MEDIA_EXT.get(mime) or (mime.split("/", 1)[1][:10] if "/" in mime else None)


def hashtags(text: str) -> list:
    """#теги з підпису → теги архіву."""
    return sorted({t.lower() for t in re.findall(r"#([\w]+)", text or "") if len(t) > 1})


# ── Читання каналу ────────────────────────────────────────────────────

def group_posts(messages):
    """
    Повідомлення (від старших до новіших) → пости. Альбом у Telegram — це
    кілька повідомлень з одним grouped_id; для архіву це один пост з
    кількома сторінками. Повідомлення без медіа, що нас цікавить,
    пропускаються, але їхній id теж «прочитаний».
    Повертає [(post_msgs, max_id)], де post_msgs може бути порожнім.
    """
    out, cur, cur_gid = [], [], None
    for m in messages:
        gid = getattr(m, "grouped_id", None)
        if cur and (gid is None or gid != cur_gid):
            out.append(cur)
            cur = []
        cur.append(m)
        cur_gid = gid
        if gid is None:
            out.append(cur)
            cur, cur_gid = [], None
    if cur:
        out.append(cur)
    return out


class ChannelReader:
    """
    Читає канал синхронно, пост за постом — для архіватора, який сам
    синхронний. Усередині власний цикл asyncio на весь час читання.

        with ChannelReader(api_id, api_hash, session, ref) as r:
            for post in r.posts(after_id=123):
                ...  # post: dict, файли вже скачані у тимчасову теку
    """

    BATCH = 100

    def __init__(self, api_id, api_hash, session, ref: dict, temp_dir: str,
                 fallback_id: int | None = None):
        self.loop = asyncio.new_event_loop()
        # Telethon привʼязується до «поточного» циклу потоку — робимо ним наш
        asyncio.set_event_loop(self.loop)
        self.client = make_client(api_id, api_hash, session)
        self.ref = ref
        self.fallback_id = fallback_id
        self.temp_dir = temp_dir
        self.entity = None
        self.info = None

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    def __enter__(self):
        self._run(self.client.connect())
        if not self._run(self.client.is_user_authorized()):
            raise TgError("Сесія Telegram недійсна — увійди в акаунт ще раз на сторінці Telegram")
        self.entity = self._run(resolve_channel(self.client, self.ref, self.fallback_id))
        self.info = channel_info(self.entity)
        return self

    def __exit__(self, *exc):
        # disconnect() у Telethon повертає корутину лише ВСЕРЕДИНІ циклу, що
        # працює; поза ним — None (і сам усе робить). Тому кличемо зсередини
        # корутини: так це завжди правильне очікування.
        async def _dc():
            res = self.client.disconnect()
            if asyncio.iscoroutine(res) or isinstance(res, asyncio.Future):
                await res
        try:
            self._run(_dc())
        except Exception as e:
            print(f"    ⚠ Telegram: відʼєднання: {e}")
        finally:
            close_loop(self.loop)
            asyncio.set_event_loop(None)

    def _fetch(self, min_id):
        return self._run(self.client.get_messages(self.entity, limit=self.BATCH,
                                                  min_id=min_id, reverse=True))

    def posts(self, after_id: int = 0):
        """
        Генератор постів новіших за after_id, від старших до новіших.
        Кожен пост — dict:
          msg_id   — id першого повідомлення (на ньому будується post_id);
          last_id  — найбільший id у пості (до нього можна зсунути курсор);
          url, tags, media — скільки в ньому файлів, які архівуємо.
        Файли ще НЕ скачані: download(post) — лише для тих, яких в архіві
        немає, щоб повторний перегляд каналу не тягнув усе знову.
        Порожні пости (текст без медіа) теж приходять — з media=0, щоб
        курсор просувався.
        """
        os.makedirs(self.temp_dir, exist_ok=True)
        cid, uname = self.info["channel_id"], self.info["username"]
        cursor = int(after_id or 0)
        carry = []                       # незавершений альбом з кінця пачки
        while True:
            batch = list(self._fetch(cursor))
            if not batch:
                break
            cursor = max(m.id for m in batch)
            msgs = carry + batch
            groups = group_posts(msgs)
            # останній альбом міг продовжитись у наступній пачці
            carry = groups.pop() if groups and getattr(groups[-1][-1], "grouped_id", None) else []
            for g in groups:
                yield self._post(g, cid, uname)
            if len(batch) < self.BATCH:
                break
        if carry:
            yield self._post(carry, cid, uname)

    def _post(self, msgs, cid, uname) -> dict:
        first = msgs[0]
        text = " ".join((getattr(m, "message", "") or "") for m in msgs)
        media = [(m, media_ext(m)) for m in msgs]
        media = [(m, e) for m, e in media if e]
        return {"msg_id": int(first.id), "last_id": int(max(m.id for m in msgs)),
                "url": post_url(cid, uname, first.id), "tags": hashtags(text),
                "media": len(media), "_media": media, "_cid": cid}

    def download(self, post) -> list:
        """Скачати файли поста: [{path, filename, ext, page_num}] — сторінки по порядку."""
        files = []
        for page, (m, ext) in enumerate(post["_media"], start=1):
            fname = f"tg_{post['_cid']}_{m.id}.{ext}"
            path = os.path.join(self.temp_dir, fname)
            try:
                got = self._run(self.client.download_media(m, file=path))
            except Exception as e:
                print(f"    ✗ Telegram: не вдалося скачати {fname}: {e}")
                continue
            if got and os.path.exists(path) and os.path.getsize(path) > 0:
                files.append({"path": path, "filename": fname, "ext": ext, "page_num": page})
        return files


# ── Вхід в акаунт (для веб-застосунку) ────────────────────────────────

def close_loop(loop):
    """
    Закрити цикл акуратно. Telethon після disconnect() лишає свої фонові
    задачі (прийом/відправка, keepalive) скасованими, але ще не завершеними;
    якщо просто закрити цикл, Python наприкінці сипле в лог
    «Task was destroyed but it is pending!». Даємо їм завершитись.
    """
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
    except Exception:
        pass
    finally:
        loop.close()


def run_sync(coro_fn, *args, **kwargs):
    """Виконати корутину в окремому циклі — Flask-потоки синхронні."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_fn(*args, **kwargs))
    finally:
        close_loop(loop)


# Куди Telegram відправив код — щоб людина знала, де його шукати.
_CODE_WHERE = {
    "SentCodeTypeApp":        "у застосунок Telegram на іншому пристрої — чат «Telegram» (синя галочка), не SMS",
    "SentCodeTypeSms":        "SMS на номер",
    "SentCodeTypeSmsWord":    "SMS на номер (слово)",
    "SentCodeTypeSmsPhrase":  "SMS на номер (фраза)",
    "SentCodeTypeCall":       "дзвінком — код продиктують",
    "SentCodeTypeFlashCall":  "дзвінком — код це останні цифри номера, що дзвонив",
    "SentCodeTypeMissedCall": "пропущеним дзвінком — код це останні цифри номера, що дзвонив",
    "SentCodeTypeFragmentSms": "через Fragment (анонімний номер) — подивись на fragment.com",
    "SentCodeTypeFirebaseSms": "SMS на номер",
}


def describe_sent(sent) -> dict:
    """SentCode → що показати людині: куди пішов код і чи можна попросити інакше."""
    if type(sent).__name__ == "SentCodePaymentRequired":
        return {"type": "SentCodePaymentRequired", "where": None, "next": None, "timeout": None,
                "email_setup_required": False, "payment_required": True}
    t = type(getattr(sent, "type", None)).__name__
    nt = getattr(sent, "next_type", None)
    nt = type(nt).__name__ if nt is not None else None
    if t == "SentCodeTypeEmailCode":
        pattern = getattr(sent.type, "email_pattern", "") or ""
        where = f"на пошту для входу {pattern}".strip()
    elif t == "SentCodeTypeSetUpEmailRequired":
        where = None
    else:
        where = _CODE_WHERE.get(t, f"невідомий спосіб ({t})")
    next_where = None
    if nt:
        next_where = {"CodeTypeSms": "SMS", "CodeTypeCall": "дзвінок", "CodeTypeFlashCall": "дзвінок",
                      "CodeTypeMissedCall": "пропущений дзвінок", "CodeTypeFragmentSms": "Fragment"}.get(nt, nt)
    return {"type": t, "where": where, "next": next_where,
            "timeout": getattr(sent, "timeout", None),
            "email_setup_required": t == "SentCodeTypeSetUpEmailRequired"}


async def login_start(api_id, api_hash, phone):
    """
    Надіслати код. Повертає (рядок незавершеної сесії, phone_code_hash, опис —
    куди пішов код). Telegram майже завжди шле код У ЗАСТОСУНОК (чат «Telegram»),
    а не SMS — через це «код не приходить».
    """
    client = make_client(api_id, api_hash)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        return client.session.save(), sent.phone_code_hash, describe_sent(sent)
    finally:
        await client.disconnect()


async def login_resend(api_id, api_hash, session, phone, phone_code_hash):
    """Надіслати код іншим способом (зазвичай SMS після застосунку)."""
    from telethon.tl.functions.auth import ResendCodeRequest
    client = make_client(api_id, api_hash, session)
    await client.connect()
    try:
        sent = await client(ResendCodeRequest(phone_number=phone, phone_code_hash=phone_code_hash))
        return client.session.save(), sent.phone_code_hash, describe_sent(sent)
    finally:
        await client.disconnect()


def explain_error(e) -> str:
    """Помилки Telegram при вході — людською мовою."""
    name = type(e).__name__
    msg = str(e)
    table = {
        "ApiIdInvalidError":        "api_id або api_hash неправильні — перевір на my.telegram.org",
        "ApiIdPublishedFloodError": "цей api_id заблоковано Telegram — створи новий застосунок на my.telegram.org",
        "PhoneNumberInvalidError":  "номер телефону неправильний — у форматі +380…",
        "PhoneNumberBannedError":   "цей номер заблоковано в Telegram",
        "PhoneNumberFloodError":    "забагато запитів коду на цей номер — зачекай і спробуй пізніше",
        "PhoneCodeInvalidError":    "код неправильний",
        "PhoneCodeExpiredError":    "код протерміновано — надішли новий. Telegram скасовує код, якщо його переслали комусь чи вставили в інший чат",
        "PhoneCodeEmptyError":      "введи код",
        "PasswordHashInvalidError": "хмарний пароль неправильний",
        "SessionPasswordNeededError": "потрібен хмарний пароль",
        "SendCodeUnavailableError": "Telegram вичерпав способи надіслати код — зачекай кілька годин",
    }
    if name in table:
        return table[name]
    if name == "FloodWaitError":
        return f"Telegram просить зачекати {getattr(e, 'seconds', '?')} с перед новою спробою"
    return msg or name


async def login_finish(api_id, api_hash, session, phone, phone_code_hash, code=None, password=None):
    """
    Завершити вхід. Повертає ('ok', сесія, ім'я) або ('password', сесія, None),
    якщо на акаунті хмарний пароль (2FA) — тоді викликати знову з password.
    """
    from telethon.errors import SessionPasswordNeededError
    client = make_client(api_id, api_hash, session)
    await client.connect()
    try:
        try:
            if password:
                await client.sign_in(password=password)
            else:
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            return "password", client.session.save(), None
        me = await client.get_me()
        name = ("@" + me.username) if getattr(me, "username", None) else \
               " ".join(p for p in [me.first_name, me.last_name] if p)
        return "ok", client.session.save(), name
    finally:
        await client.disconnect()


async def logout(api_id, api_hash, session):
    client = make_client(api_id, api_hash, session)
    await client.connect()
    try:
        if await client.is_user_authorized():
            await client.log_out()
    finally:
        await client.disconnect()


async def resolve(api_id, api_hash, session, raw_ref):
    ref = parse_ref(raw_ref)
    client = make_client(api_id, api_hash, session)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise TgError("Сесія Telegram недійсна — увійди в акаунт ще раз")
        return ref, channel_info(await resolve_channel(client, ref))
    finally:
        await client.disconnect()
