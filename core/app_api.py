"""
Те, що ядро дає застосунку.

Головне правило системи: застосунок НЕ імпортує нічого з core.* сам.
Усе, що йому потрібно, приходить одним об'єктом у register(core).
Поки це правило тримається, внутрішню будову ядра можна міняти,
не ламаючи застосунки; щойно воно порушене — стабілізувати вже
нічого.

    def register(core):
        core.api.add_routes(bp)
        core.jobs.register("sync", handler)
        core.events.on("file.created", on_new_file)
"""

import json


class _Settings:
    """Налаштування застосунку. Кожен бачить тільки свої."""

    def __init__(self, app_id, db_factory):
        self._app_id = app_id
        self._db = db_factory

    def get(self, key, default=None):
        db = self._db()
        try:
            with db.cursor() as cur:
                cur.execute("SELECT value FROM app_settings WHERE app_id=%s AND `key`=%s",
                            (self._app_id, key))
                row = cur.fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value"])
            except Exception:
                return row["value"]
        finally:
            db.close()

    def set(self, key, value):
        db = self._db()
        try:
            with db.cursor() as cur:
                cur.execute("""INSERT INTO app_settings (app_id, `key`, value) VALUES (%s,%s,%s)
                               ON DUPLICATE KEY UPDATE value=VALUES(value)""",
                            (self._app_id, key, json.dumps(value, ensure_ascii=False)))
            db.commit()
        finally:
            db.close()

    def all(self) -> dict:
        db = self._db()
        try:
            with db.cursor() as cur:
                cur.execute("SELECT `key`, value FROM app_settings WHERE app_id=%s", (self._app_id,))
                rows = cur.fetchall()
            out = {}
            for r in rows:
                try:
                    out[r["key"]] = json.loads(r["value"])
                except Exception:
                    out[r["key"]] = r["value"]
            return out
        finally:
            db.close()


class _Jobs:
    """Фонові задачі. Тип задачі автоматично отримує префікс застосунку,
    щоб два застосунки не могли перехопити задачі один одного."""

    def __init__(self, app_id):
        self._app_id = app_id

    def _full(self, name):
        return name if name.startswith(f"{self._app_id}.") else f"{self._app_id}.{name}"

    def register(self, name, fn):
        from core.jobs import register_handler
        register_handler(self._full(name), fn)

    def create(self, name, params=None, user_id=None):
        from core.jobs import create_job
        return create_job(self._full(name), params, user_id)

    def list(self, name=None, limit=50):
        from core.jobs import list_jobs
        return list_jobs(self._full(name) if name else None, limit)


class _Api:
    """Реєстрація HTTP-ендпоінтів застосунку."""

    def __init__(self, app_id, flask_app, url_prefix):
        self._app_id = app_id
        self._flask = flask_app
        self.url_prefix = url_prefix

    def add_routes(self, blueprint, at_root: bool = False):
        """
        Blueprint застосунку монтується на /api/apps/<id>/...

        at_root=True лишає адреси такими, як їх написав застосунок —
        це потрібно тим, хто переїхав у застосунок з ядра і мусить
        зберегти вже наявні адреси (інакше зламається фронтенд і
        зовнішні архіви). Нові застосунки так робити не повинні:
        двоє охочих до однієї адреси просто не зареєструються.
        """
        if at_root:
            self._flask.register_blueprint(blueprint)
        else:
            self._flask.register_blueprint(blueprint, url_prefix=self.url_prefix)


class CoreAPI:
    """Об'єкт, який отримує застосунок у register()."""

    def __init__(self, app_id, permissions, flask_app):
        self.app_id = app_id
        self.permissions = list(permissions or [])
        self.url_prefix = f"/api/apps/{app_id}"

        self.settings = _Settings(app_id, self._db)
        self.jobs = _Jobs(app_id)
        self.api = _Api(app_id, flask_app, self.url_prefix)

    # ── доступ до даних ──────────────────────────────────────

    def _db(self):
        from core.db import get_db
        return get_db()

    @property
    def db(self):
        """
        З'єднання з базою. Застосунок відповідає за свої таблиці
        (з префіксом зі свого маніфесту) і читає таблиці ядра.

        Чесно: це звичайне з'єднання, і технічно застосунок може
        звідси дістатись куди завгодно. Справжня ізоляція вимагала б
        окремих процесів — для свого коду це зайве, для чужого
        недостатньо. Тому чужий код сюди краще не ставити.
        """
        return self._db()

    @property
    def files(self):
        import core.files_api as m
        return m

    @property
    def storage(self):
        from core.storage_api import get_storage
        return get_storage()

    @property
    def events(self):
        import core.events as m
        return m

    @property
    def services(self):
        """API інших застосунків. Див. core/services.py."""
        import core.services as m
        return m

    def current_user(self):
        from core.auth import current_user
        return current_user()

    def current_user_id(self):
        from core.auth import current_user_id
        return current_user_id()
