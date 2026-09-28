"""
Власні джерела — додати сайт без написання застосунку.

Таблиця називається live_custom_sources, а не live_sources: коротке ім'я
в деяких базах уже зайняте чимось іншим, і CREATE TABLE IF NOT EXISTS
мовчки не спрацьовував, а потім запит падав на неіснуючій колонці.

Готовий застосунок (як e621 чи Inkbunny) потрібен там, де в сайту свої
особливості: пули, улюблене, хитра авторизація. Але більшість сайтів
віддають дані одним із трьох звичних способів, і для них достатньо
заповнити форму.

СПОСОБИ ПІДКЛЮЧЕННЯ
  booru  — API у стилі Danbooru/e621: .../posts.json?tags=...&page=...
           Підходить для десятків бору-сайтів (danbooru, gelbooru-подібні,
           e926, safebooru...). Ключ не обов'язковий.
  json   — будь-який JSON API: ти вказуєш адресу з підстановками
           {query} {page} {limit} і де в відповіді лежать потрібні поля.
  rss    — RSS/Atom стрічка: картинка береться з enclosure, media:content
           або з першого <img> в описі.

Що вміють власні джерела: пошук і перегляд. Чого не вміють: пулів і
улюбленого НА САМОМУ САЙТІ (для нього потрібна авторизація в їхньому
API) — улюблене зберігається локально в архіві, як і для Inkbunny.
"""

import json
import re
import xml.etree.ElementTree as ET

import requests

from core.db import get_db, q, qq

TIMEOUT = 20
USER_AGENT = "Archive/1.0 (+self-hosted)"

KINDS = {
    "booru": {
        "label": "API у стилі Danbooru / e621",
        "hint":  "Адреса сайту, напр. https://e926.net. Решту підставимо самі.",
        "fields": [
            {"key": "base_url", "label": "Адреса сайту", "required": True,
             "placeholder": "https://e926.net"},
            {"key": "path", "label": "Шлях до API (якщо інший)", "required": False,
             "placeholder": "/posts.json"},
            {"key": "login", "label": "Логін (якщо треба)", "required": False},
            {"key": "api_key", "label": "API-ключ (якщо треба)", "required": False,
             "type": "password"},
        ],
    },
    "json": {
        "label": "Довільний JSON API",
        "hint":  "У адресі можна вживати {query}, {page}, {limit}. Шляхи до полів — через крапку.",
        "fields": [
            {"key": "url", "label": "Адреса запиту", "required": True,
             "placeholder": "https://site.com/api/search?q={query}&page={page}"},
            {"key": "items_path", "label": "Де лежить список", "required": False,
             "placeholder": "data.posts  (порожньо = відповідь і є списком)"},
            {"key": "field_image", "label": "Поле з картинкою", "required": True,
             "placeholder": "file.url"},
            {"key": "field_thumb", "label": "Поле з мініатюрою", "required": False,
             "placeholder": "preview.url"},
            {"key": "field_id", "label": "Поле з id", "required": False, "placeholder": "id"},
            {"key": "field_page", "label": "Поле з посиланням на сторінку", "required": False},
            {"key": "field_tags", "label": "Поле з тегами", "required": False},
            {"key": "header_name", "label": "Назва заголовка авторизації (якщо треба)",
             "required": False, "placeholder": "X-Weasyl-API-Key або Authorization"},
            {"key": "header_value", "label": "Значення заголовка", "required": False,
             "type": "password"},
        ],
    },
    "rss": {
        "label": "RSS / Atom стрічка",
        "hint":  "У адресі можна вживати {query}. Пошуку може не бути — тоді просто стрічка.",
        "fields": [
            {"key": "url", "label": "Адреса стрічки", "required": True,
             "placeholder": "https://site.com/feed?tag={query}"},
        ],
    },
}

IMAGE_EXT = ("png", "jpg", "jpeg", "gif", "webp")
VIDEO_EXT = ("webm", "mp4")


# ── зберігання ───────────────────────────────────────────────

def list_custom(user_id=None) -> list:
    db = get_db()
    try:
        rows = qq(db, "SELECT * FROM live_custom_sources ORDER BY label")
        for r in rows:
            try:
                r["config"] = json.loads(r["config"] or "{}")
            except Exception:
                r["config"] = {}
            # ключі назовні не віддаємо
            for secret in ("api_key", "header_value"):
                if r["config"].get(secret):
                    r["config"][secret] = "••••••••"
            if r.get("created_at"):
                r["created_at"] = r["created_at"].isoformat()
        return rows
    finally:
        db.close()


def save_custom(source_id, label, kind, config, user_id=None):
    if kind not in KINDS:
        raise ValueError(f"Невідомий спосіб підключення: {kind}")
    if not re.fullmatch(r"[a-z0-9_-]{2,32}", source_id or ""):
        raise ValueError("Ідентифікатор: 2–32 символи a-z, 0-9, _ або -")
    for f in KINDS[kind]["fields"]:
        if f.get("required") and not (config.get(f["key"]) or "").strip():
            raise ValueError(f"Не заповнено: {f['label']}")

    db = get_db()
    try:
        # збережені раніше секрети не затираємо масками з форми
        old = q(db, "SELECT config FROM live_custom_sources WHERE id=%s", (source_id,))
        if old:
            try:
                old_cfg = json.loads(old["config"] or "{}")
            except Exception:
                old_cfg = {}
            for secret in ("api_key", "header_value"):
                if config.get(secret, "").startswith("••"):
                    config[secret] = old_cfg.get(secret, "")
        with db.cursor() as cur:
            cur.execute("""INSERT INTO live_custom_sources (id, label, kind, config, created_by)
                           VALUES (%s,%s,%s,%s,%s)
                           ON DUPLICATE KEY UPDATE label=VALUES(label), kind=VALUES(kind),
                                                   config=VALUES(config)""",
                        (source_id, label or source_id, kind,
                         json.dumps(config, ensure_ascii=False), user_id))
        db.commit()
    finally:
        db.close()


def delete_custom(source_id):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM live_custom_sources WHERE id=%s", (source_id,))
        db.commit()
    finally:
        db.close()


# ── саме джерело ─────────────────────────────────────────────

def _dig(obj, path, empty_is_whole=False):
    """
    Значення за шляхом 'a.b.c'.

    Порожній шлях означає «поле не вказане» і дає None — раніше він
    повертав увесь об'єкт, і в мініатюру чи теги підставлявся цілий
    запис. Виняток — список елементів (items_path), де порожній шлях
    справді означає «відповідь і є списком».
    """
    if not path:
        return obj if empty_is_whole else None
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit():
            obj = obj[int(part)] if int(part) < len(obj) else None
        else:
            return None
        if obj is None:
            return None
    return obj


def _ext_of(url):
    tail = (url or "").split("?")[0].rsplit(".", 1)
    return tail[-1].lower() if len(tail) == 2 and len(tail[-1]) <= 5 else ""


def build_custom_class(Base, row):
    """Робить клас джерела з рядка налаштувань."""
    cfg = row["config"] if isinstance(row["config"], dict) else json.loads(row["config"] or "{}")
    kind = row["kind"]

    class CustomSource(Base):
        id        = row["id"]
        label     = row["label"]
        id_prefix = f"{row['id']}_"
        is_custom = True

        credential_fields = []          # ключі зберігаються в самому джерелі
        supports_tags           = kind in ("booru", "json")
        supports_pools          = False
        supports_real_favorites = False
        supports_favs_of_others = False

        def is_configured(self):
            return True

        def is_authorized(self):
            return True

        # ── booru ────────────────────────────────────────────
        def _search_booru(self, tags, page, limit):
            base = cfg["base_url"].rstrip("/")
            path = (cfg.get("path") or "/posts.json").lstrip("/")
            auth = (cfg.get("login"), cfg.get("api_key")) if cfg.get("login") and cfg.get("api_key") else None
            r = requests.get(f"{base}/{path}", params={"tags": tags, "limit": limit, "page": page},
                             headers={"User-Agent": USER_AGENT}, auth=auth, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            posts = data.get("posts", data) if isinstance(data, dict) else data
            out = []
            for p in posts or []:
                # e621-подібний вигляд і danbooru-подібний — обидва
                file_url = _dig(p, "file.url") or p.get("file_url") or p.get("large_file_url")
                thumb    = _dig(p, "preview.url") or p.get("preview_file_url") or file_url
                if not file_url:
                    continue
                tags_obj = p.get("tags")
                if isinstance(tags_obj, dict):
                    tag_list = [x for v in tags_obj.values() if isinstance(v, list) for x in v]
                else:
                    tag_list = (p.get("tag_string") or "").split()
                out.append(self._result(p.get("id"), file_url, thumb, tag_list,
                                        f"{base}/posts/{p.get('id')}"))
            return out

        # ── довільний JSON ───────────────────────────────────
        def _search_json(self, tags, page, limit):
            url = (cfg["url"].replace("{query}", requests.utils.quote(tags or ""))
                             .replace("{page}", str(page)).replace("{limit}", str(limit)))
            headers = {"User-Agent": USER_AGENT}
            # Кожен сайт зве свій заголовок по-своєму: у Weasyl це
            # X-Weasyl-API-Key, десь Authorization, десь ще щось.
            if cfg.get("header_value"):
                headers[cfg.get("header_name") or "Authorization"] = cfg["header_value"]
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            items = _dig(r.json(), cfg.get("items_path") or "", empty_is_whole=True)
            out = []
            for it in items or []:
                img = _dig(it, cfg["field_image"])
                if not img:
                    continue
                tag_val = _dig(it, cfg.get("field_tags"))
                tag_list = (tag_val if isinstance(tag_val, list)
                            else str(tag_val).replace(",", " ").split() if tag_val else [])
                out.append(self._result(_dig(it, cfg.get("field_id") or "id"), img,
                                        _dig(it, cfg.get("field_thumb")) or img,
                                        tag_list, _dig(it, cfg.get("field_page"))))
            return out

        # ── RSS / Atom ───────────────────────────────────────
        def _search_rss(self, tags, page, limit):
            url = cfg["url"].replace("{query}", requests.utils.quote(tags or ""))
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            ns = {"media": "http://search.yahoo.com/mrss/", "atom": "http://www.w3.org/2005/Atom"}
            items = root.findall(".//item") or root.findall(".//atom:entry", ns)
            out = []
            for n, it in enumerate(items[:limit]):
                img = None
                enc = it.find("enclosure")
                if enc is not None:
                    img = enc.get("url")
                if not img:
                    mc = it.find("media:content", ns)
                    if mc is not None:
                        img = mc.get("url")
                if not img:
                    desc = (it.findtext("description") or "")
                    m = re.search(r'<img[^>]+src="([^"]+)"', desc)
                    img = m.group(1) if m else None
                if not img:
                    continue
                link = it.findtext("link") or ""
                out.append(self._result(f"{page}_{n}", img, img, [], link,
                                        title=it.findtext("title") or ""))
            return out

        def _result(self, pid, file_url, thumb, tags, post_url, title=""):
            ext = _ext_of(file_url)
            return {
                "id":        pid,
                "source":    self.id,
                "post_url":  post_url or file_url,
                "file_url":  file_url,
                "thumb_url": thumb or file_url,
                "ext":       ext,
                "artists":   [title] if title else [],
                "tags":      tags or [],
                "pool_id":   None,
                "pool_name": None,
                "is_video":  ext in VIDEO_EXT,
                "is_flash":  False,
            }

        def search(self, *, tags="", artist="", exclude="", sort="", pool_id=None,
                   page=1, limit=60):
            query = " ".join(x for x in (tags, artist) if x)
            if exclude:
                query += " " + " ".join(f"-{w}" for w in exclude.split() if w)
            fn = {"booru": self._search_booru, "json": self._search_json,
                  "rss": self._search_rss}[kind]
            return fn(query.strip(), page, limit), None

    CustomSource.__name__ = f"Custom_{row['id']}"
    return CustomSource


def load_custom(Base) -> dict:
    """Усі власні джерела як готові класи. Помилка в одному не ламає решту."""
    try:
        db = get_db()
    except Exception:
        return {}
    try:
        rows = qq(db, "SELECT * FROM live_custom_sources")
    except Exception:
        return {}                # таблиці ще немає (міграція не пройшла)
    finally:
        db.close()

    out = {}
    for r in rows:
        try:
            out[r["id"]] = build_custom_class(Base, r)
        except Exception as e:
            print(f"  ⚠ Власне джерело '{r.get('id')}': {e}")
    return out


def probe(kind, config, query="") -> dict:
    """
    Перевірка «чи працює» перед збереженням: пробуємо один запит.

    query — приклад запиту. Для адрес, де {query} обов'язковий (напр.
    ім'я користувача в адресі галереї), без нього вийшов би запит із
    порожнім місцем і незрозуміла помилка 404.
    """
    if kind not in KINDS:
        return {"ok": False, "error": "Не обрано спосіб підключення"}
    # Без цього порожнє поле давало незрозуміле KeyError просто в обличчя
    missing = [f["label"] for f in KINDS[kind]["fields"]
               if f.get("required") and not (config.get(f["key"]) or "").strip()]
    if missing:
        return {"ok": False, "error": "Не заповнено: " + ", ".join(missing)}
    # Адреса з {query} без прикладу дала б запит із порожнім місцем і
    # незрозуміле 404 — краще сказати прямо
    if "{query}" in (config.get("url") or "") and not query:
        return {"ok": False,
                "error": "В адресі є {query} — впиши приклад запиту нижче "
                         "(для Weasyl це ім'я користувача)"}
    row = {"id": "probe", "label": "probe", "kind": kind, "config": config}
    from .sources import LiveSourceBase
    cls = build_custom_class(LiveSourceBase, row)
    try:
        results, _ = cls({}).search(tags=query, limit=5)
        return {"ok": True, "found": len(results),
                "sample": [r["thumb_url"] for r in results[:3]]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
