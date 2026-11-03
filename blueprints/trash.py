"""Кошик: перегляд (з прев'ю), відновлення, остаточне видалення."""

import mimetypes
import os
import tempfile

from flask import Blueprint, jsonify, request, send_file

from core.auth import admin_required
from core.files_api import (TrashError, empty_trash, list_trash, list_trash_orphans,
                            purge_trash, purge_trash_orphan, restore_trash,
                            restore_trash_orphan, trash_item, TRASH_DIR, local_path)
from core.storage import get_storage_mode

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
        return jsonify({"ok": False, "error": str(e)}), 409


@bp.route("/api/trash/empty", methods=["POST"])
@admin_required
def api_trash_empty():
    removed, failed = empty_trash()
    return jsonify({"ok": True, "removed": removed, "failed": failed})


# ── Прев'ю й перегляд файлу з кошика ─────────────────────────────────
#
#  Окремо від /api/catalog/thumb: той доступний усім залогіненим, а кошик —
#  лише адміну. Мініатюри роблять ті самі функції, що й у каталозі, і
#  лягають у той самий дисковий кеш.

def _send_preview(rel_path: str, name: str, full: bool):
    from blueprints.catalog import (_THUMB_IMAGE_EXT, _THUMB_VIDEO_EXT, _make_image_thumb,
                                    _make_video_thumb, _thumb_cache_path)
    from core.storage_api import get_storage

    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    is_image, is_video = ext in _THUMB_IMAGE_EXT, ext in _THUMB_VIDEO_EXT

    if get_storage_mode() == "local":
        src = local_path(rel_path)
        if not os.path.isfile(src):
            return jsonify({"error": "not found"}), 404
        if full:
            return send_file(src, mimetype=mimetypes.guess_type(name)[0] or "application/octet-stream",
                             conditional=True, download_name=name)
        if not (is_image or is_video):
            return jsonify({"error": "no preview"}), 404
        out = _thumb_cache_path(rel_path, str(int(os.path.getmtime(src))))
        if not os.path.exists(out):
            ok = _make_image_thumb(src, out) if is_image else _make_video_thumb(src, out)
            if not ok:
                return jsonify({"error": "thumb failed"}), 404
        return send_file(out, mimetype="image/jpeg", conditional=True, max_age=86400)

    # Nextcloud: качаємо у тимчасовий файл і робимо так само
    if not full and not (is_image or is_video):
        return jsonify({"error": "no preview"}), 404
    out = _thumb_cache_path(f"nc:{rel_path}")
    if not full and os.path.exists(out):
        return send_file(out, mimetype="image/jpeg", conditional=True, max_age=86400)
    fd, tmp = tempfile.mkstemp(suffix=f".{ext}" if ext else "")
    os.close(fd)
    try:
        if not get_storage().download(rel_path, tmp):
            return jsonify({"error": "not found"}), 404
        if full:
            with open(tmp, "rb") as fh:
                data = fh.read()
            from flask import Response
            return Response(data, mimetype=mimetypes.guess_type(name)[0] or "application/octet-stream")
        ok = _make_image_thumb(tmp, out) if is_image else _make_video_thumb(tmp, out)
        if not ok:
            return jsonify({"error": "thumb failed"}), 404
        return send_file(out, mimetype="image/jpeg", conditional=True, max_age=86400)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


@bp.route("/api/trash/<int:trash_id>/thumb")
@admin_required
def api_trash_thumb(trash_id):
    item = trash_item(trash_id)
    if not item or not item["trash_rel_path"]:
        return jsonify({"error": "not found"}), 404
    return _send_preview(item["trash_rel_path"], item["name"] or "", full=False)


@bp.route("/api/trash/<int:trash_id>/file")
@admin_required
def api_trash_file(trash_id):
    item = trash_item(trash_id)
    if not item or not item["trash_rel_path"]:
        return jsonify({"error": "not found"}), 404
    return _send_preview(item["trash_rel_path"], item["name"] or "", full=True)


# ── «Сироти»: файли в .trash без запису ──────────────────────────────

def _orphan_name():
    name = request.args.get("name", "")
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    return name


@bp.route("/api/trash/orphans")
@admin_required
def api_trash_orphans():
    return jsonify(list_trash_orphans())


@bp.route("/api/trash/orphans/thumb")
@admin_required
def api_trash_orphan_thumb():
    name = _orphan_name()
    if not name:
        return jsonify({"error": "bad name"}), 400
    real = name.split("__", 1)[1] if "__" in name else name
    return _send_preview(f"{TRASH_DIR}/{name}", real, full=False)


@bp.route("/api/trash/orphans/file")
@admin_required
def api_trash_orphan_file():
    name = _orphan_name()
    if not name:
        return jsonify({"error": "bad name"}), 400
    real = name.split("__", 1)[1] if "__" in name else name
    return _send_preview(f"{TRASH_DIR}/{name}", real, full=True)


@bp.route("/api/trash/orphans/restore", methods=["POST"])
@admin_required
def api_trash_orphan_restore():
    name = _orphan_name()
    if not name:
        return jsonify({"ok": False, "error": "bad name"}), 400
    try:
        return jsonify({"ok": True, "file_id": restore_trash_orphan(name)})
    except TrashError as e:
        return jsonify({"ok": False, "error": str(e)}), 409


@bp.route("/api/trash/orphans", methods=["DELETE"])
@admin_required
def api_trash_orphan_purge():
    name = _orphan_name()
    if not name:
        return jsonify({"ok": False, "error": "bad name"}), 400
    try:
        purge_trash_orphan(name)
        return jsonify({"ok": True})
    except TrashError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
