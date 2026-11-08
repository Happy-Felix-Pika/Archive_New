"""Розділ «Застосунки»: список, вмикання/вимикання, статика застосунків."""

import os

from flask import Blueprint, jsonify, request as flask_request, send_from_directory

from core.app_manager import APPS_DIR, manager
from core.auth import admin_required, is_current_user_admin, login_required

bp = Blueprint("apps", __name__)


@bp.route("/api/apps")
@login_required
def api_apps_list():
    return jsonify(manager.listing())


@bp.route("/api/apps/<app_id>/enable", methods=["POST"])
@admin_required
def api_app_enable(app_id):
    ok, msg = manager.set_enabled(app_id, True)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@bp.route("/api/apps/<app_id>/disable", methods=["POST"])
@admin_required
def api_app_disable(app_id):
    ok, msg = manager.set_enabled(app_id, False)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@bp.route("/api/apps/ui")
@login_required
def api_apps_ui():
    """Меню й сторінки для фронтенду — інтерфейс будується з цього."""
    lang = (flask_request.args.get("lang") or "uk")[:5]
    from core.auth import current_role
    return jsonify(manager.ui(lang=lang, is_admin=is_current_user_admin(), role=current_role()))


@bp.route("/apps/<app_id>/<path:filename>")
def app_static(app_id, filename):
    """
    Статика застосунку (js/css/іконки).

    send_from_directory сам не випускає за межі вказаної теки, тож
    вийти з теки застосунку через '..' у filename не вийде.
    """
    if app_id not in manager.apps:
        return jsonify({"error": "Немає такого застосунку"}), 404
    return send_from_directory(os.path.join(APPS_DIR, app_id), filename)
