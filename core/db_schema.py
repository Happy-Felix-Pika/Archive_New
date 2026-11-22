"""db_schema"""

import base64, hashlib, json, mimetypes, os, queue, random, re, secrets, subprocess, sys, threading, time
from datetime import datetime, timedelta
from functools import wraps

import pymysql, pymysql.cursors
from flask import (Blueprint, Response, jsonify, request as flask_request,
                   send_file, send_from_directory, session)
from werkzeug.security import check_password_hash, generate_password_hash
from core.db import get_db


# ── Migrate / ensure tables exist ─────────────────────────────────────────

ENSURE_TABLES_SQL = [
    # Публічні посилання «поділитися»: файл або тека, відкриваються без входу
    """CREATE TABLE IF NOT EXISTS share_links (
        id          BIGINT AUTO_INCREMENT PRIMARY KEY,
        token       VARCHAR(64) NOT NULL UNIQUE,
        kind        VARCHAR(10) NOT NULL,
        rel_path    VARCHAR(1024) NOT NULL,
        file_id     BIGINT NULL,
        pool_id     INT NULL,
        created_by  INT NOT NULL,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        expires_at  DATETIME NULL,
        views       INT NOT NULL DEFAULT 0,
        INDEX idx_by (created_by)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    # Журнал дій модераторів: що зроблено, ким, і що потрібно, щоб це
    # відмінити (payload). Адмін бачить журнал і відміняє одним кліком.
    """CREATE TABLE IF NOT EXISTS mod_actions (
        id          BIGINT AUTO_INCREMENT PRIMARY KEY,
        user_id     INT NOT NULL,
        action      VARCHAR(40) NOT NULL,
        summary     VARCHAR(500),
        payload     LONGTEXT,
        undoable    TINYINT(1) NOT NULL DEFAULT 1,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        undone_at   DATETIME NULL,
        undone_by   INT NULL,
        undo_error  VARCHAR(500) NULL,
        INDEX idx_created (created_at),
        INDEX idx_user (user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # Пропозиції тегів: користувач пропонує додати/прибрати тег у файла,
    # модератор — видалити тег з архіву взагалі. Вирішують модератор/адмін
    # (видалення тегу цілком — лише адмін).
    """CREATE TABLE IF NOT EXISTS tag_suggestions (
        id           BIGINT AUTO_INCREMENT PRIMARY KEY,
        action       VARCHAR(16) NOT NULL,
        file_id      BIGINT NULL,
        tag_id       INT NULL,
        tag_name     VARCHAR(100) NOT NULL,
        category     VARCHAR(20) NOT NULL DEFAULT 'other',
        suggested_by INT NOT NULL,
        status       VARCHAR(16) NOT NULL DEFAULT 'pending',
        decided_by   INT NULL,
        decided_at   DATETIME NULL,
        created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_status (status),
        INDEX idx_file (file_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    
    # downloads більше немає: файл живе у files, а прив'язка до автора
    # й поста — у furry_posts. Обидві створює міграція 001.
    

    
    
    
    """CREATE TABLE IF NOT EXISTS external_favorites (
        id                BIGINT AUTO_INCREMENT PRIMARY KEY,
        user_id           INT NOT NULL,
        archive_id        VARCHAR(64) NOT NULL,
        remote_download_id BIGINT NOT NULL,
        created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_ext_fav (user_id, archive_id, remote_download_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS key_hidden_folders (
        key_id      INT NOT NULL,
        folder_name VARCHAR(255) NOT NULL,
        PRIMARY KEY (key_id, folder_name),
        FOREIGN KEY (key_id) REFERENCES external_access_keys(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS catalog_folder_hidden (
        folder_name VARCHAR(255) NOT NULL,
        user_id     INT NOT NULL,
        PRIMARY KEY (folder_name, user_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # chat_messages створює застосунок 'chat' своєю міграцією.
    

    """CREATE TABLE IF NOT EXISTS external_archives (
        id               VARCHAR(64) PRIMARY KEY,
        added_by_user_id INT NOT NULL,
        label            VARCHAR(255) NOT NULL,
        base_url         VARCHAR(512) NOT NULL,
        access_key       VARCHAR(255) NOT NULL,
        my_identity      VARCHAR(255),
        created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS external_archive_access (
        archive_id VARCHAR(64) NOT NULL,
        user_id    INT NOT NULL,
        include_in_combined TINYINT(1) DEFAULT 1,
        PRIMARY KEY (archive_id, user_id),
        FOREIGN KEY (archive_id) REFERENCES external_archives(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS external_access_keys (
        id             INT AUTO_INCREMENT PRIMARY KEY,
        key_hash       VARCHAR(64) UNIQUE NOT NULL,
        label          VARCHAR(255),
        is_active      TINYINT(1) DEFAULT 1,
        created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
        last_used_at   DATETIME,
        last_used_from VARCHAR(255)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    
    """CREATE TABLE IF NOT EXISTS user_favorites (
        id           BIGINT AUTO_INCREMENT PRIMARY KEY,
        user_id      INT NOT NULL,
        download_id  BIGINT NOT NULL,
        created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_user_fav (user_id, download_id),
        INDEX idx_download (download_id),
        FOREIGN KEY (download_id) REFERENCES files(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    
    
    """CREATE TABLE IF NOT EXISTS files (
        id            BIGINT AUTO_INCREMENT PRIMARY KEY,
        storage_id    VARCHAR(64)   NOT NULL DEFAULT 'default',
        rel_path      VARCHAR(1024) NOT NULL,
        path_hash     CHAR(32) AS (MD5(rel_path)) STORED,
        name          VARCHAR(255)  NOT NULL,
        ext           VARCHAR(10),
        size_bytes    BIGINT UNSIGNED,
        width         INT UNSIGNED,
        height        INT UNSIGNED,
        md5_hash      CHAR(32),
        owner_user_id INT,
        created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_path (storage_id, path_hash),
        INDEX idx_md5 (md5_hash),
        INDEX idx_name (name),
        INDEX idx_ext (ext)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;""",

    """CREATE TABLE IF NOT EXISTS key_file_exceptions (
        key_id     INT    NOT NULL,
        file_id    BIGINT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (key_id, file_id),
        FOREIGN KEY (key_id)  REFERENCES external_access_keys(id) ON DELETE CASCADE,
        FOREIGN KEY (file_id) REFERENCES files(id)                ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS trash_items (
        id                BIGINT AUTO_INCREMENT PRIMARY KEY,
        file_id           BIGINT NOT NULL,           -- id файлу до видалення: відновлюємо з тим самим
        name              VARCHAR(255),
        ext               VARCHAR(10),
        size_bytes        BIGINT UNSIGNED,
        storage_id        VARCHAR(64) NOT NULL DEFAULT 'default',
        original_rel_path VARCHAR(1024) NOT NULL,    -- куди повертати
        trash_rel_path    VARCHAR(1024) NOT NULL,    -- де лежить зараз (.trash/...)
        snapshot          LONGTEXT,                  -- JSON: файл + теги, улюблене + дані застосунків
        deleted_by        INT,
        deleted_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_deleted_at (deleted_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # Видалена тека: усі її файли в кошику мають спільну позначку, щоб кошик
    # показував теку одним рядком і відновлював/стирав її цілком.
    """ALTER TABLE trash_items
        ADD COLUMN IF NOT EXISTS folder_rel   VARCHAR(1024) NULL,
        ADD COLUMN IF NOT EXISTS folder_batch VARCHAR(32)   NULL,
        ADD INDEX IF NOT EXISTS idx_folder_batch (folder_batch)""",

    """CREATE TABLE IF NOT EXISTS tags (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        name       VARCHAR(100) NOT NULL UNIQUE,
        category   ENUM('character','species','general','other') DEFAULT 'other',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_name (name),
        INDEX idx_category (category)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS download_tags (
        download_id BIGINT NOT NULL,
        tag_id      INT NOT NULL,
        PRIMARY KEY (download_id, tag_id),
        FOREIGN KEY (download_id) REFERENCES files(id) ON DELETE CASCADE,
        FOREIGN KEY (tag_id)      REFERENCES tags(id)      ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    
    """CREATE TABLE IF NOT EXISTS users (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        email         VARCHAR(255) NOT NULL UNIQUE,
        password_hash VARCHAR(255) NOT NULL,
        display_name  VARCHAR(255),
        is_admin      TINYINT(1) DEFAULT 0,
        setup_done    TINYINT(1) DEFAULT 0,
        storage_mode  ENUM('own','shared') DEFAULT 'shared',
        settings_json TEXT,
        created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
        last_login_at DATETIME,
        INDEX idx_email (email)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS access_codes (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        code        VARCHAR(64) NOT NULL UNIQUE,
        created_by  INT NOT NULL,
        used_by     INT DEFAULT NULL,
        used_at     DATETIME DEFAULT NULL,
        is_revoked  TINYINT(1) DEFAULT 0,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_code (code),
        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY (used_by)    REFERENCES users(id) ON DELETE SET NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # Запити на відновлення пароля: лист з одноразовим посиланням або —
    # якщо пошта не налаштована — запит до адміна. Токен зберігається лише
    # як sha256, саме посилання знає тільки адресат листа.
    """CREATE TABLE IF NOT EXISTS password_resets (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        user_id     INT NOT NULL,
        token_hash  CHAR(64) DEFAULT NULL,
        status      ENUM('pending','emailed','done','rejected') NOT NULL DEFAULT 'pending',
        ip          VARCHAR(64) DEFAULT NULL,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        expires_at  DATETIME DEFAULT NULL,
        resolved_by INT DEFAULT NULL,
        resolved_at DATETIME DEFAULT NULL,
        INDEX idx_token (token_hash),
        INDEX idx_status (status),
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


def _core_migration_tables() -> list:
    """
    Таблиці ядра, описані лише в migrations/ (черга задач, застосунки).
    На вже наявних базах їх створив перехід на нову структуру, а на новій
    установці — створюємо тут. Там лише CREATE TABLE IF NOT EXISTS.
    """
    import os, re
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
    out = []
    for fn in ("005_jobs.sql", "006_apps.sql"):
        try:
            with open(os.path.join(base, fn), encoding="utf-8") as f:
                body = re.sub(r"--[^\n]*", "", f.read())
        except OSError:
            continue
        out += [st.strip() for st in body.split(";") if st.strip().upper().startswith("CREATE TABLE IF NOT EXISTS")]
    return out


def ensure_tables():
    """Создаём таблицы если их нет. Вызывается при старте сервера."""
    try:
        db = get_db()
        with db.cursor() as cur:
            # Історичні міграції нижче правлять таблицю downloads, якої після
            # переходу на files/furry_posts більше немає. Виконуємо їх лише
            # на старій схемі — інакше вони обривають увесь ensure_tables.
            cur.execute("""SELECT COUNT(*) AS c FROM information_schema.TABLES
                           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'downloads'""")
            HAS_OLD = cur.fetchone()["c"] > 0
            # Те саме для таблиць, що переїхали в застосунки: на чистому
            # універсальному архіві їх просто немає.
            cur.execute("""SELECT COUNT(*) AS c FROM information_schema.TABLES
                           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'artists'""")
            HAS_FURRY = cur.fetchone()["c"] > 0
            # Таблиці зі зовнішніми ключами можуть стояти в списку РАНІШЕ за ті,
            # на які посилаються. На вже наявній БД це непомітно, а на порожній
            # MySQL відмовляє (errno 150) і решта таблиць не створюється взагалі.
            pending = list(ENSURE_TABLES_SQL) + _core_migration_tables()
            while pending:
                failed, last_err = [], None
                for sql in pending:
                    try:
                        cur.execute(sql)
                    except Exception as e:
                        failed.append(sql); last_err = e
                if len(failed) == len(pending):
                    raise last_err
                pending = failed
            # share_links.pool_id — посилання на цілий пул (таблиця вже могла бути без нього)
            cur.execute("""SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'share_links'
                             AND COLUMN_NAME = 'pool_id'""")
            if cur.fetchone()["c"] == 0:
                cur.execute("ALTER TABLE share_links ADD COLUMN pool_id INT NULL AFTER file_id")
            # matched_folder_name для крос-авторських дублікатів
            if HAS_FURRY:   # таблиця належить застосунку
                cur.execute("""
                    SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'pending_duplicates'
                      AND COLUMN_NAME = 'matched_folder_name'
                """)
                if cur.fetchone()['c'] == 0:
                    cur.execute("""
                        ALTER TABLE pending_duplicates
                        ADD COLUMN matched_folder_name VARCHAR(255)
                        AFTER matched_filename
                    """)
                    print("  ✓ Додано колонку pending_duplicates.matched_folder_name")
            # Додаємо category якщо ще немає (для існуючих БД)
            cur.execute("""
                SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'tags'
                  AND COLUMN_NAME = 'category'
            """)
            if cur.fetchone()['c'] == 0:
                cur.execute("""
                    ALTER TABLE tags
                    ADD COLUMN category ENUM('character','species','general','other')
                    DEFAULT 'other' AFTER name
                """)
                print("  ✓ Додано колонку tags.category")

            # width/height для downloads (розмір файлу в пікселях)
            if HAS_OLD:   # стара схема — колонка ще в downloads
                cur.execute("""
                    SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'downloads'
                      AND COLUMN_NAME = 'width'
                """)
                if cur.fetchone()['c'] == 0:
                    cur.execute("ALTER TABLE downloads ADD COLUMN width INT UNSIGNED AFTER file_ext")
                    cur.execute("ALTER TABLE downloads ADD COLUMN height INT UNSIGNED AFTER width")
                    print("  ✓ Додано колонки downloads.width, downloads.height")

            # subfolder — для e621 pool (серій/коміксів): @автор/НазваPool/файл
            if HAS_OLD:   # стара схема — колонка ще в downloads
                cur.execute("""
                    SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'downloads'
                      AND COLUMN_NAME = 'subfolder'
                """)
                if cur.fetchone()['c'] == 0:
                    cur.execute("ALTER TABLE downloads ADD COLUMN subfolder VARCHAR(255) AFTER page_num")
                    print("  ✓ Додано колонку downloads.subfolder (для e621 pool)")

            # Індекси для швидкості — на великих бібліотеках (сотні тисяч
            # файлів) їх відсутність помітно гальмує пошук/фільтрацію
            def _ensure_index(table, index_name, column_expr):
                cur.execute("""SELECT COUNT(*) AS c FROM information_schema.TABLES
                               WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s""", (table,))
                if cur.fetchone()['c'] == 0:
                    return          # таблиці ще немає (застосунок створить її пізніше)
                cur.execute("""
                    SELECT COUNT(*) AS c FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s
                """, (table, index_name))
                if cur.fetchone()['c'] == 0:
                    cur.execute(f"ALTER TABLE {table} ADD INDEX {index_name} ({column_expr})")
                    print(f"  ✓ Додано індекс {table}.{index_name}")

            if HAS_OLD:
                _ensure_index('downloads', 'idx_post_id', 'post_id')
                _ensure_index('downloads', 'idx_subfolder', 'subfolder')
            if HAS_FURRY:
                _ensure_index('furry_posts', 'idx_post_id2', 'post_id')
            _ensure_index('download_tags', 'idx_tag_id', 'tag_id')
            # «за добу» на «Огляді» — без індексу це прохід усією таблицею
            _ensure_index('files', 'idx_created', 'created_at')

            # Заднім числом виправляємо вже наявні записи "На перевірку",
            # у яких matched_folder_name порожній (створені до виправлення
            # в furry_archiver.py, яке тепер передає цю папку одразу) —
            # знаходимо реальну папку через сам збережений файл-дублікат.
            if HAS_OLD:
                cur.execute("""
                    UPDATE pending_duplicates p
                    JOIN downloads d ON d.post_id = p.matched_post_id AND d.filename = p.matched_filename
                    JOIN artists a  ON a.id = d.artist_id
                    SET p.matched_folder_name = a.folder_name
                    WHERE p.matched_folder_name IS NULL OR p.matched_folder_name = ''
                """)
                if cur.rowcount:
                    print(f"  ✓ Долатано matched_folder_name для {cur.rowcount} записів 'На перевірку'")

            # pool_order — позиція файлу всередині pool (щоб показувати по черзі,
            # а не в випадковому порядку)
            if HAS_OLD:   # стара схема — колонка ще в downloads
                cur.execute("""
                    SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'downloads'
                      AND COLUMN_NAME = 'pool_order'
                """)
                if cur.fetchone()['c'] == 0:
                    cur.execute("ALTER TABLE downloads ADD COLUMN pool_order SMALLINT UNSIGNED AFTER subfolder")
                    print("  ✓ Додано колонку downloads.pool_order")

            # downloads.source мала бути NOT NULL — через це файли з "чужою"
            # назвою (не e621_XXXX/ib_XXXX, тобто покладені вручну) при
            # синхронізації просто мовчки пропускались (INSERT IGNORE ковтав
            # помилку NOT NULL без жодного сліду в лозі). Дозволяємо NULL —
            # означає "джерело невідоме", а не забороняємо такі файли взагалі.
            if HAS_OLD:   # стара схема — колонка ще в downloads
                cur.execute("""
                    SELECT IS_NULLABLE FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'downloads'
                      AND COLUMN_NAME = 'source'
                """)
            src_col = cur.fetchone()
            if src_col and src_col['IS_NULLABLE'] == 'NO':
                cur.execute("ALTER TABLE downloads MODIFY COLUMN source ENUM('e621','inkbunny') NULL")
                print("  ✓ downloads.source тепер може бути NULL (файли без визначеного джерела більше не губляться)")

            # auto_update — чи перевіряти цього автора на нове при щоденному
            # авто-оновленні (за замовчуванням увімкнено для всіх)
            if HAS_FURRY:   # таблиця належить застосунку
                cur.execute("""
                    SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'artists'
                      AND COLUMN_NAME = 'auto_update'
                """)
                if cur.fetchone()['c'] == 0:
                    cur.execute("ALTER TABLE artists ADD COLUMN auto_update TINYINT(1) DEFAULT 1")
                    print("  ✓ Додано колонку artists.auto_update")

            # include_in_combined — чи включати цей архів у "Усі разом",
            # окремо на кожного юзера (той самий архів може бути виключений
            # в одного й увімкнений в іншого)
            cur.execute("""
                SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'external_archive_access'
                  AND COLUMN_NAME = 'include_in_combined'
            """)
            if cur.fetchone()['c'] == 0:
                cur.execute("ALTER TABLE external_archive_access ADD COLUMN include_in_combined TINYINT(1) DEFAULT 1")
                print("  ✓ Додано колонку external_archive_access.include_in_combined")

            # size_bytes — вага файлу (для сортування авторів "по вазі")
            if HAS_OLD:   # стара схема — колонка ще в downloads
                cur.execute("""
                    SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'downloads' AND COLUMN_NAME = 'size_bytes'
                """)
                if cur.fetchone()['c'] == 0:
                    cur.execute("ALTER TABLE downloads ADD COLUMN size_bytes BIGINT UNSIGNED AFTER pool_order")
                    print("  ✓ Додано колонку downloads.size_bytes")

            # is_favorite — особисті улюблені, окремо від "вже в бібліотеці"
            if HAS_OLD:   # стара схема — колонка ще в downloads
                cur.execute("""
                    SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'downloads' AND COLUMN_NAME = 'is_favorite'
                """)
                if cur.fetchone()['c'] == 0:
                    cur.execute("ALTER TABLE downloads ADD COLUMN is_favorite TINYINT(1) DEFAULT 0 AFTER size_bytes")
                    cur.execute("ALTER TABLE downloads ADD INDEX idx_favorite (is_favorite)")
                    print("  ✓ Додано колонку downloads.is_favorite")

            # Перенесення старих "улюблених" (спільних на всіх) у персональні —
            # призначаємо адміну (user_id=1), бо стара колонка не знала, хто саме
            # позначив. Робиться лише один раз (IGNORE — повторний старт нічого
            # не задублює).
            if HAS_OLD:
                cur.execute("""
                    INSERT IGNORE INTO user_favorites (user_id, download_id)
                    SELECT 1, id FROM downloads WHERE is_favorite = 1
                """)
                if cur.rowcount:
                    print(f"  ✓ Перенесено {cur.rowcount} старих улюблених у персональний список (адмін)")

            # source має дозволяти NULL — для файлів з невідомим джерелом
            # (наприклад доданих вручну через Синхронізацію)
            if HAS_OLD:   # стара схема — колонка ще в downloads
                cur.execute("""
                    SELECT IS_NULLABLE FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'downloads'
                      AND COLUMN_NAME = 'source'
                """)
            row = cur.fetchone()
            if row and row['IS_NULLABLE'] == 'NO':
                cur.execute("ALTER TABLE downloads MODIFY source ENUM('e621','inkbunny')")
                print("  ✓ downloads.source тепер дозволяє NULL (невідоме джерело)")

            # ── Видаляємо мультитенантність: artists.user_id більше не потрібен ──
            if HAS_FURRY:   # таблиця належить застосунку
                cur.execute("""
                    SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'artists'
                      AND COLUMN_NAME = 'user_id'
                """)
                if cur.fetchone()['c'] > 0:
                    # Мерджимо можливі дублікати folder_name (різні юзери архівували
                    # одного й того ж автора) в один запис, перш ніж знімати user_id
                    cur.execute("""
                        SELECT folder_name, MIN(id) AS keep_id
                        FROM artists GROUP BY folder_name HAVING COUNT(*) > 1
                    """)
                    for d in cur.fetchall():
                        cur.execute("SELECT id FROM artists WHERE folder_name=%s AND id != %s",
                                    (d['folder_name'], d['keep_id']))
                        dup_ids = [r['id'] for r in cur.fetchall()]
                        for did in dup_ids:
                            for tbl in (("downloads",) if HAS_OLD else ()) + ("pending_duplicates", "rejected_duplicates"):
                                cur.execute(f"UPDATE IGNORE {tbl} SET artist_id=%s WHERE artist_id=%s",
                                            (d['keep_id'], did))
                                cur.execute(f"DELETE FROM {tbl} WHERE artist_id=%s", (did,))
                            cur.execute("DELETE FROM artists WHERE id=%s", (did,))
                    # Знімаємо старі ключі/індекси, привʼязані до user_id
                    for idx_name in ("uniq_user_folder", "idx_user"):
                        try:
                            cur.execute(f"ALTER TABLE artists DROP INDEX {idx_name}")
                        except Exception:
                            pass
                    cur.execute("ALTER TABLE artists DROP COLUMN user_id")
                    try:
                        cur.execute("ALTER TABLE artists ADD UNIQUE KEY uniq_folder (folder_name)")
                    except Exception:
                        pass
                    print("  ✓ Контент знову спільний для всіх — artists.user_id видалено")

            # ── access_codes: видалення колонки used_by FK залежить від users,
            #    таблиця створюється через ENSURE_TABLES_SQL вище ──────────────

            # ── users.storage_mode / settings_json ───────────────────────────
            # (колонки лишені в схемі для сумісності зі старими БД, але більше
            #  не використовуються — усі налаштування знову зберігаються у
            #  archiver_config.json, а не в БД)
            cur.execute("""
                SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'users'
                  AND COLUMN_NAME = 'storage_mode'
            """)
            if cur.fetchone()['c'] == 0:
                cur.execute("ALTER TABLE users ADD COLUMN storage_mode ENUM('own','shared') DEFAULT 'shared'")
                cur.execute("ALTER TABLE users ADD COLUMN settings_json TEXT")
                print("  ✓ Додано колонки users.storage_mode, users.settings_json")

            # ── users.role: user / moderator / admin ─────────────────────────
            # is_admin лишається (його читає багато коду) і завжди дорівнює
            # role='admin' — їх синхронізує зміна ролі.
            cur.execute("""
                SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'role'
            """)
            if cur.fetchone()['c'] == 0:
                cur.execute("ALTER TABLE users ADD COLUMN role VARCHAR(16) NOT NULL DEFAULT 'user'")
                cur.execute("UPDATE users SET role='admin' WHERE is_admin=1")
                print("  ✓ Додано колонку users.role")
            # Творець (id 1) — завжди адмін, хоч би що було в базі
            cur.execute("UPDATE users SET role='admin', is_admin=1 WHERE id=1 AND (is_admin<>1 OR role<>'admin')")

            # Службові файли архіву (ключ сесій, налаштування, кеш), які
            # синхронізація колись внесла в архів як звичайні файли, коли
            # тека сховища збігалась з /data, — прибираємо з бази (з диска
            # нічого не видаляється).
            from core.storage_api import SERVICE_ROOT_NAMES
            names = sorted(SERVICE_ROOT_NAMES)
            ph = ",".join(["%s"] * len(names))
            likes = " OR ".join(["rel_path LIKE %s"] * len(names))
            cur.execute(f"""DELETE FROM files WHERE rel_path IN ({ph}) OR {likes}
                            OR rel_path LIKE '.%%' OR rel_path LIKE '%%/.%%'""",
                        names + [n + "/%" for n in names])
            if cur.rowcount:
                print(f"  ✓ Прибрано з архіву службових файлів: {cur.rowcount}")

        db.commit()
        db.close()
        print("  ✓ Таблицы БД проверены/созданы")
    except Exception as e:
        print(f"  ⚠ Не удалось создать таблицы: {e}")
