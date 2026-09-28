"""nextcloud"""
from core.auth import get_user_settings
from core.db import q, qq


import threading

import pymysql, pymysql.cursors


# ── Nextcloud DB (read-only) ──────────────────────────────────────────────

def get_nc_db(user_id: int | None = None):
    """Підключення до БД Nextcloud — ТІЛЬКИ ЧИТАННЯ.
    Конфіг nextcloud_db читається зі спільного archiver_config.json."""
    cfg = get_user_settings().get("nextcloud_db", {})
    if not cfg.get("host") or not cfg.get("user") or not cfg.get("database"):
        raise ValueError("Nextcloud DB не налаштована (Конфіг → Nextcloud DB)")
    conn = pymysql.connect(
        host=cfg.get("host", "localhost"),
        port=int(cfg.get("port", 3306)),
        user=cfg.get("user", ""),
        password=cfg.get("password", ""),
        database=cfg.get("database", ""),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        # read_only session — гарантує що жодний запит не може змінити дані
        init_command="SET SESSION TRANSACTION READ ONLY",
    )
    return conn


# ── Nextcloud Share URL ───────────────────────────────────────────────────

_nc_artist_paths: dict = {}
_nc_artist_paths_lock = threading.Lock()
_nc_artist_paths_scanned = False


def _scan_artist_paths():
    """Будує мапу @artist → відносний шлях через БД Nextcloud (реальний share root з БД).
    share_token завжди адмінський (user_id=1) — це спільне публічне посилання."""
    global _nc_artist_paths_scanned
    try:
        admin_nc   = get_user_settings(1).get("nextcloud", {})
        token      = admin_nc.get("share_token", "")
        share_root = _get_share_root_path(token) if token else None

        nc_conn = get_nc_db(1)
        rows = qq(nc_conn, """
            SELECT name, path FROM oc_filecache
            WHERE name LIKE %s
              AND mimetype = (SELECT id FROM oc_mimetypes WHERE mimetype='httpd/unix-directory')
        """, ('@%',))
        nc_conn.close()

        result = {}
        for r in rows:
            name = r['name']
            path = r['path']  # напр. "files/Furry/Artists/1/@lizardlars"
            rel  = path.removeprefix("files/").removeprefix("files")

            if share_root is not None:
                if rel == share_root:
                    artist_rel = "/"
                elif rel.startswith(share_root + "/"):
                    artist_rel = "/" + rel[len(share_root) + 1:]
                else:
                    artist_rel = f"/{name}"  # файл не під цим share — fallback
            else:
                artist_rel = f"/{name}"

            # Якщо для цього автора вже є шлях — лишаємо найглибший (найдовший)
            existing = result.get(name)
            if existing is None or len(artist_rel) > len(existing):
                result[name] = artist_rel

        with _nc_artist_paths_lock:
            _nc_artist_paths.update(result)
            _nc_artist_paths_scanned = True
        print(f"  ✓ NC artist paths: знайдено {len(result)} авторів")
    except Exception as e:
        print(f"  ⚠ NC artist paths: {e}")
        with _nc_artist_paths_lock:
            _nc_artist_paths_scanned = True


def get_artist_nc_rel_path(folder_name: str) -> str:
    """Повертає відносний шлях до папки автора від кореня share."""
    with _nc_artist_paths_lock:
        path = _nc_artist_paths.get(folder_name)
    return path or f"/{folder_name}"


_share_root_path_cache: dict = {}
_share_root_path_lock = threading.Lock()


def _get_share_root_path(token: str) -> str | None:
    """
    Знаходить РЕАЛЬНИЙ шлях папки, на яку зроблено публічне посилання (share_token),
    напряму з БД Nextcloud — без жодних здогадок чи конфігів.
    Повертає шлях у форматі "Furry/Artists" (без 'files/' на початку).
    """
    with _share_root_path_lock:
        if token in _share_root_path_cache:
            return _share_root_path_cache[token]
    try:
        nc_conn = get_nc_db(1)  # share_token завжди адмінський
        # oc_share.token → file_source (fileid) → oc_filecache.path
        row = q(nc_conn, """
            SELECT fc.path
            FROM oc_share s
            JOIN oc_filecache fc ON fc.fileid = s.file_source
            WHERE s.token = %s
            LIMIT 1
        """, (token,))
        nc_conn.close()
        if not row:
            return None
        path = row["path"]  # напр. "files/Furry/Artists"
        root = path.removeprefix("files/").removeprefix("files")
        root = root.strip("/")
        with _share_root_path_lock:
            _share_root_path_cache[token] = root
        return root
    except Exception as e:
        print(f"  ⚠ _get_share_root_path: {e}")
        return None


def get_file_nc_real_path(folder_name: str, filename: str) -> tuple[str, str] | None:
    """
    Знаходить РЕАЛЬНИЙ шлях файлу напряму з NC БД (oc_filecache) — без здогадок
    про base_folder з конфігу. Повертає (full_dir_path, filename) де full_dir_path
    включає все від кореня сховища, напр. "Furry/Artists/1/@author".

    На диску можуть існувати дублікати структури папок (наприклад через
    стару/нову схему base_folder), тому беремо ВСІ збіги і обираємо
    найдовший шлях (найглибший рівень вкладеності) як найбільш актуальний —
    і явно виключаємо службову папку files_versions.
    """
    try:
        nc_conn = get_nc_db(1)  # NC DB — спільна, адмінська конфігурація
        rows = qq(nc_conn, """
            SELECT fc.path
            FROM oc_filecache fc
            WHERE fc.name = %s
              AND fc.path LIKE %s
              AND fc.path NOT LIKE 'files\\_versions/%%'
        """, (filename, f'%%/{folder_name}/%%'))
        nc_conn.close()
        if not rows:
            return None

        # Якщо декілька збігів — беремо найдовший шлях (найглибша/найточніша копія)
        best_path = max(rows, key=lambda r: len(r["path"]))["path"]

        rel = best_path.removeprefix("files/").removeprefix("files")
        parts = rel.rsplit("/", 1)
        if len(parts) != 2:
            return None
        return (parts[0], parts[1])  # ("Furry/Artists/1/@author", "file.jpg")
    except Exception:
        return None


def nc_share_url(folder_name: str, filename: str, base_folder: str | None = None) -> str | None:
    """
    Будує прямий URL до файлу через Nextcloud Share Link.
    Шлях файлу і шлях share-кореня беруться НАПРЯМУ з БД Nextcloud (oc_filecache/oc_share) —
    жодних здогадок з конфігу. share_token завжди адмінський (user_id=1) — це спільне
    публічне посилання. base_folder параметр більше не використовується для обчислень,
    залишений для зворотної сумісності викликів.
    """
    admin_nc = get_user_settings(1).get("nextcloud", {})
    token    = admin_nc.get("share_token", "")
    if not token:
        return None
    from urllib.parse import quote
    nc_base = admin_nc.get("webdav_url", "").split("/remote.php")[0]

    file_info  = get_file_nc_real_path(folder_name, filename)
    share_root = _get_share_root_path(token)

    if file_info and share_root is not None:
        full_dir, fname = file_info
        # Відрізаємо share_root від повного шляху файлу
        if full_dir == share_root:
            dir_path = "/"
        elif full_dir.startswith(share_root + "/"):
            dir_path = "/" + full_dir[len(share_root) + 1:]
        else:
            # Файл не всередині цього share — пробуємо все одно як fallback
            dir_path = "/" + full_dir
        return f"{nc_base}/s/{token}/download?path={quote(dir_path, safe='')}&files={quote(fname)}"

    # Fallback — старий метод через кешовані шляхи авторів (на випадок збою БД)
    rel_path = get_artist_nc_rel_path(folder_name)
    return f"{nc_base}/s/{token}/download?path={quote(rel_path, safe='')}&files={quote(filename)}"
