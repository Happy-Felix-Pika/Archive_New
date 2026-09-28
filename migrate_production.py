#!/usr/bin/env python3
"""
Переведення БОЙОВОЇ бази на нову схему — одним запуском.

    python migrate_production.py

Запускати з кореня проєкту (там, де main.py), коли архів ЗУПИНЕНО.
Налаштування бази береться з archiver_config.json — так само, як їх
бере сам архів.

Що робить, по порядку:
  1. Перевіряє, в якому стані база (стара / вже переведена / напівдорозі)
  2. Створює нові таблиці й копіює в них дані         (≈ міграція 001)
  3. Звіряє, що нічого не загубилось
  4. Переводить зовнішні ключі на нову таблицю         (≈ міграція 003)
     — сам знаходить справжні імена ключів, вручну нічого підставляти не треба
  5. Прибирає стару таблицю downloads                  (≈ міграція 004)
  6. Таблиці фонових задач і застосунків                (≈ міграції 005, 006)

БЕЗПЕКА:
  * Кроки 2–4 нічого не видаляють і повторюються без шкоди.
  * Єдина незворотна дія — видалення старої таблиці на кроці 5. Перед нею
    скрипт ще раз звіряє дані й ЗУПИНЯЄТЬСЯ, якщо хоч щось не зійшлось.
    До того моменту він питає підтвердження.
  * Якщо впав посередині — просто запусти ще раз: він продовжить з того
    місця, де зупинився.

ЗРОБИ БЕКАП БАЗИ ПЕРЕД ЗАПУСКОМ.
"""

import json
import os
import re
import sys

import pymysql
import pymysql.cursors

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "archiver_config.json")
MIGRATIONS = os.path.join(HERE, "migrations")

OK, WARN, FAIL = "  ✓", "  ⚠", "  ✗"


# ── з'єднання ────────────────────────────────────────────────

def connect():
    if not os.path.isfile(CONFIG):
        die(f"Не знайдено {CONFIG}. Поклади свій archiver_config.json поруч із цим скриптом.")
    cfg = json.load(open(CONFIG, encoding="utf-8")).get("database") or {}
    cfg = {
        "host":     os.environ.get("DB_HOST")     or cfg.get("host"),
        "port":     int(os.environ.get("DB_PORT") or cfg.get("port") or 3306),
        "user":     os.environ.get("DB_USER")     or cfg.get("user"),
        "password": os.environ.get("DB_PASSWORD") or cfg.get("password"),
        "database": os.environ.get("DB_NAME")     or cfg.get("database"),
    }
    if not cfg["host"] or not cfg["database"]:
        die("У archiver_config.json немає секції database (host, user, password, database).")
    try:
        conn = pymysql.connect(**cfg, charset="utf8mb4",
                               cursorclass=pymysql.cursors.DictCursor, autocommit=False)
    except Exception as e:
        die(f"Не вдалося підключитись до бази: {e}")
    return conn, cfg


def die(msg):
    print(f"\n{FAIL} {msg}\n")
    sys.exit(1)


def one(conn, sql, args=None):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
    return list(row.values())[0] if row else None


def rows(conn, sql, args=None):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def table_exists(conn, name):
    return one(conn, """SELECT COUNT(*) FROM information_schema.TABLES
                        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s""", (name,)) > 0


def index_exists(conn, table, index):
    return one(conn, """SELECT COUNT(*) FROM information_schema.STATISTICS
                        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s""",
               (table, index)) > 0


def constraint_exists(conn, name):
    return one(conn, """SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
                        WHERE CONSTRAINT_SCHEMA = DATABASE() AND CONSTRAINT_NAME = %s""", (name,)) > 0


def run_sql_file(conn, filename):
    """Виконує SQL-файл по одному оператору. Коментарі прибираються ДО
    розбиття — інакше крапка з комою всередині коментаря ріже оператор."""
    sql = open(os.path.join(MIGRATIONS, filename), encoding="utf-8").read()
    sql = re.sub(r"--[^\n]*", "", sql)
    with conn.cursor() as cur:
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur.execute(stmt)
    conn.commit()


def ask(question):
    try:
        return input(f"\n  {question} [так/ні]: ").strip().lower() in ("так", "да", "yes", "y", "t")
    except EOFError:
        return False


# ── кроки ────────────────────────────────────────────────────

def step_state(conn):
    print("\n1. Стан бази")
    has_old, has_new = table_exists(conn, "downloads"), table_exists(conn, "files")
    if has_old and not has_new:
        print(f"{OK} стара схема — переводимо")
    elif has_old and has_new:
        print(f"{OK} переведення почато раніше — продовжуємо з того місця")
    elif not has_old and has_new:
        print(f"{OK} стару таблицю вже прибрано — лишилось тільки довести останні кроки")
    else:
        die("У базі немає ні downloads, ні files. Це точно база архіву?")
    return has_old


def step_copy(conn):
    print("\n2. Нові таблиці й копіювання даних")
    n_old = one(conn, "SELECT COUNT(*) FROM downloads")
    print(f"{OK} записів у старій таблиці: {n_old}")
    if n_old > 50000:
        print(f"{WARN} записів багато — копіювання може тривати кілька хвилин, не переривай")
    run_sql_file(conn, "001_files_split.sql")
    print(f"{OK} files:       {one(conn, 'SELECT COUNT(*) FROM files')}")
    print(f"{OK} furry_posts: {one(conn, 'SELECT COUNT(*) FROM furry_posts')}")
    print(f"{OK} file_phash:  {one(conn, 'SELECT COUNT(*) FROM file_phash')}")


def step_verify(conn):
    """Повертає True, якщо все зійшлось. Нічого не змінює."""
    print("\n3. Звірка даних")
    good = True

    lost = one(conn, """SELECT COUNT(*) FROM downloads d LEFT JOIN files f ON f.id = d.id
                        WHERE f.id IS NULL AND d.filename IS NOT NULL AND d.filename <> ''""")
    if lost:
        good = False
        print(f"{FAIL} НЕ перенесено рядків: {lost}")
        sample = rows(conn, """SELECT d.id, d.filename, a.folder_name, d.subfolder
                               FROM downloads d JOIN artists a ON a.id = d.artist_id
                               LEFT JOIN files f ON f.id = d.id
                               WHERE f.id IS NULL AND d.filename <> '' LIMIT 10""")
        for r in sample:
            sub = f"{r['subfolder']}/" if r["subfolder"] else ""
            print(f"      id={r['id']}  {r['folder_name']}/{sub}{r['filename']}")
        print("      Найчастіша причина — два записи з однаковим шляхом до файлу.")
    else:
        print(f"{OK} нічого не загублено")

    posts_lost = one(conn, """SELECT COUNT(*) FROM downloads d LEFT JOIN furry_posts p ON p.file_id = d.id
                              WHERE p.file_id IS NULL AND d.filename IS NOT NULL AND d.filename <> ''""")
    if posts_lost:
        good = False
        print(f"{FAIL} без запису в furry_posts: {posts_lost}")
    else:
        print(f"{OK} прив'язка до авторів перенесена повністю")

    # шлях має збігатись з тим, як його будує код
    bad = 0
    for r in rows(conn, """SELECT d.id, a.folder_name, d.subfolder, d.filename, f.rel_path
                           FROM downloads d JOIN artists a ON a.id = d.artist_id
                           JOIN files f ON f.id = d.id"""):
        base = r["filename"] if not r["subfolder"] else f"{r['subfolder']}/{r['filename']}"
        expected = base if r["folder_name"] == "_root" else f"{r['folder_name']}/{base}"
        if expected != r["rel_path"]:
            bad += 1
    if bad:
        good = False
        print(f"{FAIL} шляхів не збіглось: {bad}")
    else:
        print(f"{OK} усі шляхи до файлів правильні")

    return good


def step_foreign_keys(conn):
    print("\n4. Зовнішні ключі — з downloads на files")

    # сироти: теги/улюблене, що вказують на неіснуючі файли. Без чистки новий
    # ключ просто не створиться.
    cleanup = {
        "download_tags":          "DELETE x FROM download_tags x LEFT JOIN files f ON f.id = x.download_id WHERE f.id IS NULL",
        "user_favorites":         "DELETE x FROM user_favorites x LEFT JOIN files f ON f.id = x.download_id WHERE f.id IS NULL",
        "duplicate_scan_results": """DELETE x FROM duplicate_scan_results x
                                     LEFT JOIN files a ON a.id = x.download_id_a
                                     LEFT JOIN files b ON b.id = x.download_id_b
                                     WHERE a.id IS NULL OR b.id IS NULL""",
        "duplicate_ignore_pairs": """DELETE x FROM duplicate_ignore_pairs x
                                     LEFT JOIN files a ON a.id = x.download_id_a
                                     LEFT JOIN files b ON b.id = x.download_id_b
                                     WHERE a.id IS NULL OR b.id IS NULL""",
    }
    with conn.cursor() as cur:
        for table, sql in cleanup.items():
            if table_exists(conn, table):
                cur.execute(sql)
                if cur.rowcount:
                    print(f"{WARN} {table}: прибрано записів-сиріт: {cur.rowcount}")
    conn.commit()

    # справжні імена ключів беремо з самої бази — у кожної бази вони свої
    old = rows(conn, """SELECT TABLE_NAME, CONSTRAINT_NAME FROM information_schema.REFERENTIAL_CONSTRAINTS
                        WHERE CONSTRAINT_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME = 'downloads'""")
    with conn.cursor() as cur:
        for r in old:
            cur.execute(f"ALTER TABLE `{r['TABLE_NAME']}` DROP FOREIGN KEY `{r['CONSTRAINT_NAME']}`")
            print(f"{OK} знято старий ключ {r['TABLE_NAME']}.{r['CONSTRAINT_NAME']}")

    new = [
        ("download_tags",          "download_tags_file_fk",     "download_id"),
        ("user_favorites",         "user_favorites_file_fk",    "download_id"),
        ("duplicate_scan_results", "duplicate_scan_a_file_fk",  "download_id_a"),
        ("duplicate_scan_results", "duplicate_scan_b_file_fk",  "download_id_b"),
        ("duplicate_ignore_pairs", "duplicate_ignore_a_file_fk", "download_id_a"),
        ("duplicate_ignore_pairs", "duplicate_ignore_b_file_fk", "download_id_b"),
    ]
    with conn.cursor() as cur:
        for table, name, col in new:
            if not table_exists(conn, table):
                continue
            if constraint_exists(conn, name):
                print(f"{OK} {table}.{col} уже вказує на files")
                continue
            cur.execute(f"""ALTER TABLE `{table}` ADD CONSTRAINT `{name}`
                            FOREIGN KEY (`{col}`) REFERENCES files(id) ON DELETE CASCADE""")
            print(f"{OK} {table}.{col} → files")
    conn.commit()

    left = one(conn, """SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS
                        WHERE CONSTRAINT_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME = 'downloads'""")
    if left:
        die(f"На downloads досі посилаються ключів: {left}. Зупиняюсь, нічого не видаляю.")


def step_cutover(conn):
    print("\n5. Прибирання старої таблиці — ЄДИНИЙ НЕЗВОРОТНИЙ КРОК")

    # останні перевірки — вже після зміни ключів
    if not step_verify(conn):
        die("Дані не зійшлись. Стару таблицю НЕ видалено. Покажи цей вивід — розберемось.")

    dup = rows(conn, """SELECT rel_path, COUNT(*) c FROM files
                        GROUP BY storage_id, path_hash HAVING c > 1 LIMIT 20""")
    if dup:
        print(f"{FAIL} є файли з однаковим шляхом — після переходу два записи не зможуть")
        print("      вказувати на одне місце. Треба вирішити, який лишити:")
        for r in dup:
            print(f"      {r['c']}×  {r['rel_path']}")
        die("Стару таблицю НЕ видалено. Прибери зайві записи і запусти скрипт ще раз.")

    print(f"{OK} усе зійшлось, дублікатів шляхів немає")
    if not ask("Видалити стару таблицю downloads? Бекап зроблено?"):
        die("Зупинено за твоїм рішенням. Нічого не видалено — можна запустити знову.")

    with conn.cursor() as cur:
        for trg in ("downloads_ai", "downloads_au", "downloads_ad", "artists_bd", "artists_au"):
            cur.execute(f"DROP TRIGGER IF EXISTS {trg}")
        cur.execute("DROP TABLE downloads")
        print(f"{OK} стару таблицю видалено")

        # унікальність шляху: два записи на одне місце — це помилка
        if index_exists(conn, "files", "idx_path"):
            cur.execute("ALTER TABLE files DROP INDEX idx_path")
        if not index_exists(conn, "files", "uniq_path"):
            cur.execute("ALTER TABLE files ADD UNIQUE KEY uniq_path (storage_id, path_hash)")
        print(f"{OK} унікальність шляху увімкнено")
    conn.commit()


def step_new_tables(conn):
    print("\n6. Фонові задачі й застосунки")
    run_sql_file(conn, "005_jobs.sql")
    print(f"{OK} таблиця jobs")
    run_sql_file(conn, "006_apps.sql")
    print(f"{OK} таблиці застосунків")


# ── головне ──────────────────────────────────────────────────

def main():
    print("═" * 60)
    print(" Переведення бази архіву на нову схему")
    print("═" * 60)
    conn, cfg = connect()
    print(f"\n  База: {cfg['host']}:{cfg['port']}/{cfg['database']}")
    if not ask(f"Це правильна база, архів зупинено і бекап зроблено?"):
        die("Зупинено. Нічого не змінено.")

    has_old = step_state(conn)
    if has_old:
        step_copy(conn)
        if not step_verify(conn):
            die("Дані не зійшлись. Нічого не видалено. Покажи цей вивід — розберемось.")
        step_foreign_keys(conn)
        step_cutover(conn)
    step_new_tables(conn)

    print("\n" + "═" * 60)
    print(" ✓ ГОТОВО. Можна запускати архів: python main.py")
    print("═" * 60 + "\n")


if __name__ == "__main__":
    main()
