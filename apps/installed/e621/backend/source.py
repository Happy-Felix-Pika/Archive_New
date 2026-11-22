"""Джерело e621 для вкладки «Онлайн».

Логіка — без змін з колишнього live_sources/e621_source.py. Єдина різниця:
базовий клас приходить від застосунку «Онлайн» через ядро (build(Base)),
а не імпортом — застосунки не імпортують код один одного напряму.
"""


def build(LiveSourceBase):

    class E621Source(LiveSourceBase):
        id        = "e621"
        label     = "e621"
        id_prefix = "e621_"

        credential_fields = [
            {"key": "username", "label": "юзернейм e621",                   "type": "text"},
            {"key": "api_key",  "label": "API-ключ (Профіль → API Access)", "type": "password"},
        ]

        supports_tags           = True
        supports_pools          = True
        supports_real_favorites = True
        supports_favs_of_others = True

        # Поля пошуку цього джерела. Ядро малює рівно цей список і
        # нічого більше — щоб змінити набір, правиться ТІЛЬКИ цей файл.
        #
        # Поля «автор» тут свідомо немає: на e621 автор — це звичайний
        # тег, пишеться прямо в теги як artist:ім'я. Окреме поле лише
        # збивало з пантелику, бо насправді нічого не давало.
        search_fields = [
            {"id": "tags", "type": "text", "width": 240,
             "placeholder": "Теги e621 (rating:safe order:score …)",
             "autocomplete": "tags"},
            {"id": "exclude", "type": "text", "width": 180,
             "placeholder": "Виключити теги…", "autocomplete": "tags"},
            {"id": "sort", "type": "select", "options": [
                {"value": "",          "label": "За замовчуванням"},
                {"value": "score",     "label": "Найбільше лайків"},
                {"value": "score_asc", "label": "Найменше лайків"},
                {"value": "id",        "label": "Від найстаріших"},
                {"value": "id_desc",   "label": "Від найновіших"},
            ]},
            {"id": "favs_username", "type": "text", "width": 170,
             "placeholder": "чиєсь улюблене (юзернейм)…"},
        ]

        def __init__(self, credentials):
            super().__init__(credentials)
            self._dl = None

        def _downloader(self):
            if self._dl is None:
                from furry_archiver import Config as ArchiverConfig, E621Downloader
                self._dl = E621Downloader(
                    ArchiverConfig(),
                    username_override=self.credentials.get("username"),
                    api_key_override=self.credentials.get("api_key"),
                )
            return self._dl

        def is_authorized(self) -> bool:
            if not self.is_configured():
                return False
            try:
                return self._downloader() is not None
            except Exception:
                return False

        @staticmethod
        def _build_result(dl, post: dict) -> dict:
            file_info = post.get("file", {}) or {}
            preview   = post.get("preview", {}) or {}
            ext       = file_info.get("ext")
            tags_obj  = post.get("tags", {}) or {}
            all_tags  = []
            for cat in ("artist", "character", "species", "copyright", "general", "lore", "meta"):
                all_tags.extend(tags_obj.get(cat, []))

            pool_ids  = post.get("pools") or []
            pool_id   = pool_ids[0] if pool_ids else None
            pool_name = None
            if pool_id:
                pool_name, _ = dl._get_pool_info(post)

            # e621 кладе деякі технічні мета-теги (обмеження публікації, не
            # імена авторів) у категорію "artist" — відфільтровуємо їх
            NON_ARTIST_TAGS = {"conditional_dnp", "avoid_posting"}
            real_artists = [a for a in tags_obj.get("artist", []) if a not in NON_ARTIST_TAGS]

            return {
                "id":        post.get("id"),
                "source":    "e621",
                "post_url":  f"https://e621.net/posts/{post.get('id')}",
                "file_url":  file_info.get("url"),
                "thumb_url": preview.get("url") or file_info.get("url"),
                "ext":       ext,
                "width":     file_info.get("width"),
                "height":    file_info.get("height"),
                "artists":   real_artists,
                "tags":      all_tags,
                "pool_id":   pool_id,
                "pool_name": pool_name,
                "is_video":  ext in ("webm", "mp4"),
                "is_flash":  ext == "swf",
                "is_favorite": bool(post.get("is_favorited", False)),
            }

        def search(self, *, tags="", artist="", exclude="", sort="", pool_id=None,
                   page=1, limit=60):
            dl = self._downloader()
            pool_name = None
            if pool_id:
                pool_name, posts = dl.get_pool_posts(int(pool_id))
                if pool_name is None:
                    raise RuntimeError(f"Pool {pool_id} не знайдено на e621 (можливо видалений)")
            else:
                parts = []
                if artist: parts.append(f"artist:{artist}")
                if tags:   parts.append(tags)
                if exclude:
                    parts.extend(f"-{t}" for t in exclude.split() if t)
                if sort:
                    parts.append(f"order:{sort}")
                query = " ".join(parts)
                posts = dl.search_posts(query or "order:score", limit=limit, page=page)
                self._report_tag_total(dl, f"{artist and 'artist:' + artist} {tags}", limit)

            results = [self._build_result(dl, p) for p in posts]
            return results, pool_name

        # e621 не каже, скільки всього результатів. Для запиту з ОДНОГО тегу
        # (автор, вид…) кількість відома з самого тегу — тоді «з M» (приблизно:
        # виключення й видалені пости її трохи зменшують). Сайт віддає не далі 750-ї сторінки.
        def _report_tag_total(self, dl, query, limit):
            words = [w for w in query.split() if w and not w.startswith("-")
                     and not w.split(":", 1)[0] in ("order", "rating", "status", "date")]
            if len(words) != 1:
                return
            w = words[0]
            if ":" in w:
                pref, rest = w.split(":", 1)
                if pref not in ("artist", "species", "character", "copyright", "general", "meta", "lore", "invalid"):
                    return
                w = rest
            n = dl.tag_post_count(w) if hasattr(dl, "tag_post_count") else None
            if n:
                self.report_total(pages=min(750, -(-n // limit)), count=n, approx=True)

        def search_favorites(self, *, username=None, tags="", exclude="", sort="", page=1, limit=60):
            dl = self._downloader()
            target_username = username or self.credentials.get("username", "")
            if not target_username:
                raise ValueError("Не вказано юзернейм e621")
            # fav:юзернейм комбінується з будь-якими тегами й сортуванням
            # через звичайний пошук — і працює для БУДЬ-ЯКОГО юзера
            parts = [f"fav:{target_username}"]
            if tags:    parts.append(tags)
            if exclude:
                parts.extend(f"-{t}" for t in exclude.split() if t)
            if sort:    parts.append(f"order:{sort}")
            posts = dl.search_posts(" ".join(parts), limit=limit, page=page)
            return [self._build_result(dl, p) for p in posts]

        def toggle_favorite(self, post_id):
            dl = self._downloader()
            post = dl.get_post(post_id) or {}
            currently_fav = bool(post.get("is_favorited", False))
            ok = dl.remove_favorite(post_id) if currently_fav else dl.add_favorite(post_id)
            if not ok:
                raise RuntimeError("Не вдалося звернутись до e621")
            return not currently_fav

        def tag_autocomplete(self, q: str) -> list:
            try:
                import requests as req
                r = req.get(
                    "https://e621.net/tags/autocomplete.json",
                    params={"search[name_matches]": f"{q}*", "expiry": 1},
                    headers={"User-Agent": "FurryArchiveDashboard/1.0"},
                    timeout=10,
                )
                r.raise_for_status()
                data = r.json()
                return [
                    {"name": t.get("name"), "post_count": t.get("post_count"), "category": t.get("category")}
                    for t in data if t.get("name")
                ][:15]
            except Exception:
                return []

    return E621Source
