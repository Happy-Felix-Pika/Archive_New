"""Кошик: перегляд, відновлення, остаточне видалення."""

from flask import Blueprint, jsonify

from core.auth import admin_required
from core.files_api import TrashError, empty_trash, list_trash, purge_trash, restore_trash

bp = Blueprint("trash", __name__)


@bp.route("/api/trash")
@admin_required
def api_trash_list():
    return jsonify(list_trash())


@bp.route("/api/trash/<int:trash_id>/restore", methods=["POST"])
@admin_required
def api_trash_restore(trash_id):
    try:
        file_id = restore_trash(trash_id)
        return jsonify({"ok": True, "file_id": file_id})
    except TrashError as e:
        return jsonify({"ok": False, "error": str(e)}), 409


@bp.route("/api/trash/<int:trash_id>", methods=["DELETE"])
@admin_required
def api_trash_purge(trash_id):
    try:
        purge_trash(trash_id)
        return jsonify({"ok": True})
    except TrashError as e:
        return jsonify({"ok": False, "error": str(e)}), 404


@bp.route("/api/trash/empty", methods=["POST"])
@admin_required
def api_trash_empty():
    return jsonify({"ok": True, "removed": empty_trash()})
