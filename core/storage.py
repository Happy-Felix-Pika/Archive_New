"""storage"""
from core.auth import get_user_settings


import os



def get_storage_mode() -> str | None:
    """'local' | None. Ядро зберігає файли лише в локальній теці; Nextcloud —
    окремий застосунок і сховищем ядра не є."""
    if get_user_settings().get("local", {}).get("path"):
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
