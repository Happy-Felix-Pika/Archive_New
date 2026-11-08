"""Файловий каталог сховища"""

import hashlib
import mimetypes, os

from flask import Blueprint, Response, jsonify, request as flask_request, send_file


from core.auth import admin_required, moderator_required, current_user_id, get_user_nc_config, get_user_settings, is_current_user_admin, merge_user_settings
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

    # Для свого каталогу — id файлу в архіві та «улюблене»: з ними перегляд
    # показує автора й посилання на оригінал, а дії працюють з базою.
    # Файл без id — лежить у сховищі, але в архів не внесений.
    if hidden_folders_override is None and files:
        try:
            from core.files_api import file_ids_by_paths
            db = get_db()
            prefix = f"{rel_path}/" if rel_path else ""
            ids = file_ids_by_paths([prefix + f["name"] for f in files], db)
            favs = set()
            if ids:
                ph = ",".join(["%s"] * len(ids))
                favs = {r["download_id"] for r in qq(
                    db, f"SELECT download_id FROM user_favorites WHERE user_id=%s AND download_id IN ({ph})",
                    [current_user_id()] + list(ids.values()))}
            db.close()
            for f in files:
                fid = ids.get(prefix + f["name"])
                f["id"] = fid
                f["is_favorite"] = fid in favs
        except Exception as e:
            print(f"  ⚠ Каталог: не вдалося підтягнути id файлів: {e}")

    return jsonify({"path": rel_path, "folders": folders, "files": files})


# ── Дії з файлами в каталозі (лише адмін) ─────────────────────────────
#
#  Кожна дія йде через ядро (core/files_api), яке міняє і сховище, і базу:
#  видалення — у кошик зі знімком тегів/автора, переміщення — з новим
#  files.rel_path. Тому після дій у каталозі «Арти», кошик і архіватор
#  бачать файл там, де він справді лежить.

def _catalog_target_folder(raw: str) -> str | None:
    """Тека призначення: без '..', не кошик. None — шлях недопустимий."""
    rel = _safe_rel_path(raw)
    if rel is None:
        return None
    if rel.split("/")[0] == ".trash":
        return None
    return rel


def _catalog_paths(body) -> list | None:
    out = []
    for raw in body.get("paths") or []:
        rel = _safe_rel_path(raw)
        if not rel or rel.split("/")[0] == ".trash":
            return None
        out.append(rel)
    return out


@bp.route("/api/catalog/delete", methods=["POST"])
@moderator_required
def api_catalog_delete():
    """Файли з каталогу — у кошик (з бази теж: тегі, автор, улюблене зберігаються у знімку)."""
    from core.files_api import TrashError, trash_path
    paths = _catalog_paths(flask_request.get_json(silent=True) or {})
    if not paths:
        return jsonify({"error": "Не вказано файли"}), 400
    done, errors, items = [], [], []
    for p in paths:
        try:
            tid = trash_path(p, current_user_id())
            done.append(p)
            items.append({"path": p, "trash_id": tid})
        except TrashError as e:
            errors.append({"path": p, "error": str(e)})
        except Exception as e:
            errors.append({"path": p, "error": str(e)})
    if items:
        from core import modlog
        names = ", ".join(i["path"].rsplit("/", 1)[-1] for i in items[:3]) + ("…" if len(items) > 3 else "")
        modlog.record("file.trash", f"{len(items)} файл(ів): {names}", {"items": items})
    return jsonify({"ok": not errors, "done": done, "errors": errors})


@bp.route("/api/catalog/mkdir", methods=["POST"])
@moderator_required
def api_catalog_mkdir():
    """Нова тека в каталозі (можна одразу вкладену: «Документи/2026»)."""
    from blueprints.upload import _safe_folder
    from core.storage_api import get_storage
    rel = _safe_folder((flask_request.get_json(silent=True) or {}).get("path") or "")
    if not rel:
        return jsonify({"error": "Недопустима назва теки"}), 400
    try:
        st = get_storage()
        if st.exists(rel):
            return jsonify({"error": f"«{rel}» уже існує"}), 409
        if not st.mkdir(rel):
            return jsonify({"error": "Не вдалося створити теку"}), 500
        from core import modlog
        modlog.record("folder.create", rel, {"path": rel})
        return jsonify({"ok": True, "path": rel})
    except StorageError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/catalog/delete-folder", methods=["POST"])
@moderator_required
def api_catalog_delete_folder():
    """
    Видалити теку: усі файли з неї (і з підтек) — у кошик, потім порожня тека.
    dry_run=true — нічого не робити, лише сказати, скільки файлів зачепить
    і чи можна це взагалі (для підтвердження на фронті).
    """
    from core.files_api import TrashError, folder_delete_check, trash_folder
    from core.storage_api import get_storage
    body = flask_request.get_json(silent=True) or {}
    rel = _catalog_target_folder(body.get("path") or "")
    if not rel:
        return jsonify({"error": "Недопустима тека"}), 400
    try:
        if body.get("dry_run"):
            st = get_storage()
            if not st.exists(rel):
                return jsonify({"error": "Теку не знайдено"}), 404
            problems = folder_delete_check(rel)
            if problems:
                return jsonify({"error": " ".join(problems)}), 409
            return jsonify({"ok": True, "files": len(st.walk_files(rel))})
        res = trash_folder(rel, current_user_id())
        if res.get("trash_ids") or res.get("folder_removed"):
            from core import modlog
            modlog.record("folder.trash", f"{rel} ({res['trashed']} файл(ів))",
                          {"folder": rel, "items": [{"path": p, "trash_id": t}
                                                    for p, t in res.get("trash_ids", [])]})
        res.pop("trash_ids", None)
        return jsonify({"ok": not res["errors"], **res})
    except TrashError as e:
        return jsonify({"error": str(e)}), 409
    except StorageError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/catalog/move", methods=["POST"])
@moderator_required
def api_catalog_move():
    """Перемістити файли в іншу теку — і в сховищі, і в базі (files.rel_path)."""
    from core.files_api import MoveError, move_path
    body = flask_request.get_json(silent=True) or {}
    paths = _catalog_paths(body)
    dest = _catalog_target_folder(body.get("dest") or "")
    if not paths:
        return jsonify({"error": "Не вказано файли"}), 400
    if dest is None:
        return jsonify({"error": "Недопустима тека призначення"}), 400
    from core import events, modlog
    from core.files_api import find_file_by_path
    done, errors, items = [], [], []
    for p in paths:
        try:
            # знімок даних застосунків (автор, пул, сторінка) — щоб відміна
            # повернула їх, а не лише сам файл
            snap, fid = None, None
            row = find_file_by_path(p)
            if row:
                fid = row["id"]
                db = get_db()
                snap = events.apply_filters("file.trash", {}, file_id=fid, db=db) or {}
            to = move_path(p, dest)
            done.append({"from": p, "to": to})
            if to != p:
                items.append({"from": p, "to": to, "file_id": fid, "snapshot": snap})
        except MoveError as e:
            errors.append({"path": p, "error": str(e)})
        except Exception as e:
            errors.append({"path": p, "error": str(e)})
    if items:
        modlog.record("file.move", f"{len(items)} файл(ів) → {dest or 'корінь'}", {"items": items})
    return jsonify({"ok": not errors, "done": done, "errors": errors})


# ─────────────────────────────────────────────────────────────────────
#  Мініатюри каталогу
#
#  Раніше плитка показувала прев'ю тільки для картинок, і то підставляла
#  в <img> ПОВНОРОЗМІРНИЙ файл — тека з двома сотнями PNG тягнула сотні
#  мегабайт заради сітки 150×150. Відео не мало прев'ю взагалі, лише
#  іконку 🎬.
#
#  Тут обидва випадки зводяться до одного: маленький JPEG, який робиться
#  один раз і лягає в той самий дисковий кеш, що й решта зображень.
#  Для відео кадр дістає ffmpeg (він уже є в образі), для картинок —
#  Pillow. Якщо щось не вийшло, віддаємо 404, а фронт малює іконку.
# ─────────────────────────────────────────────────────────────────────

THUMB_SIZE       = 320      # по довшій стороні, з запасом під ретіну
THUMB_VIDEO_SEEK = 3        # секунда кадру: нульова часто чорна
THUMB_TIMEOUT    = 25

_THUMB_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "avif", "jfif", "tif", "tiff"}
_THUMB_VIDEO_EXT = {"webm", "mp4", "mov", "mkv", "avi", "m4v", "ogv",
                    "flv", "wmv", "mpg", "mpeg", "ts", "m2ts", "mts", "3gp", "asf", "divx"}


def _thumb_cache_path(rel_path: str, tag: str = "") -> str:
    key = hashlib.md5(f"thumb:{THUMB_SIZE}:{rel_path}:{tag}".encode()).hexdigest()
    return os.path.join(_IMG_CACHE_DIR, f"t_{key}.jpg")


def _make_image_thumb(src_path: str, out_path: str) -> bool:
    try:
        from PIL import Image
        with Image.open(src_path) as im:
            im.seek(0) if getattr(im, "is_animated", False) else None
            im = im.convert("RGB")
            im.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
            im.save(out_path, "JPEG", quality=82, optimize=True)
        return True
    except Exception as e:
        print(f"  ! мініатюра зображення не вийшла ({src_path}): {e}")
        return False


def _make_video_thumb(src: str, out_path: str, headers: str | None = None) -> bool:
    """
    Кадр з відео через ffmpeg. src — шлях на диску або http(s)-URL.

    Пробуємо по черзі три способи, від найшвидшого до найнадійнішого:

      1. -ss ПЕРЕД -i  — швидке позиціонування: ffmpeg стрибає одразу
         в потрібне місце, не декодуючи початок. Працює миттєво, але
         для частини контейнерів (avi, flv, mpeg-ps, пошкоджені mp4
         без індексу) просто нічого не віддає.
      2. -ss ПІСЛЯ -i  — точне позиціонування: декодує від початку й
         відкидає кадри. Повільніше, зате бере там, де спосіб 1 мовчки
         провалювався. Саме через брак цього кроку частина відео
         лишалась без прев'ю.
      3. без позиціонування — найперший кадр. Остання спроба: коротке
         відео, одна картинка в контейнері тощо.

    Перший кадр у багатьох роликів чорний, тому способи 1 і 2 беруть
    кадр на THUMB_VIDEO_SEEK секунді, і лише спосіб 3 — з нуля.
    """
    import subprocess

    attempts = [
        ("швидке позиціонування", ["-ss", str(THUMB_VIDEO_SEEK)], []),
        ("точне позиціонування",  [], ["-ss", str(THUMB_VIDEO_SEEK)]),
        ("перший кадр",           [], []),
    ]
    last_err = ""
    for label, pre, post in attempts:
        cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error"]
        if headers:
            cmd += ["-headers", headers]
        cmd += pre + ["-i", src] + post + [
            "-frames:v", "1", "-an", "-sn", "-dn",
            "-vf", f"scale='min({THUMB_SIZE},iw)':-1",
            "-q:v", "5", out_path]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=THUMB_TIMEOUT)
        except subprocess.TimeoutExpired:
            print(f"  ! мініатюра відео: ffmpeg завис ({label}) на {src}")
            continue
        except FileNotFoundError:
            print("  ! ffmpeg не знайдено — мініатюр для відео не буде")
            return False
        if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return True
        last_err = (r.stderr or b"").decode("utf-8", "replace").strip()[-300:]

    # Жоден спосіб не спрацював — пишемо причину в лог, інакше
    # «у частини відео немає прев'ю» неможливо діагностувати.
    print(f"  ! мініатюра відео не вийшла: {src}\n    ffmpeg: {last_err or 'без повідомлення'}")
    return False


@bp.route("/api/catalog/thumb")
def api_catalog_thumb(external: bool = False):
    """Мініатюра файлу каталогу: зображення або кадр з відео."""
    rel_path = _safe_rel_path(flask_request.args.get("path"))
    if not rel_path:
        return jsonify({"error": "bad path"}), 400

    # external=True — запит прийшов через ключ доступу, а не від залогіненого
    # юзера. Папки для такого запиту вже перевірив виклик у federation.py, за
    # списком САМЕ ЦЬОГО ключа. Перевіряти тут ще й сесію не можна: сесії
    # немає, current_user_id() у такому разі повертає 1, і зовнішній архів
    # мовчки впирався в папки, які власник сховав від себе — 403 на файл,
    # до якого ключ має повне право.
    if not external and not is_current_user_admin():
        top_segment = rel_path.split("/", 1)[0]
        if top_segment in get_hidden_catalog_folders(current_user_id()):
            return jsonify({"error": f"Папка «{top_segment}» прихована від цього користувача",
                            "origin": "сесія"}), 403
    # Далі — лише файли й ffmpeg (до хвилини на відео). Базу віддаємо одразу,
    # інакше сотні мініатюр одночасно вичерпують зʼєднання MariaDB.
    from core.db import release_db
    release_db()

    filename = rel_path.rsplit("/", 1)[-1]
    ext      = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    is_image = ext in _THUMB_IMAGE_EXT
    is_video = ext in _THUMB_VIDEO_EXT

    # ?debug=1 — чому саме для цього файлу немає прев'ю. Інакше
    # «у частини відео його немає» доводиться ловити по логах.
    debug = flask_request.args.get("debug") == "1"
    if debug:
        info = {"path": rel_path, "ext": ext, "kind":
                "зображення" if is_image else "відео" if is_video else "не підтримується",
                "storage": get_storage_mode()}
        if is_video and get_storage_mode() == "local":
            src = os.path.join(get_local_root(), rel_path)
            info["файл на диску"] = os.path.isfile(src)
            if os.path.isfile(src):
                info["розмір"] = os.path.getsize(src)
                import subprocess
                try:
                    pr = subprocess.run(
                        ["ffprobe", "-v", "error", "-show_entries",
                         "stream=codec_type,codec_name,width,height:format=duration",
                         "-of", "default=nw=1", src],
                        capture_output=True, timeout=20)
                    info["ffprobe"] = (pr.stdout or b"").decode("utf-8", "replace").split("\n")
                    err = (pr.stderr or b"").decode("utf-8", "replace").strip()
                    if err:
                        info["ffprobe_помилка"] = err[-300:]
                except Exception as e:
                    info["ffprobe"] = f"не вдалось запустити: {e}"
        return jsonify(info)

    if not (is_image or is_video):
        return jsonify({"error": "no preview"}), 404

    mode = get_storage_mode()

    if mode == "local":
        src = os.path.join(get_local_root(), rel_path)
        if not os.path.isfile(src):
            return jsonify({"error": "not found"}), 404
        # mtime у ключі: файл підмінили — мініатюра перероблюється сама
        try:
            tag = str(int(os.path.getmtime(src)))
        except OSError:
            tag = ""
        out = _thumb_cache_path(rel_path, tag)
        if not os.path.exists(out):
            ok = _make_image_thumb(src, out) if is_image else _make_video_thumb(src, out)
            if not ok:
                return jsonify({"error": "thumb failed"}), 404
        return send_file(out, mimetype="image/jpeg", conditional=True,
                         max_age=86400)

    # ── Nextcloud ────────────────────────────────────────────────────
    nc_cfg     = get_user_nc_config()
    webdav_url = nc_cfg.get("webdav_url", "").rstrip("/")
    base       = nc_cfg.get("base_folder", "").strip("/")
    full_path  = f"{base}/{rel_path}".strip("/")
    file_url   = f"{webdav_url}/{full_path}"
    user, pwd  = nc_cfg.get("username", ""), nc_cfg.get("password", "")

    out = _thumb_cache_path(full_path)
    if os.path.exists(out):
        return send_file(out, mimetype="image/jpeg", conditional=True, max_age=86400)

    if is_video:
        # ffmpeg читає WebDAV по HTTP сам і забирає лише потрібні шматки —
        # качати ролик цілком заради одного кадру не треба.
        import base64 as _b64
        auth = _b64.b64encode(f"{user}:{pwd}".encode()).decode()
        ok = _make_video_thumb(file_url, out, headers=f"Authorization: Basic {auth}\r\n")
        if not ok:
            return jsonify({"error": "thumb failed"}), 404
        return send_file(out, mimetype="image/jpeg", conditional=True, max_age=86400)

    import tempfile
    import requests as req
    tmp = None
    try:
        r = req.get(file_url, auth=(user, pwd), stream=True, timeout=30)
        r.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as fh:
            tmp = fh.name
            for chunk in r.iter_content(65536):
                if chunk:
                    fh.write(chunk)
        if not _make_image_thumb(tmp, out):
            return jsonify({"error": "thumb failed"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 404
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return send_file(out, mimetype="image/jpeg", conditional=True, max_age=86400)


@bp.route("/api/catalog/file")
def api_catalog_file(external: bool = False):
    """Віддає конкретний файл за відносним шляхом у сховищі — для перегляду."""
    rel_path = _safe_rel_path(flask_request.args.get("path"))
    if not rel_path:
        return jsonify({"error": "bad path"}), 400

    # external=True — запит прийшов через ключ доступу, а не від залогіненого
    # юзера. Папки для такого запиту вже перевірив виклик у federation.py, за
    # списком САМЕ ЦЬОГО ключа. Перевіряти тут ще й сесію не можна: сесії
    # немає, current_user_id() у такому разі повертає 1, і зовнішній архів
    # мовчки впирався в папки, які власник сховав від себе — 403 на файл,
    # до якого ключ має повне право.
    if not external and not is_current_user_admin():
        top_segment = rel_path.split("/", 1)[0]
        if top_segment in get_hidden_catalog_folders(current_user_id()):
            return jsonify({"error": f"Папка «{top_segment}» прихована від цього користувача",
                            "origin": "сесія"}), 403

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
