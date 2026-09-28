"""config"""
from core.paths import CONFIG_FILE


import json, os



# ── Config ────────────────────────────────────────────────────────────────

# Файл налаштувань читався з диска на КОЖНЕ підключення до бази, а він
# лежить на змонтованому томі. Тримаємо в пам'яті й перечитуємо лише
# коли змінився час його зміни — правки з інтерфейсу підхоплюються так
# само миттєво.
_cache = {"mtime": None, "data": {}}


def load_config() -> dict:
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
    except OSError:
        return {}
    if _cache["mtime"] != mtime:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            _cache["data"] = json.load(f)
        _cache["mtime"] = mtime
    return _cache["data"]


def save_config(data: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    _cache["mtime"] = None      # наступне читання візьме свіже
