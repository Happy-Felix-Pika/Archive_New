"""Файловий каталог сховища"""

import hashlib
import mimetypes, os

from flask import Blueprint, Response, jsonify, request as flask_request, send_file


from core.auth import admin_required, current_user_id, get_user_nc_config, get_user_settings, is_current_user_admin, merge_user_settings
from core.db import get_db, qq
from core.imgcache import _IMG_CACHE_DIR
from core.storage_api import StorageError, get_storage
from core.storage import get_local_root, get_storage_mode

bp = Blueprint("catalog", __name__)


def get_hidden_catalog_folders(user_id: int) -> set:
    """Папки верхнього рівня, які адмін сховав від цього конкретного юзера."""
    db   = get_db()
    rows = qq(db, "SELECT folder_name FROM catalog_folder_hidden WHERE user_id=%s", (user_id,))
    db.close()
    return {r["folder_name"] for r in rows}


@bp.route("/api/catalog/folder-access")
@admin_required
def api_catalog_folder_access_list():
    """Від кого зараз схована ця папка — тільки адмін бачить/керує цим."""
    folder = flask_request.args.get("folder", "")
    if not folder:
        return jsonify({"error": "folder required"}), 400
    db     = get_db()
    hidden = qq(db, "SELECT user_id FROM catalog_folder_hidden WHERE folder_name=%s", (folder,))
    db.close()
    return jsonify({"hidden_user_ids": [r["user_id"] for r in hidden]})


@bp.route("/api/catalog/folder-access", methods=["POST"])
@admin_required
def api_catalog_folder_access_toggle():
    """
    Ховає/показує конкретну кореневу папку для конкретного юзера. За
    замовчуванням усі папки видно всім — цей запис з'являється лише коли
    адмін явно щось приховав.
    """
    body    = flask_request.get_json(silent=True) or {}
    folder  = (body.get("folder") or "").strip()
    user_id = body.get("user_id")
    hide    = bool(body.get("hide"))
    if not folder or not user_id:
        return jsonify({"error": "folder і user_id обов'язкові"}), 400
    db = get_db()
    with db.cursor() as cur:
        if hide:
            cur.execute("INSERT IGNORE INTO catalog_folder_hidden (folder_name, user_id) VALUES (%s,%s)",
                        (folder, user_id))
        else:
            cur.execute("DELETE FROM catalog_folder_hidden WHERE folder_name=%s AND user_id=%s", (folder, user_id))
    db.commit(); db.close()
    return jsonify({"ok": True})


# ── Каталог: браузер файлової системи сховища (доступно всім користувачам) ─

def _safe_rel_path(raw: str) -> str | None:
    """Прибирає зайві слеші і не дає вийти за межі кореня через '..'."""
    rel = (raw or "").strip("/")
    if not rel:
        return ""
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    return "/".join(parts)


@bp.route("/api/catalog/excluded-folders")
def api_catalog_excluded_folders():
    """Список папок, які юзер позначив 'не сканувати в Арти'."""
    excluded = get_user_settings().get("scan_exclude", {}).get("folders", []) or []
    return jsonify(excluded)


@bp.route("/api/catalog/toggle-exclude", methods=["POST"])
@admin_required
def api_catalog_toggle_exclude():
    """
    Додає/прибирає папку зі списку виключень зі сканування. Каталог і
    надалі показує цю папку як звичайну (для перегляду) — виключення
    впливає ТІЛЬКИ на синхронізацію/Арти, щоб особисті файли, що лежать
    у тій самій кореневій папці, ніколи не потрапляли в архів.
    """
    body       = flask_request.get_json(silent=True) or {}
    folder     = (body.get("folder") or "").strip()
    exclude    = bool(body.get("exclude"))
    if not folder:
        return jsonify({"error": "Не вказано папку"}), 400
    current = get_user_settings().get("scan_exclude", {}).get("folders", []) or []
    current = [f for f in current if f != folder]
    if exclude:
        current.append(folder)
    merge_user_settings({"scan_exclude": {"folders": current}})
    return jsonify({"ok": True, "excluded": current})


@bp.route("/api/catalog/list")
def api_catalog_list(hidden_folders_override=None):
    """Список папок і файлів у каталозі сховища за відносним шляхом.
    hidden_folders_override — для зовнішнього доступу через ключ, де нема
    сесії локального юзера, тож приховані папки визначаються самим ключем,
    а не get_hidden_catalog_folders(current_user_id())."""
    rel_path = _safe_rel_path(flask_request.args.get("path"))
    if rel_path is None:
        return jsonify({"error": "bad path"}), 400

    if hidden_folders_override is not None:
        hidden_folders = hidden_folders_override
        top_segment = rel_path.split("/", 1)[0] if rel_path else None
        if top_segment and top_segment in hidden_folders:
            return jsonify({"error": "Немає доступу до цієї папки"}), 403
    else:
        # Приховані папки діють лише на кореневому рівні, і тільки для НЕ
        # адміна — сам адмін завжди бачить усе, він же й керує приховуванням
        hidden_folders = set()
        if not is_current_user_admin():
            hidden_folders = get_hidden_catalog_folders(current_user_id())
            # Блокуємо і пряму навігацію всередину прихованої папки (не лише
            # приховуємо зі списку) — інакше можна було б просто підставити
            # шлях руками і все одно потрапити туди
            top_segment = rel_path.split("/", 1)[0] if rel_path else None
            if top_segment and top_segment in hidden_folders:
                return jsonify({"error": "Немає доступу до цієї папки"}), 403

    # Одне сховище на обидва режими: розбір відповіді WebDAV і обхід
    # локальної теки живуть тепер у core/storage_api.py, а тут лишається
    # тільки те, що справді стосується каталогу — приховані теки,
    # службовий download.txt і сортування.
    if rel_path.strip("/").split("/")[0] == ".trash":
        return jsonify({"error": "Кошик переглядається в розділі «Кошик»"}), 403

    try:
        entries = get_storage().list_dir(rel_path)
    except StorageError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not entries and rel_path and not get_storage().exists(rel_path):
        return jsonify({"error": "Папку не знайдено"}), 404

    folders, files = [], []
    for it in entries:
        name = it["name"]
        if it["is_dir"]:
            # .trash — кошик ядра; у каталозі йому не місце, для нього є
            # окремий розділ «Кошик»
            if name in hidden_folders or name == ".trash":
                continue
            folders.append({"name": name})
        elif name != "download.txt":
            files.append({
                "name":  name,
                "ext":   name.rsplit(".", 1)[-1].lower() if "." in name else "",
                "size":  it.get("size"),
                "mtime": it.get("mtime"),
            })

    folders.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())
    return jsonify({"path": rel_path, "folders": folders, "files": files})


@bp.route("/api/catalog/file")
def api_catalog_file():
    """Віддає конкретний файл за відносним шляхом у сховищі — для перегляду."""
    rel_path = _safe_rel_path(flask_request.args.get("path"))
    if not rel_path:
        return jsonify({"error": "bad path"}), 400

    if not is_current_user_admin():
        top_segment = rel_path.split("/", 1)[0]
        if top_segment in get_hidden_catalog_folders(current_user_id()):
            return jsonify({"error": "Немає доступу до цієї папки"}), 403

    filename = rel_path.rsplit("/", 1)[-1]
    mime     = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    mode     = get_storage_mode()

    if mode == "local":
        fpath = os.path.join(get_local_root(), rel_path)
        if not os.path.isfile(fpath):
            return jsonify({"error": "not found"}), 404
        return send_file(fpath, mimetype=mime, conditional=True)

    elif mode == "nextcloud":
        nc_cfg     = get_user_nc_config()
        webdav_url = nc_cfg.get("webdav_url", "").rstrip("/")
        base       = nc_cfg.get("base_folder", "").strip("/")
        full_path  = f"{base}/{rel_path}".strip("/")
        file_url   = f"{webdav_url}/{full_path}"
        is_video   = mime.startswith("video/")

        import requests as req

        if not is_video:
            cache_key  = hashlib.md5(f"catalog:{full_path}".encode()).hexdigest()
            cache_path = os.path.join(_IMG_CACHE_DIR, cache_key)
            if not os.path.exists(cache_path):
                r = req.get(file_url, auth=(nc_cfg.get("username", ""), nc_cfg.get("password", "")),
                            stream=True, timeout=30)
                r.raise_for_status()
                with open(cache_path, "wb") as f:
                    for chunk in r.iter_content(65536):
                        if chunk: f.write(chunk)
            return send_file(cache_path, mimetype=mime, conditional=True)

        # відео — стрім напряму, без кешу
        range_header = flask_request.headers.get("Range")
        headers = {"Range": range_header} if range_header else {}
        nc_resp = req.get(file_url, auth=(nc_cfg.get("username", ""), nc_cfg.get("password", "")),
                          headers=headers, stream=True, timeout=30)

        def gen():
            for chunk in nc_resp.iter_content(chunk_size=131072):
                if chunk: yield chunk

        resp_headers = {"Content-Type": mime, "Accept-Ranges": "bytes", "Cache-Control": "no-store"}
        if "Content-Length" in nc_resp.headers: resp_headers["Content-Length"] = nc_resp.headers["Content-Length"]
        if "Content-Range" in nc_resp.headers:  resp_headers["Content-Range"]  = nc_resp.headers["Content-Range"]
        return Response(gen(), status=nc_resp.status_code, headers=resp_headers)

    return jsonify({"error": "Сховище не налаштоване"}), 400
