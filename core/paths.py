"""paths"""

import os, secrets



CONFIG_FILE     = os.path.join(os.environ["CONFIG_DIR"], "archiver_config.json") if os.environ.get("CONFIG_DIR") \
                   else ("/data/archiver_config.json" if os.path.isdir("/data") else "archiver_config.json")
PENDING_DIR     = "/data/pending_review" if os.path.isdir("/data") else "./pending_review"
# Ключ сесій (і шифрування Telegram) — поруч із файлом налаштувань, а не
# в теці даних: якщо задано окрему теку налаштувань (CONFIG_DIR), ключ
# лежить там і в сховище архіву не потрапляє зовсім.
SECRET_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(CONFIG_FILE)), ".secret_key")
_OLD_SECRET_KEY_FILES = ["/data/.secret_key", os.path.abspath("./.secret_key")]


def _move_old_secret_key():
    """Ключ зі старого місця — на нове (інакше всі розлогіняться, а Telegram
    доведеться підключати знову)."""
    if os.path.exists(SECRET_KEY_FILE):
        return
    for old in _OLD_SECRET_KEY_FILES:
        if old != SECRET_KEY_FILE and os.path.isfile(old):
            try:
                os.makedirs(os.path.dirname(SECRET_KEY_FILE), exist_ok=True)
                import shutil
                shutil.move(old, SECRET_KEY_FILE)
                os.chmod(SECRET_KEY_FILE, 0o600)
                print(f"  ✓ Ключ сесій перенесено: {old} → {SECRET_KEY_FILE}")
            except OSError as e:
                print(f"  ⚠ Не вдалося перенести ключ сесій з {old}: {e}")
            return


def _get_or_create_secret_key() -> str:
    """Секретний ключ для підпису сесій. Генерується один раз і зберігається,
    щоб сесії не злітали при кожному перезапуску сервера."""
    _move_old_secret_key()
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, "r") as f:
            key = f.read().strip()
            if key:
                return key
    key = secrets.token_hex(32)
    with open(SECRET_KEY_FILE, "w") as f:
        f.write(key)
    try:
        os.chmod(SECRET_KEY_FILE, 0o600)
    except OSError:
        pass
    return key
