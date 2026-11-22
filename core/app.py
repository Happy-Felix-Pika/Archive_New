"""Створення Flask-застосунку, глобальний захист API, реєстрація blueprint'ів."""

import logging
import os
import time

from flask import Flask, jsonify, request as flask_request, send_from_directory, session
from flask_cors import CORS

from core.paths import _get_or_create_secret_key

# Корінь проєкту — тека, де лежить main.py (на рівень вище за core/).
# Рахуємо від __file__, а не від поточної теки: інакше сервер знаходить
# dashboard.html і static/ лише тоді, коли його запустили саме з кореня.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _QuietRequestFilter(logging.Filter):
    """
    Werkzeug за замовчуванням логує КОЖЕН HTTP-запит — навіть рутинний
    поллінг статусу раз на кілька секунд чи кожну картинку з проксі.
    Ховаємо успішні (2xx/3xx) запити до "шумних" ендпоінтів, але
    помилки (4xx/5xx) там же лишаємо видимими.
    """
    NOISY_PREFIXES = (
        "/api/status", "/api/live/proxy", "/api/live/tag-autocomplete",
        "/api/gallery/stream", "/api/gallery/meta", "/api/nc/paths-status",
        "/api/logs/stream", "/api/live/search",
    )
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if any(f'"GET {p}' in msg or f'"POST {p}' in msg for p in self.NOISY_PREFIXES):
            if any(f" {code} " in msg for code in ("200", "206", "304")):
                return False
        return True


# Усі /api/* ендпоінти, крім цих, вимагають активної сесії. Централізована
# перевірка — новий ендпоінт автоматично захищений, декоратор не потрібен.
_PUBLIC_PATHS = {
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/me",
    "/api/db/status",
    "/api/db/setup",
    "/api/branding",
    "/api/auth/forgot",
    "/api/auth/reset",
    "/api/auth/reset/check",
}

# Порядок не має значення — кожен blueprint самодостатній.
# ЯДРО: універсальний файловий архів. Нічого про авторів, зовнішні
# джерела чи дублікати тут немає — усе це застосунки в apps/installed/.
BLUEPRINT_MODULES = [
    "apps", "auth_api", "catalog", "federation", "files", "gallery",
    "jobs", "settings_api", "stats", "tags", "trash", "upload", "users", "version", "password",
]


def create_app() -> Flask:
    logging.getLogger("werkzeug").addFilter(_QuietRequestFilter())

    app = Flask(
        __name__,
        root_path=BASE_DIR,
        static_folder=os.path.join(BASE_DIR, "static"),
        static_url_path="/static",
    )
    app.secret_key = _get_or_create_secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,  # 30 днів
    )
    CORS(app, supports_credentials=True)

    # Повільні запити — у лог, щоб було видно, що саме гальмує
    # (SSE-потік логів живе довго за задумом — його не рахуємо).
    @app.before_request
    def _t_start():
        flask_request._t0 = time.monotonic()

    @app.after_request
    def _t_log(resp):
        t0 = getattr(flask_request, "_t0", None)
        if t0 is not None and not resp.is_streamed:
            dt = time.monotonic() - t0
            if dt >= 1.0:
                print(f"  ⏱ Повільний запит: {flask_request.method} {flask_request.path} — {dt:.1f} с")
        return resp

    @app.before_request
    def _require_auth():
        path = flask_request.path
        if not path.startswith("/api/"):
            return None  # статика, /, тощо — не чіпаємо
        if path in _PUBLIC_PATHS:
            return None
        if path.startswith("/api/external-access/"):
            # Ці ендпоінти для ІНШОГО сервера (не для юзера з сесією) — доступ
            # перевіряється ключем усередині самого хендлера, не сесією тут.
            return None
        if flask_request.method == "OPTIONS":
            return None  # CORS preflight
        if not session.get("user_id"):
            return jsonify({"error": "Потрібна авторизація"}), 401
        return None

    @app.route("/")
    def index():
        path = os.path.join(BASE_DIR, "dashboard.html")
        if not os.path.isfile(path):
            return (f"dashboard.html не знайдено.\n"
                    f"Шукав тут: {path}\n"
                    f"Поклади dashboard.html поруч з main.py.\n\n"
                    f"Вміст теки проєкту:\n  " +
                    "\n  ".join(sorted(os.listdir(BASE_DIR))), 500,
                    {"Content-Type": "text/plain; charset=utf-8"})
        return send_from_directory(BASE_DIR, "dashboard.html")

    # ── Встановлення як застосунок (PWA) ─────────────────────────────
    @app.route("/manifest.webmanifest")
    def pwa_manifest():
        """Опис застосунку для «Встановити» / «На головний екран»: назва —
        та сама, що в Конфігу (Назва архіву)."""
        from flask import Response
        import json as _json
        from core.config import load_config
        try:
            title = ((load_config().get("ui") or {}).get("title") or "Archive").strip()
        except Exception:
            title = "Archive"
        data = {
            "name": title,
            "short_name": title[:12],
            "id": "/",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#0d0f14",
            "theme_color": "#0d0f14",
            "icons": [
                {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {"src": "/static/icons/maskable-512.png", "sizes": "512x512", "type": "image/png",
                 "purpose": "maskable"},
            ],
        }
        return Response(_json.dumps(data, ensure_ascii=False), mimetype="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})

    @app.route("/sw.js")
    def pwa_service_worker():
        """Service worker має лежати в корені сайту, щоб діяти на весь архів."""
        resp = send_from_directory(os.path.join(BASE_DIR, "static"), "sw.js")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/offline.html")
    def pwa_offline():
        return send_from_directory(os.path.join(BASE_DIR, "static"), "offline.html")

    @app.route("/favicon.ico")
    def favicon():
        return send_from_directory(os.path.join(BASE_DIR, "static", "icons"), "favicon-32.png",
                                   mimetype="image/png")

    @app.route("/i18n.js")
    def i18n_bundle():
        """Переклади ядра й застосунків одним скриптом (див. core/i18n.py)."""
        from flask import Response
        from core.i18n import bundle_js
        js, etag = bundle_js()
        if flask_request.if_none_match.contains(etag):
            return Response(status=304, headers={"ETag": f'"{etag}"'})
        return Response(js, mimetype="application/javascript",
                        headers={"ETag": f'"{etag}"', "Cache-Control": "no-cache"})

    # спільне з'єднання з базою живе рівно стільки, скільки сам запит
    from core.db import close_shared_db
    app.teardown_appcontext(close_shared_db)

    # Журнал модераторів: що з дій ядра вміє відмінятись
    from core import modlog_core, tagops
    modlog_core.register_all()
    tagops.register_undo()

    import importlib
    for name in BLUEPRINT_MODULES:
        mod = importlib.import_module(f"blueprints.{name}")
        app.register_blueprint(mod.bp)

    # Таблиці ядра — до застосунків: на новій (порожній) базі застосунки
    # інакше падали б на таблиці apps, якої ще немає
    try:
        from core.db_schema import ensure_tables
        ensure_tables()
    except Exception as e:
        print(f"  ⚠ Таблиці не перевірено: {e}")

    # Застосунки — після ядра: вони спираються на його таблиці й API
    from core.app_manager import manager
    manager.load_all(app)

    # Важкі зведення (автори, «Огляд») — рахуються на старті у фоні й
    # оновлюються після змін, сторінки отримують готове (core/warmcache.py)
    from core import warmcache
    warmcache.install(app)
    warmcache.start_all()

    return app
