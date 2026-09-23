"""paths"""

import os, secrets



CONFIG_FILE     = os.path.join(os.environ["CONFIG_DIR"], "archiver_config.json") if os.environ.get("CONFIG_DIR") \
                   else ("/data/archiver_config.json" if os.path.isdir("/data") else "archiver_config.json")
PENDING_DIR     = "/data/pending_review" if os.path.isdir("/data") else "./pending_review"
SECRET_KEY_FILE = "/data/.secret_key" if os.path.isdir("/data") else "./.secret_key"

def _get_or_create_secret_key() -> str:
    """Секретний ключ для підпису сесій. Генерується один раз і зберігається,
    щоб сесії не злітали при кожному перезапуску сервера."""
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, "r") as f:
            key = f.read().strip()
            if key:
                return key
    key = secrets.token_hex(32)
    with open(SECRET_KEY_FILE, "w") as f:
        f.write(key)
    return key
