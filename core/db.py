"""db"""
from core.config import load_config



import pymysql, pymysql.cursors


# ── DB ────────────────────────────────────────────────────────────────────

def get_db():
    """
    Підключення до основної БД додатку.
    Конфіг береться з archiver_config.json (секція 'database'), як було раніше.
    """
    cfg = load_config().get("database", {})

    if not cfg.get("host") or not cfg.get("user") or not cfg.get("database"):
        raise RuntimeError(
            "База даних не налаштована. Вкажи DB_HOST/DB_USER/DB_PASSWORD/DB_NAME "
            "у змінних оточення контейнера (рекомендовано), або заповни секцію "
            "'database' у archiver_config.json."
        )

    return pymysql.connect(
        host=cfg.get("host", "localhost"),
        port=int(cfg.get("port", 3306)),
        user=cfg.get("user", ""),
        password=cfg.get("password", ""),
        database=cfg.get("database", ""),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
    )


def q(db, sql, args=None):
    """Выполнить запрос и вернуть первую строку (скалярный COUNT)."""
    with db.cursor() as cur:
        cur.execute(sql, args or ())
        return cur.fetchone()

def qq(db, sql, args=None):
    """Вернуть все строки."""
    with db.cursor() as cur:
        cur.execute(sql, args or ())
        return cur.fetchall()


_TABLE_CACHE = {}


def table_exists(db, name: str) -> bool:
    """
    Чи є така таблиця. Потрібно там, де ядро вміє працювати і з даними
    застосунку, і без них: якщо застосунок не встановлено, його таблиці
    просто немає, і це не помилка.

    Відповідь кешується — питати БД на кожен запит галереї не варто.
    """
    if name in _TABLE_CACHE:
        return _TABLE_CACHE[name]
    with db.cursor() as cur:
        cur.execute("""SELECT COUNT(*) AS c FROM information_schema.TABLES
                       WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s""", (name,))
        exists = cur.fetchone()["c"] > 0
    _TABLE_CACHE[name] = exists
    return exists
