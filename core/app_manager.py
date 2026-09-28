"""
Ядро: менеджер застосунків.

Знаходить теки застосунків, перевіряє маніфести, вирішує залежності,
проганяє їхні міграції й підключає до Flask. Меню й сторінки береться
з маніфесту — не виконуючи код застосунку, тому вимкнений застосунок
просто зникає з інтерфейсу без жодних побічних ефектів.

Головне правило надійності: падіння одного застосунку НЕ валить
сервер. Він отримує статус 'error' і лишається видимим у розділі
застосунків разом із текстом помилки.
"""

import importlib.util
import json
import os
import re

from core.app_api import CoreAPI
from core.db import get_db

CORE_VERSION = "1.0.0"
CORE_API_VERSION = 1

APPS_DIR = os.environ.get("ARCHIVE_APPS_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps", "installed")

REQUIRED_FIELDS = ("id", "name", "version", "core_api_version", "min_core_version")


def _ver(s) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(s or "0")))


class AppManager:
    def __init__(self):
        self.apps = {}       # id -> {"manifest":.., "module":.., "status":.., "error":..}
        self._recorded = {}  # id -> (status, error), записане під час цього завантаження

    # ── читання й перевірка ──────────────────────────────────

    def discover(self) -> dict:
        found = {}
        if not os.path.isdir(APPS_DIR):
            return found
        for name in sorted(os.listdir(APPS_DIR)):
            mpath = os.path.join(APPS_DIR, name, "manifest.json")
            if not os.path.isfile(mpath):
                continue
            try:
                with open(mpath, encoding="utf-8") as f:
                    manifest = json.load(f)
            except Exception as e:
                self._record(name, "0", "error", f"Некоректний manifest.json: {e}")
                continue
            problem = self._validate(manifest, name)
            if problem:
                self._record(manifest.get("id", name), manifest.get("version", "0"),
                             "incompatible", problem)
                continue
            found[manifest["id"]] = manifest
        return found

    def _validate(self, m: dict, dirname: str) -> str | None:
        for field in REQUIRED_FIELDS:
            if field not in m:
                return f"Відсутнє обов'язкове поле '{field}'"
        if not re.fullmatch(r"[a-z0-9_-]+", str(m["id"])):
            return "id може містити лише a-z, 0-9, _ і -"
        if m["id"] != dirname:
            return f"id '{m['id']}' не збігається з іменем теки '{dirname}'"
        if int(m["core_api_version"]) != CORE_API_VERSION:
            return (f"Потребує Core API v{m['core_api_version']}, "
                    f"а ядро надає v{CORE_API_VERSION}")
        if _ver(m["min_core_version"]) > _ver(CORE_VERSION):
            return f"Потребує ядро {m['min_core_version']}+, наявне {CORE_VERSION}"
        return None

    # ── залежності ───────────────────────────────────────────

    def _order(self, found: dict, enabled: set) -> list:
        """Порядок завантаження: залежність раніше за того, хто її просить."""
        ordered, done = [], set()

        def visit(aid, chain):
            if aid in done:
                return True
            if aid in chain:
                self._record(aid, found[aid]["version"], "error", "Циклічна залежність")
                return False
            m = found.get(aid)
            if not m:
                return False
            for dep, spec in (m.get("dependencies") or {}).items():
                if dep not in found:
                    self._record(aid, m["version"], "incompatible",
                                 f"Потребує застосунок '{dep}', якого немає")
                    return False
                if dep not in enabled:
                    self._record(aid, m["version"], "incompatible",
                                 f"Потребує застосунок '{dep}', а він вимкнений")
                    return False
                if _ver(found[dep]["version"]) < _ver(spec):
                    self._record(aid, m["version"], "incompatible",
                                 f"Потребує '{dep}' {spec}, наявна {found[dep]['version']}")
                    return False
                if not visit(dep, chain | {aid}):
                    self._record(aid, m["version"], "incompatible",
                                 f"Залежність '{dep}' не завантажилась")
                    return False
            done.add(aid)
            ordered.append(aid)
            return True

        for aid in found:
            if aid in enabled:
                visit(aid, set())
        return ordered

    def dependents_of(self, app_id: str) -> list:
        """Хто зламається, якщо це вимкнути."""
        out = []
        for aid, a in self.apps.items():
            if app_id in (a["manifest"].get("dependencies") or {}):
                out.append(aid)
        return out

    # ── міграції застосунку ──────────────────────────────────

    def _migrate(self, m: dict, db):
        prefix = m.get("table_prefix") or f"{m['id']}_"
        # Застосунки, що переїхали з ядра, володіють таблицями з історичними
        # іменами (artists, pending_duplicates...). Перейменовувати їх —
        # величезна міграція заради косметики, тож застосунок просто
        # оголошує їх у маніфесті. Перевірка префікса лишається захистом
        # від описки, а не від злого наміру.
        owned = set(m.get("legacy_tables") or [])
        mig_dir = os.path.join(APPS_DIR, m["id"], "migrations")
        if not os.path.isdir(mig_dir):
            return
        with db.cursor() as cur:
            cur.execute("SELECT filename FROM app_migrations WHERE app_id=%s", (m["id"],))
            applied = {r["filename"] for r in cur.fetchall()}

            for fn in sorted(os.listdir(mig_dir)):
                if not fn.endswith(".sql") or fn in applied:
                    continue
                with open(os.path.join(mig_dir, fn), encoding="utf-8") as f:
                    sql = f.read()

                # Коментарі прибираємо ДО перевірки імен: інакше згадка
                # "CREATE TABLE" у поясненні сприймалась як справжня
                # таблиця, і застосунок не вантажився через власний
                # коментар.
                body = re.sub(r"--[^\n]*", "", sql)

                # Перевірка префікса — захист від описки, а не від зловмисника:
                # застосунок і так має звичайне з'єднання з базою.
                for tbl in re.findall(
                        r"(?:CREATE|ALTER|DROP)\s+TABLE\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?`?(\w+)`?",
                        body, re.I):
                    if not tbl.startswith(prefix) and tbl not in owned:
                        raise RuntimeError(
                            f"{fn}: таблиця '{tbl}' поза префіксом '{prefix}'")

                # (коментарі вже прибрані вище — крапка з комою всередині
                # коментаря інакше різала б міграцію навпіл)
                for stmt in [s.strip() for s in body.split(";") if s.strip()]:
                    cur.execute(stmt)
                cur.execute("INSERT INTO app_migrations (app_id, filename) VALUES (%s,%s)",
                            (m["id"], fn))
        db.commit()

    # ── завантаження ─────────────────────────────────────────

    def load_all(self, flask_app):
        found = self.discover()
        db = get_db()
        try:
            with db.cursor() as cur:
                cur.execute("SELECT id, status FROM apps")
                known = {r["id"]: r["status"] for r in cur.fetchall()}
        finally:
            db.close()

        # Новий застосунок вмикається одразу: його поклали в теку навмисно.
        # Не вантажимо лише явно ВИМКНЕНІ. Статуси 'error' та 'incompatible'
        # означають "минулого разу не вийшло", а не "не треба" — причину
        # могли вже виправити, тож пробуємо знову при кожному старті.
        enabled = {aid for aid in found if known.get(aid, "enabled") != "disabled"}

        for aid in self._order(found, enabled):
            m = found[aid]
            try:
                db = get_db()
                try:
                    self._migrate(m, db)
                finally:
                    db.close()

                module = self._import(aid, m.get("backend") or "backend")
                core = CoreAPI(aid, m.get("permissions"), flask_app)
                if hasattr(module, "register"):
                    module.register(core)

                self.apps[aid] = {"manifest": m, "module": module,
                                  "status": "enabled", "error": None}
                self._record(aid, m["version"], "enabled", None)
                print(f"  ✓ Застосунок '{aid}' v{m['version']}")
            except Exception as e:
                # Зламаний застосунок не має валити архів
                msg = f"{type(e).__name__}: {e}"
                self.apps[aid] = {"manifest": m, "module": None,
                                  "status": "error", "error": msg}
                self._record(aid, m.get("version", "0"), "error", msg)
                print(f"  ⚠ Застосунок '{aid}' не завантажено: {msg}")

        # Незавантажені теж показуємо в списку. Статус беремо той, що
        # з'ясувався ПІД ЧАС завантаження (напр. "несумісний: потребує
        # вимкнений застосунок"), а не прочитаний із бази до перевірки —
        # інакше застосунок, якому бракує залежності, показувався б як
        # "увімкнено", хоча насправді не працює.
        for aid, m in found.items():
            if aid not in self.apps:
                status, error = self._recorded.get(aid, (known.get(aid, "disabled"), None))
                self.apps[aid] = {"manifest": m, "module": None, "status": status, "error": error}

    def _import(self, app_id: str, backend_pkg: str):
        """
        Підключає backend застосунку САМЕ ЯК ПАКЕТ.

        Інакше всередині не працюють відносні імпорти (from . import ...),
        а застосунок із кількох файлів без них не написати.
        """
        import sys

        pkg_dir = os.path.join(APPS_DIR, app_id, backend_pkg)
        path = os.path.join(pkg_dir, "__init__.py")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"немає {backend_pkg}/__init__.py")

        mod_name = f"archive_app_{app_id.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(
            mod_name, path, submodule_search_locations=[pkg_dir])
        module = importlib.util.module_from_spec(spec)
        # реєструємо ДО виконання: відносні імпорти шукають пакет тут
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(mod_name, None)
            raise
        return module

    # ── стан ─────────────────────────────────────────────────

    def _record(self, app_id, version, status, error):
        self._recorded[app_id] = (status, error)
        db = get_db()
        try:
            with db.cursor() as cur:
                cur.execute("""INSERT INTO apps (id, version, status, error_message, updated_at)
                               VALUES (%s,%s,%s,%s,NOW())
                               ON DUPLICATE KEY UPDATE version=VALUES(version),
                                   status=VALUES(status), error_message=VALUES(error_message),
                                   updated_at=NOW()""",
                            (app_id, str(version), status, error))
            db.commit()
        finally:
            db.close()

    def set_enabled(self, app_id: str, enabled: bool) -> tuple[bool, str]:
        """
        Вмикає/вимикає застосунок. Зміна діє після перезапуску сервера:
        прибрати з працюючого Flask уже зареєстровані маршрути не можна,
        і вдавати, що можна, — гірше, ніж сказати прямо.
        """
        if not enabled:
            blockers = [a for a in self.dependents_of(app_id)
                        if self.apps.get(a, {}).get("status") == "enabled"]
            if blockers:
                return False, ("Не можна вимкнути: від нього залежать — "
                               + ", ".join(blockers))
        info = self.apps.get(app_id)
        version = info["manifest"]["version"] if info else "0"
        self._record(app_id, version, "enabled" if enabled else "disabled", None)
        if info:
            info["status"] = "enabled" if enabled else "disabled"
        return True, "Застосовано. Зміна набуде чинності після перезапуску."

    # ── інтерфейс ────────────────────────────────────────────

    def ui(self, lang="uk", is_admin=False) -> dict:
        """Пункти меню й сторінки увімкнених застосунків — з маніфестів."""
        menu, pages = [], []
        for aid, a in self.apps.items():
            if a["status"] != "enabled":
                continue
            m = a["manifest"]
            for item in m.get("menu", []):
                if item.get("admin_only") and not is_admin:
                    continue
                label = item.get("label")
                if isinstance(label, dict):
                    label = label.get(lang) or label.get("uk") or item.get("id")
                menu.append({**item, "label": label, "app_id": aid})
            # файли, які вантажаться одразу при старті: вони реєструють
            # кнопки застосунку в зонах розширення ядра
            if m.get("scripts"):
                pages.append({"id": f"{aid}-scripts", "app_id": aid,
                              "scripts": [f"/apps/{aid}/{s}" for s in m["scripts"]]})

            # секція налаштувань застосунку (необов'язкова)
            cfg = m.get("config")
            if cfg and cfg.get("js"):
                pages.append({"id": f"{aid}-config", "app_id": aid,
                              "config_js": f"/apps/{aid}/{cfg['js']}"})
            for p in m.get("pages", []):
                page = {**p, "app_id": aid}
                for key in ("js", "css"):
                    if p.get(key):
                        page[key] = f"/apps/{aid}/{p[key]}"
                pages.append(page)
        menu.sort(key=lambda x: x.get("order", 100))
        return {"menu": menu, "pages": pages}

    def listing(self) -> list:
        out = []
        for aid, a in sorted(self.apps.items()):
            m = a["manifest"]
            out.append({
                "id": aid,
                "name": m.get("name", aid),
                "version": m.get("version"),
                "description": m.get("description", ""),
                "author": m.get("author", ""),
                "status": a["status"],
                "error": a["error"],
                "dependencies": m.get("dependencies") or {},
                "dependents": self.dependents_of(aid),
            })
        return out


manager = AppManager()
