"""
Telegram як джерело вкладки «Онлайн».

Так само, як e621 й Inkbunny: окремий клас-джерело, що реєструється через
сервіс 'online.sources' застосунку «Онлайн». Різниця одна — у Telegram
немає публічних посилань на файли: превʼю й самі файли віддає наш сервер
(/api/apps/telegram/media/...), скачуючи їх акаунтом того, хто дивиться.

Щоб не відкривати нове зʼєднання з Telegram на кожну мініатюру, клієнти
живуть довго: один фоновий цикл asyncio на процес, по клієнту на
користувача (ClientPool).
"""

import asyncio
import hashlib
import os
import tempfile
import threading

from . import tgcore

CACHE_DIR = os.path.join(tempfile.gettempdir(), "archive_tg_cache")
PAGE_LIMIT = 60


class ClientPool:
    """Підключені клієнти Telethon по користувачах, в одному фоновому циклі."""

    def __init__(self, creds_of):
        self._creds_of = creds_of        # user_id -> (api_id, api_hash, session) або TgError
        self._loop = None
        self._lock = threading.Lock()
        self._clients = {}               # user_id -> (fingerprint, client)
        self._entities = {}              # (user_id, key) -> entity

    def _ensure_loop(self):
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, daemon=True,
                                 name="telegram-online").start()
        return self._loop

    def run(self, coro, timeout=120):
        fut = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        return fut.result(timeout)

    async def _client(self, user_id):
        api_id, api_hash, session = self._creds_of(user_id)
        fp = hashlib.sha256(f"{api_id}:{session}".encode()).hexdigest()
        cur = self._clients.get(user_id)
        if cur and cur[0] == fp and cur[1].is_connected():
            return cur[1]
        if cur:
            try:
                await cur[1].disconnect()
            except Exception:
                pass
        try:
            client = tgcore.make_client(api_id, api_hash, session)
        except ValueError:
            raise tgcore.TgError("Сесія Telegram пошкоджена — увійди ще раз на сторінці Telegram")
        await client.connect()
        if not await client.is_user_authorized():
            raise tgcore.TgError("Сесія Telegram недійсна — увійди ще раз на сторінці Telegram")
        self._clients[user_id] = (fp, client)
        self._entities = {k: v for k, v in self._entities.items() if k[0] != user_id}
        return client

    def drop(self, user_id):
        cur = self._clients.pop(user_id, None)
        self._entities = {k: v for k, v in self._entities.items() if k[0] != user_id}
        if cur and self._loop:
            try:
                self.run(cur[1].disconnect(), timeout=15)
            except Exception:
                pass

    async def entity(self, user_id, raw_ref=None, channel_id=None):
        """Канал за посиланням або за id (кешується на користувача)."""
        client = await self._client(user_id)
        if raw_ref:
            key = (user_id, "ref", raw_ref.strip().lower())
            if key not in self._entities:
                ent = await tgcore.resolve_channel(client, tgcore.parse_ref(raw_ref))
                self._entities[key] = ent
                self._entities[(user_id, "id", int(ent.id))] = ent
            return client, self._entities[key]
        key = (user_id, "id", int(channel_id))
        if key not in self._entities:
            self._entities[key] = await tgcore.resolve_channel(
                client, {"channel_id": int(channel_id)}, fallback_id=int(channel_id))
        return client, self._entities[key]


def media_paths(user_id, cid, mid, thumb):
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = hashlib.md5(f"{user_id}:{cid}:{mid}:{int(bool(thumb))}".encode()).hexdigest()
    return os.path.join(CACHE_DIR, key)


async def fetch_media(pool, user_id, cid, mid, thumb):
    """
    Файл повідомлення (або його мініатюра) у кеш на диску. Кеш окремий для
    кожного користувача: файли із закритого каналу не має бачити той, хто
    в ньому не учасник. Повертає (шлях, розширення).
    """
    client, ent = await pool.entity(user_id, channel_id=cid)
    msg = await client.get_messages(ent, ids=int(mid))
    if not msg:
        raise tgcore.TgError("Повідомлення не знайдено")
    ext = tgcore.media_ext(msg)
    if not ext:
        raise tgcore.TgError("У повідомленні немає картинки чи відео")
    base = media_paths(user_id, cid, mid, thumb)
    for cand in (base + ".jpg", base + "." + ext):
        if os.path.exists(cand) and os.path.getsize(cand) > 0:
            return cand, cand.rsplit(".", 1)[-1]
    if thumb and not getattr(msg, "photo", None):
        # мініатюра документа/відео — якщо Telegram її зробив
        try:
            got = await client.download_media(msg, file=base + ".jpg", thumb=-1)
            if got and os.path.exists(base + ".jpg") and os.path.getsize(base + ".jpg") > 0:
                return base + ".jpg", "jpg"
        except Exception:
            pass
        if ext in ("mp4", "webm", "mov", "mkv"):
            raise tgcore.TgError("Мініатюри немає")
    path = base + "." + ext
    await client.download_media(msg, file=path)
    if not os.path.exists(path):
        raise tgcore.TgError("Не вдалося скачати")
    return path, ext


def build(LiveSourceBase, pool, current_user_id, db_factory, url_prefix):
    """Клас джерела для реєстру «Онлайн» (база приходить від застосунку online)."""

    def _bound_artists(channel_id):
        db = db_factory()
        try:
            with db.cursor() as cur:
                cur.execute("""SELECT DISTINCT a.artist_name FROM telegram_channels c
                               JOIN artists a ON a.id = c.artist_id WHERE c.channel_id=%s""", (channel_id,))
                return [r["artist_name"] for r in cur.fetchall()]
        except Exception:
            return []
        finally:
            db.close()

    def _result(cid, uname, title, m, artists, is_fav=False, head_id=None):
        ext = tgcore.media_ext(m)
        return {
            # у архіві альбом — один пост з id першого повідомлення
            "album_key": f"tg_{cid}_{head_id or m.id}",
            "id":        f"{cid}_{m.id}",
            "source":    "telegram",
            "post_url":  tgcore.post_url(cid, uname, m.id),
            "file_url":  f"{url_prefix}/media/{cid}/{m.id}",
            "thumb_url": f"{url_prefix}/media/{cid}/{m.id}?thumb=1",
            "ext":       ext,
            "width":     None, "height": None,
            # автор — той, до кого канал привʼязано в архіві: тоді кнопка
            # «скачати автора» запускає архіватор саме для нього
            "artists":   artists,
            "tags":      tgcore.hashtags(getattr(m, "message", "") or ""),
            "pool_id":   None, "pool_name": None,
            "is_video":  ext in ("mp4", "webm", "mov", "mkv"),
            "is_flash":  False,
            "is_favorite": is_fav,
            "channel_title": title,
        }

    def _favs_of(user_id, keys):
        if not keys:
            return set()
        db = db_factory()
        try:
            with db.cursor() as cur:
                ph = ",".join(["(%s,%s)"] * len(keys))
                args = [user_id]
                for c, m in keys:
                    args += [c, m]
                cur.execute(f"""SELECT channel_id, msg_id FROM telegram_favorites
                                WHERE user_id=%s AND (channel_id, msg_id) IN ({ph})""", args)
                return {(r["channel_id"], r["msg_id"]) for r in cur.fetchall()}
        finally:
            db.close()

    class TelegramSource(LiveSourceBase):
        id        = "telegram"
        label     = "Telegram"
        id_prefix = "tg_"
        # Ключів у «Онлайн» не питаємо: акаунт підключається на сторінці
        # Telegram (там вхід за кодом, якого тут не намалюєш).
        credential_fields = []

        supports_tags           = False
        supports_pools          = False
        supports_real_favorites = True     # своя таблиця telegram_favorites
        supports_favs_of_others = False

        search_fields = [
            {"id": "channel", "type": "text", "width": 300,
             "placeholder": "@канал, t.me/канал або t.me/+запрошення"},
        ]

        def is_configured(self):
            return True

        def library_key(self, r):
            return r.get("album_key") or f"{self.id_prefix}{r['id']}"

        def search(self, *, channel="", page=1, limit=60, **_):
            uid = current_user_id()
            if not (channel or "").strip():
                raise ValueError("Вкажи канал: @назва, t.me/назва або t.me/+запрошення")
            limit = min(int(limit or PAGE_LIMIT), 100)

            async def go():
                client, ent = await pool.entity(uid, raw_ref=channel)
                msgs = await client.get_messages(ent, limit=limit,
                                                 add_offset=(max(int(page), 1) - 1) * limit)
                return ent, msgs

            ent, msgs = pool.run(go())
            info = tgcore.channel_info(ent)
            cid, uname, title = info["channel_id"], info["username"], info["title"]
            media = [m for m in msgs if tgcore.media_ext(m)]
            heads = {}
            for m in msgs:
                gid = getattr(m, "grouped_id", None)
                if gid is not None:
                    heads[gid] = min(heads.get(gid, m.id), m.id)
            favs = _favs_of(uid, [(cid, m.id) for m in media])
            artists = _bound_artists(cid)
            return [_result(cid, uname, title, m, artists, (cid, m.id) in favs,
                            heads.get(getattr(m, "grouped_id", None)))
                    for m in media], None

        def toggle_favorite(self, post_id):
            uid = current_user_id()
            cid, mid = (int(x) for x in str(post_id).split("_", 1))
            db = db_factory()
            try:
                with db.cursor() as cur:
                    cur.execute("SELECT id FROM telegram_favorites WHERE user_id=%s AND channel_id=%s AND msg_id=%s",
                                (uid, cid, mid))
                    row = cur.fetchone()
                    if row:
                        cur.execute("DELETE FROM telegram_favorites WHERE id=%s", (row["id"],))
                        db.commit()
                        return False

                async def meta():
                    client, ent = await pool.entity(uid, channel_id=cid)
                    return ent, await client.get_messages(ent, ids=mid)

                ent, m = pool.run(meta())
                info = tgcore.channel_info(ent)
                ext = tgcore.media_ext(m) if m else None
                with db.cursor() as cur:
                    cur.execute("""INSERT IGNORE INTO telegram_favorites
                                   (user_id, channel_id, msg_id, ext, is_video, post_url, title)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                                (uid, cid, mid, ext, 1 if ext in ("mp4", "webm", "mov", "mkv") else 0,
                                 tgcore.post_url(cid, info["username"], mid), info["title"]))
                db.commit()
                return True
            finally:
                db.close()

        def search_favorites(self, *, page=1, limit=60, **_):
            uid = current_user_id()
            db = db_factory()
            try:
                with db.cursor() as cur:
                    cur.execute("""SELECT * FROM telegram_favorites WHERE user_id=%s
                                   ORDER BY created_at DESC LIMIT %s OFFSET %s""",
                                (uid, int(limit), (max(int(page), 1) - 1) * int(limit)))
                    rows = cur.fetchall()
            finally:
                db.close()
            out = []
            for r in rows:
                cid, mid = r["channel_id"], r["msg_id"]
                out.append({
                    "id": f"{cid}_{mid}", "source": "telegram", "post_url": r["post_url"],
                    "file_url": f"{url_prefix}/media/{cid}/{mid}",
                    "thumb_url": f"{url_prefix}/media/{cid}/{mid}?thumb=1",
                    "ext": r["ext"], "width": None, "height": None,
                    "artists": _bound_artists(cid), "tags": [], "pool_id": None, "pool_name": None,
                    "is_video": bool(r["is_video"]), "is_flash": False, "is_favorite": True,
                    "channel_title": r["title"],
                })
            return out

    return TelegramSource
