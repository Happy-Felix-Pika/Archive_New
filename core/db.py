"""db"""
from core.config import load_config



import pymysql, pymysql.cursors


# ── DB ────────────────────────────────────────────────────────────────────

def _connect():
    """Нове з'єднання з базою."""
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


def get_db():
    """
    З'єднання з базою.

    У межах ОДНОГО http-запиту віддається те саме з'єднання: раніше
    кожен виклик відкривав нове, а з віддаленою базою кожне — це окреме
    мережеве рукостискання, тобто зайва затримка на кожній сторінці.
    Виклики db.close() у наявному коді нічого не ламають: справжнє
    закриття робить сам Flask, коли запит завершився.

    Поза запитом (фонові задачі, планувальник, архіватор) — як і було,
    своє окреме з'єднання, яке закриває той, хто його відкрив.
    """
    try:
        from flask import g, has_app_context
    except Exception:
        return _connect()

    if not has_app_context():
        return _connect()

    conn = getattr(g, "_shared_db", None)
    if conn is not None and conn.open:
        return conn

    conn = _connect()
    conn._real_close = conn.close
    conn.close = lambda: None          # закриє teardown, а не окремий виклик
    g._shared_db = conn
    return conn


def close_shared_db(exc=None):
    """Справжнє закриття спільного з'єднання — у кінці запиту."""
    try:
        from flask import g
    except Exception:
        return
    conn = getattr(g, "_shared_db", None)
    if conn is None:
        return
    g._shared_db = None
    try:
        (getattr(conn, "_real_close", None) or conn.close)()
    except Exception:
        pass


def release_db():
    """
    Віддати спільне зʼєднання запиту ДОСТРОКОВО — перед довгою роботою без
    бази (запит до підключеного архіву, ffmpeg). Інакше зʼєднання висить
    до кінця запиту, а сторінка каталогу з сотнями мініатюр одночасно
    вичерпує ліміт MariaDB: «Too many connections» (1040).
    Якщо після цього знову знадобиться get_db(), буде нове зʼєднання.
    """
    close_shared_db()


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


def table_exists(db=None, name: str = "") -> bool:
    """
    Чи є така таблиця. Потрібно там, де ядро вміє працювати і з даними
    застосунку, і без них: якщо застосунок не встановлено, його таблиці
    просто немає, і це не помилка.

    Відповідь кешується — питати БД на кожен запит галереї не варто.
    """
    if name in _TABLE_CACHE:
        return _TABLE_CACHE[name]
    # З'єднання відкриваємо ЛИШЕ коли відповіді ще немає в кеші: раніше
    # виклик у гарячому шляху (кожен запит галереї) щоразу відкривав і
    # закривав окреме з'єднання, хоч відповідь уже була відома.
    own = db is None
    if own:
        db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""SELECT COUNT(*) AS c FROM information_schema.TABLES
                           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s""", (name,))
            exists = cur.fetchone()["c"] > 0
    finally:
        if own:
            db.close()
    _TABLE_CACHE[name] = exists
    return exists
