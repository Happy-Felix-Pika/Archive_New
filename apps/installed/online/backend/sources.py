"""
Реєстр джерел для вкладки «Онлайн».

Раніше джерела були зашиті в код: SOURCES = {e621: ..., inkbunny: ...}.
Тепер кожне джерело — окремий застосунок, який сам себе реєструє тут.
Вимкнули застосунок e621 — його джерела у вкладці просто немає.

Щоб додати НОВЕ джерело (напр. Weasyl), зроби застосунок, який
залежить від 'online', успадкуй LiveSourceBase і в register():

    core.services.get("online.sources").register(WeasylSource)
"""


class LiveSourceBase:
    """
    Базовий клас джерела.

    Нащадок МУСИТЬ визначити:
      id                — унікальний слаг ("e621")
      label             — назва для інтерфейсу
      id_prefix         — префікс post_id у БД ("e621_")
      credential_fields — [{"key": "api_key", "label": "...", "type": "password"}]

    Необов'язкові прапорці:
      supports_tags, supports_pools, supports_real_favorites, supports_favs_of_others
    """

    id: str = None
    label: str = None
    id_prefix: str = None
    credential_fields: list = []

    supports_tags = True
    supports_pools = False
    supports_real_favorites = False
    supports_favs_of_others = False

    def __init__(self, credentials: dict):
        self.credentials = credentials or {}

    def is_configured(self) -> bool:
        return all(self.credentials.get(f["key"]) for f in self.credential_fields)

    def is_authorized(self) -> bool:
        return self.is_configured()

    def search(self, *, tags="", artist="", exclude="", sort="", pool_id=None, page=1, limit=60):
        raise NotImplementedError

    def toggle_favorite(self, post_id):
        raise NotImplementedError

    def search_favorites(self, *, username=None, tags="", exclude="", sort="", page=1, limit=60):
        raise NotImplementedError

    def refresh_details(self, post_ids: list) -> dict:
        return {}

    def tag_autocomplete(self, q: str) -> list:
        return []


# id -> клас. Живий словник: live.py читає його при кожному запиті, тож
# джерело, зареєстроване пізніше за імпорт live.py, теж буде видно.
SOURCES: dict = {}


class SourceRegistry:
    """Те, що застосунок «Онлайн» дає іншим застосункам як сервіс."""

    base = LiveSourceBase   # щоб джерело могло успадкуватись, не імпортуючи цей модуль

    def register(self, cls):
        if not cls.id:
            raise ValueError(f"У джерела {cls.__name__} не задано id")
        if not issubclass(cls, LiveSourceBase):
            raise TypeError(f"{cls.__name__} має успадковувати LiveSourceBase")
        SOURCES[cls.id] = cls

    def unregister(self, source_id):
        SOURCES.pop(source_id, None)

    def all(self) -> dict:
        return dict(SOURCES)


def all_sources() -> dict:
    """
    Усі джерела: застосунки (e621, Inkbunny...) плюс власні, додані
    користувачем через форму. Читається щоразу — додав джерело й воно
    одразу є, без перезапуску.
    """
    out = dict(SOURCES)
    try:
        from .custom import load_custom
        out.update(load_custom(LiveSourceBase))
    except Exception as e:
        print(f"  ⚠ Власні джерела: {e}")
    return out


def get_source(source_id: str, credentials: dict):
    cls = all_sources().get(source_id)
    return cls(credentials) if cls else None
