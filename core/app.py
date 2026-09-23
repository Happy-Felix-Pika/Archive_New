"""Створення Flask-застосунку, глобальний захист API, реєстрація blueprint'ів."""

import logging
import os

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
}

# Порядок не має значення — кожен blueprint самодостатній.
# ЯДРО: універсальний файловий архів. Нічого про авторів, зовнішні
# джерела чи дублікати тут немає — усе це застосунки в apps/installed/.
BLUEPRINT_MODULES = [
    "apps", "auth_api", "catalog", "federation", "files", "gallery",
    "jobs", "settings_api", "stats", "tags", "trash", "upload", "users",
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

    import importlib
    for name in BLUEPRINT_MODULES:
        mod = importlib.import_module(f"blueprints.{name}")
        app.register_blueprint(mod.bp)

    # Застосунки — після ядра: вони спираються на його таблиці й API
    from core.app_manager import manager
    manager.load_all(app)

    return app
