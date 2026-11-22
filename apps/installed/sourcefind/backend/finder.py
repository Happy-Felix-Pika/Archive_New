"""
Пошук оригіналу файлу за самою картинкою.

Порядок (від дешевого й точного до повільного):
  1. e621 за MD5 — той самий файл байт у байт (точно, миттєво).
  2. FuzzySearch — перцептивний хеш по e621, Inkbunny, FurAffinity, Twitter,
     Weasyl… (на ньому ж працюють Telegram-боти пошуку). Потрібен API-ключ.
  3. e621 IQDB — схожі картинки на e621 (власний пошук e621, ключ e621).

Знайдене впевнено — застосовуємо одразу: посилання на оригінал і теги.
Сумнівне — у «На перевірку». Пріоритет джерел: e621 → Inkbunny → решта.
Файли нікуди не переносяться.
"""

import hashlib
import io
import json
import re
import time

SITE_PRIORITY = {"e621": 0, "inkbunny": 1, "furaffinity": 2, "weasyl": 3, "twitter": 4}

FUZZY_URLS = ["https://api-next.fuzzysearch.net/v1/image", "https://api.fuzzysearch.net/image"]
FUZZY_SURE = 3        # відстань хешів: 0–3 — та сама картинка
FUZZY_MAYBE = 7
IQDB_SURE = 90        # схожість e621 IQDB у відсотках
IQDB_MAYBE = 75

IMAGE_EXT = ("jpg", "jpeg", "png", "gif", "webp", "bmp")


def page_url(site: str, site_id) -> str | None:
    if not site_id:
        return None
    return {
        "e621":        f"https://e621.net/posts/{site_id}",
        "inkbunny":    f"https://inkbunny.net/s/{site_id}",
        "furaffinity": f"https://www.furaffinity.net/view/{site_id}/",
        "weasyl":      f"https://www.weasyl.com/~/submissions/{site_id}",
        "twitter":     f"https://twitter.com/i/web/status/{site_id}",
    }.get(site)


def _norm_site(s) -> str:
    s = re.sub(r"[^a-z0-9]", "", str(s or "").lower())
    return {"fa": "furaffinity", "furaffinitynet": "furaffinity", "e926": "e621",
            "ib": "inkbunny", "x": "twitter"}.get(s, s)


def _site_from_url(u: str) -> tuple[str | None, str | None]:
    """Сайт і номер роботи з посилання на сторінку (для джерел e621-поста)."""
    pats = [
        ("inkbunny",    r"inkbunny\.net/(?:s/|submissionview\.php\?id=)(\d+)"),
        ("furaffinity", r"furaffinity\.net/(?:view|full)/(\d+)"),
        ("e621",        r"e(?:621|926)\.net/posts/(\d+)"),
        ("weasyl",      r"weasyl\.com/(?:~[^/]+/)?submissions?/(\d+)"),
        ("twitter",     r"(?:twitter|x)\.com/[^/]+/status/(\d+)"),
    ]
    for site, p in pats:
        m = re.search(p, u or "", re.I)
        if m:
            return site, m.group(1)
    return None, None


class Finder:
    def __init__(self, fuzzy_key: str = "", use_iqdb: bool = True, log=print):
        self.fuzzy_key = (fuzzy_key or "").strip()
        self.use_iqdb = use_iqdb
        self.log = log
        self._e6 = None
        self._ib = None
        self._last_fuzzy = 0.0
        self._fuzzy_warned = False

    # ── доступ до сайтів (облікові дані — з «Конфігу» архіву) ──────────
    def e621(self):
        if self._e6 is None:
            from furry_archiver import Config, E621Downloader
            self._e6 = E621Downloader(Config())
        return self._e6

    def inkbunny(self):
        if self._ib is None:
            try:
                from furry_archiver import Config, InkbunnyDownloader
                self._ib = InkbunnyDownloader(Config())
            except Exception as e:
                self.log(f"  ⚠ Inkbunny недоступний: {e}")
                self._ib = False
        return self._ib or None

    # ── пошук ────────────────────────────────────────────────────────
    def find(self, content: bytes, ext: str, md5: str | None = None) -> list:
        """Усі кандидати: [{site, site_id, url, artists, method, score, sure}].
        self.errors — що не вдалося спитати (тоді «не знайдено» не остаточне)."""
        self.errors = []
        out = []
        md5 = md5 or hashlib.md5(content).hexdigest()

        # 1. Той самий файл на e621
        try:
            from furry_archiver import E621_API_BASE
            dl = self.e621()
            dl._rate_limit()
            r = dl.session.get(f"{E621_API_BASE}/posts.json", params={"tags": f"md5:{md5}", "limit": 1}, timeout=30)
            r.raise_for_status()
            posts = dl._posts(r.json())
            for p in posts or []:
                out.append(self._e621_cand(p, "md5", 100, True))
        except Exception as e:
            self.log(f"  ⚠ e621 MD5: {e}")
            self.errors.append(f"e621: {e}")
        if out:
            return out

        small = _shrink(content, ext)

        # 2. FuzzySearch
        if self.fuzzy_key:
            out += self._fuzzy(small)
        if any(c["sure"] and c["site"] in ("e621", "inkbunny") for c in out):
            return out

        # 3. e621 IQDB
        if self.use_iqdb:
            out += self._iqdb(small)
        return out

    def _e621_cand(self, p: dict, method, score, sure):
        tags = p.get("tags") or {}
        return {"site": "e621", "site_id": str(p.get("id")), "url": page_url("e621", p.get("id")),
                "artists": [a for a in (tags.get("artist") or []) if a not in ("conditional_dnp", "sound_warning", "unknown_artist")],
                "method": method, "score": score, "sure": sure,
                "sources": p.get("sources") or [], "_post": p}

    def _fuzzy(self, img: bytes) -> list:
        import requests
        wait = 1.0 - (time.time() - self._last_fuzzy)
        if wait > 0:
            time.sleep(wait)
        self._last_fuzzy = time.time()
        headers = {"x-api-key": self.fuzzy_key, "Authorization": f"Bearer {self.fuzzy_key}",
                   "User-Agent": "Archive-sourcefind/1.0"}
        data = None
        for url in FUZZY_URLS:
            try:
                field = "file" if "api-next" in url else "image"
                r = requests.post(url, headers=headers, files={field: ("image.jpg", img, "image/jpeg")}, timeout=30)
            except Exception as e:
                self.log(f"  ⚠ FuzzySearch: {e}")
                self.errors.append(f"FuzzySearch: {e}")
                continue
            if r.status_code in (401, 403):
                if not self._fuzzy_warned:
                    self.log(f"  ✗ FuzzySearch не прийняв ключ (код {r.status_code})")
                    self._fuzzy_warned = True
                return []
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    data = None
                break
        if data is None:
            return []
        items = data if isinstance(data, list) else (data.get("matches") or data.get("results") or [])
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            info = it.get("site_info") or {}
            site = _norm_site(it.get("site") or (info.get("site") if isinstance(info, dict) else None)
                              or (next(iter(info)) if isinstance(info, dict) and info else ""))
            sid = it.get("site_id_str") or it.get("site_id") or it.get("id")
            if not site:
                site, sid = _site_from_url(it.get("url") or "")
            try:
                dist = float(it.get("distance"))
            except (TypeError, ValueError):
                dist = 99.0
            if dist > FUZZY_MAYBE or not site:
                continue
            artists = it.get("artists") or []
            out.append({"site": site, "site_id": str(sid) if sid is not None else None,
                        "url": page_url(site, sid) or it.get("url"), "artists": artists,
                        "method": "fuzzy", "score": dist, "sure": dist <= FUZZY_SURE})
        return out

    def _iqdb(self, img: bytes) -> list:
        dl = self.e621()
        try:
            dl._rate_limit()
            from furry_archiver import E621_API_BASE
            r = dl.session.post(f"{E621_API_BASE}/iqdb_queries.json",
                                files={"file": ("image.jpg", img, "image/jpeg")}, timeout=60)
            if r.status_code == 405:
                return []
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            self.log(f"  ⚠ e621 IQDB: {e}")
            self.errors.append(f"e621 IQDB: {e}")
            return []
        items = data if isinstance(data, list) else (data.get("posts") or [])
        out = []
        from furry_archiver import e621_norm_post
        for it in items[:5]:
            if not isinstance(it, dict):
                continue
            post = it.get("post") if isinstance(it.get("post"), dict) else it
            if isinstance(post.get("post"), dict):
                post = post["post"]
            try:
                score = float(it.get("score"))        # схожість IQDB, 0–100
            except (TypeError, ValueError):
                continue
            if score < IQDB_MAYBE or not post.get("id"):
                continue
            post = e621_norm_post(post)
            out.append(self._e621_cand(post, "iqdb", score, score >= IQDB_SURE))
        return out

    # ── теги для обраного джерела ─────────────────────────────────────
    def tags_for(self, cand: dict) -> dict | list | None:
        if cand["site"] == "e621":
            post = cand.get("_post")
            if not post or not post.get("tags"):
                post = self.e621().get_post(cand["site_id"])
            if not post:
                return None
            cand.setdefault("artists", (post.get("tags") or {}).get("artist") or [])
            return self.e621()._extract_e621_tags(post)
        if cand["site"] == "inkbunny" and str(cand.get("site_id") or "").isdigit():
            ib = self.inkbunny()
            if ib and ib.sid:
                return ib.get_keywords([int(cand["site_id"])]).get(int(cand["site_id"])) or None
        return None


def pick(cands: list) -> dict | None:
    """Найкращий впевнений кандидат: e621 → Inkbunny → решта; в межах сайту — точніший."""
    sure = [c for c in cands if c.get("sure")]
    if not sure:
        return None

    def key(c):
        q = -c["score"] if c["method"] == "iqdb" else (c["score"] if c["method"] == "fuzzy" else -1000)
        return (SITE_PRIORITY.get(c["site"], 9), q)
    return sorted(sure, key=key)[0]


def extra_links(chosen: dict, cands: list) -> list:
    """Інші адреси тієї ж роботи (напр. Inkbunny із джерел e621-поста)."""
    links = []
    for u in chosen.get("sources") or []:
        site, sid = _site_from_url(u)
        if site and site != chosen["site"]:
            links.append({"site": site, "site_id": sid, "url": page_url(site, sid) or u})
    for c in cands:
        if c is not chosen and c.get("sure") and c["site"] != chosen["site"]:
            links.append({"site": c["site"], "site_id": c.get("site_id"), "url": c.get("url")})
    seen, out = set(), []
    for l in links:
        if l["url"] and l["url"] not in seen:
            seen.add(l["url"]); out.append(l)
    return out


def _shrink(content: bytes, ext: str) -> bytes:
    """Картинка для відправки: JPEG до 1600 px — швидше й у межах лімітів сервісів."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(content))
        try:
            im.seek(0)
        except Exception:
            pass
        im = im.convert("RGB")
        im.thumbnail((1600, 1600))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        return buf.getvalue()
    except Exception:
        return content


def clean_cands(cands: list) -> str:
    return json.dumps([{k: v for k, v in c.items() if not k.startswith("_")} for c in cands], ensure_ascii=False)
