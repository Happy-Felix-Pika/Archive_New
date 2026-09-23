"""Джерело Inkbunny для вкладки «Онлайн».

Логіка — без змін з колишнього live_sources/inkbunny_source.py. Базовий
клас приходить від застосунку «Онлайн» через ядро (build(Base)).
"""


def build(LiveSourceBase):

    class InkbunnySource(LiveSourceBase):
        id        = "inkbunny"
        label     = "Inkbunny"
        id_prefix = "ib_"

        credential_fields = [
            {"key": "username", "label": "юзернейм Inkbunny", "type": "text"},
            {"key": "password", "label": "пароль Inkbunny",    "type": "password"},
        ]

        # Структурованого тег-пошуку, як у e621, немає, і немає певного
        # знання про їхнє API улюбленого — тому улюблене локальне
        supports_tags           = False
        supports_pools          = True
        supports_real_favorites = False
        supports_favs_of_others = True   # ПЕРЕГЛЯД чужого улюбленого (read-only)

        def __init__(self, credentials):
            super().__init__(credentials)
            self._dl = None

        def _downloader(self):
            if self._dl is None:
                from furry_archiver import Config as ArchiverConfig, InkbunnyDownloader
                self._dl = InkbunnyDownloader(
                    ArchiverConfig(),
                    username_override=self.credentials.get("username"),
                    password_override=self.credentials.get("password"),
                )
            return self._dl

        def is_authorized(self) -> bool:
            if not self.is_configured():
                return False
            try:
                dl = self._downloader()
                return bool(dl and getattr(dl, "sid", None))
            except Exception:
                return False

        def _refetch_full_details(self, dl, sub_ids: list) -> dict:
            """Груповий запит деталей для списку submission_id — дані з пошуку
            (_search.php) ненадійні, а _submissions.php гарантовано працює."""
            from furry_archiver import INKBUNNY_API_BASE
            full_by_id = {}
            if not sub_ids:
                return full_by_id
            dl._rate_limit()
            try:
                r = dl.session.post(f"{INKBUNNY_API_BASE}_submissions.php", data={
                    "sid": dl.sid, "submission_ids": ",".join(sub_ids),
                    "show_description": "no", "show_pools": "yes"
                })
                r.raise_for_status()
                for full in r.json().get("submissions", []):
                    full_by_id[str(full.get("submission_id"))] = full
            except Exception as e:
                print(f"  ⚠ Inkbunny деталі: {e}")
            return full_by_id

        @staticmethod
        def _extract_file_fields(full: dict) -> tuple:
            files = full.get("files", []) or []
            f0 = files[0] if files else {}
            file_url = f0.get("file_url_full") or f0.get("file_url_screen") or ""
            thumb_url = (f0.get("thumbnail_url_medium_noncustom") or f0.get("thumbnail_url_medium")
                         or f0.get("thumbnail_url_large_noncustom") or f0.get("file_url_screen") or file_url)
            ext = file_url.rsplit(".", 1)[-1].lower() if "." in file_url else ""
            return file_url, thumb_url, ext

        def search(self, *, tags="", artist="", exclude="", sort="", pool_id=None,
                   page=1, limit=60):
            dl = self._downloader()

            if pool_id:
                pool_name, subs = dl.get_pool_posts(int(pool_id))
                if not subs:
                    raise RuntimeError(f"Pool {pool_id} не знайдено на Inkbunny, або в ньому немає доступних робіт")
                full_by_id = {str(s.get("submission_id")): s for s in subs}
            else:
                pool_name = None
                # Inkbunny сам підтримує виключення "-слово" у текстовому пошуку
                query_parts = [tags] if tags else []
                if exclude:
                    query_parts.extend(f"-{w}" for w in exclude.split() if w)
                query = " ".join(query_parts)

                if artist:
                    subs = dl.search_artist_submissions(artist, limit=limit)
                else:
                    # сортування за лайками пошук напряму не дає — рахуємо самі нижче
                    subs = dl.search_by_keywords(query, limit=limit, page=page, orderby='create_datetime')

                sub_ids    = [str(s.get("submission_id")) for s in subs if s.get("submission_id")]
                full_by_id = self._refetch_full_details(dl, sub_ids)

            results = []
            for s in subs:
                sid  = str(s.get("submission_id"))
                full = full_by_id.get(sid, s)
                file_url, thumb_url, ext = self._extract_file_fields(full)

                # Показуємо тільки фото/відео — тексти й музику пропускаємо
                if ext not in ('png', 'jpg', 'jpeg', 'gif', 'webp', 'webm', 'mp4', 'swf'):
                    continue

                keywords = [kw.get("keyword_name", "").strip()
                            for kw in (full.get("keywords") or []) if kw.get("keyword_name")]

                r_pool_id, r_pool_name = None, None
                pools_list = full.get("pools") or []
                if pools_list:
                    r_pool_id   = pools_list[0].get("pool_id")
                    r_pool_name = pools_list[0].get("name")
                if pool_id:
                    r_pool_id, r_pool_name = int(pool_id), pool_name

                try:
                    fav_count = int(full.get("favorites_count") or 0)
                except (TypeError, ValueError):
                    fav_count = 0

                results.append({
                    "id":        s.get("submission_id"),
                    "source":    "inkbunny",
                    "post_url":  f"https://inkbunny.net/s/{s.get('submission_id')}",
                    "file_url":  file_url,
                    "thumb_url": thumb_url,
                    "ext":       ext,
                    "artists":   [s.get("username")],
                    "tags":      keywords,
                    "pool_id":   r_pool_id,
                    "pool_name": r_pool_name,
                    "is_video":  ext in ("webm", "mp4"),
                    "is_flash":  ext == "swf",
                    "_fav_count": fav_count,
                })

            if sort == "score":
                results.sort(key=lambda r: r["_fav_count"], reverse=True)
            elif sort == "score_asc":
                results.sort(key=lambda r: r["_fav_count"])
            elif sort == "id":
                results.reverse()

            for r in results:
                del r["_fav_count"]

            return results[:limit], pool_name

        def search_favorites(self, *, username=None, tags="", exclude="", sort="", page=1, limit=60):
            """Перегляд улюбленого БУДЬ-ЯКОГО юзера Inkbunny (публічна інформація)."""
            if not username:
                raise ValueError("Вкажи юзернейм Inkbunny, чиє улюблене переглянути")
            dl = self._downloader()
            subs = dl.get_favorites_of_user(username, limit=limit, page=page)

            sub_ids    = [str(s.get("submission_id")) for s in subs if s.get("submission_id")]
            full_by_id = self._refetch_full_details(dl, sub_ids)

            results = []
            for s in subs:
                sid  = str(s.get("submission_id"))
                full = full_by_id.get(sid, s)
                file_url, thumb_url, ext = self._extract_file_fields(full)
                keywords = [kw.get("keyword_name", "").strip()
                            for kw in (full.get("keywords") or []) if kw.get("keyword_name")]
                pools_list = full.get("pools") or []
                pool_id   = pools_list[0].get("pool_id") if pools_list else None
                pool_name = pools_list[0].get("name") if pools_list else None
                results.append({
                    "id":        s.get("submission_id"),
                    "source":    "inkbunny",
                    "post_url":  f"https://inkbunny.net/s/{s.get('submission_id')}",
                    "file_url":  file_url,
                    "thumb_url": thumb_url,
                    "ext":       ext,
                    "artists":   [s.get("username")],
                    "tags":      keywords,
                    "pool_id":   pool_id,
                    "pool_name": pool_name,
                    "is_video":  ext in ("webm", "mp4"),
                    "is_flash":  ext == "swf",
                })
            return results

        def refresh_details(self, post_ids: list) -> dict:
            """Оновлює thumb_url/file_url для локально збережених улюблених."""
            dl = self._downloader()
            if not (dl and dl.sid):
                return {}
            full_by_id = self._refetch_full_details(dl, [str(p) for p in post_ids])
            out = {}
            for pid, full in full_by_id.items():
                file_url, thumb_url, ext = self._extract_file_fields(full)
                out[pid] = {"file_url": file_url, "thumb_url": thumb_url, "ext": ext}
            return out

    return InkbunnySource
