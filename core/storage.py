"""storage"""
from core.auth import get_user_settings


import os



def get_storage_mode() -> str | None:
    """'nextcloud' | 'local' | None — залежно від того, що налаштовано і увімкнено."""
    settings = get_user_settings()
    nc = settings.get("nextcloud", {})
    nc_enabled = nc.get("enabled", bool(nc.get("webdav_url")))
    if nc_enabled and nc.get("webdav_url"):
        return "nextcloud"
    if settings.get("local", {}).get("path"):
        return "local"
    return None


def get_local_root() -> str:
    return get_user_settings().get("local", {}).get("path", "")


def local_file_path(folder_name: str, filename: str, subfolder: str | None = None) -> str:
    """
    Будує абсолютний шлях до файлу в локальному сховищі.
    '_root' — спеціальна позначка папки для файлів, синхронізованих прямо
    з кореня сховища (без окремої підпапки автора) — див. sync_all_files
    у furry_archiver.py.
    subfolder — додаткова вкладена папка всередині папки автора
    (наприклад назва e621 pool): @автор/НазваPool/файл.
    """
    root = get_local_root()
    base = filename if not subfolder else os.path.join(subfolder, filename)
    if folder_name == "_root":
        return os.path.join(root, base)
    return os.path.join(root, folder_name, base)
