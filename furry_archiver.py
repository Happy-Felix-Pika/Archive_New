#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Furry Art Archiver - автоматический архиватор контента с e621 и Inkbunny
Сканирует Nextcloud папку, находит авторов и скачивает контент
Хранит данные в MariaDB + дедупликация через pHash (как MediaDC)
"""

import os
import io
import json
import hashlib
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
import requests
import time
import warnings
from webdav3.client import Client
import argparse
import pymysql
import pymysql.cursors
import imagehash
from PIL import Image

# ── Сумісність з videohash: Pillow >= 10 видалив Image.ANTIALIAS
# (перейменували на Image.Resampling.LANCZOS), а videohash досі на нього
# посилається всередині себе. Повертаємо старий атрибут, щоб не залежати
# від того, яку версію Pillow встановить pip.
if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.Resampling.LANCZOS
from pathlib import Path

# Придушуємо deprecated warning від pymysql для ping(reconnect=True)
warnings.filterwarnings("ignore", message=".*reconnect.*", category=DeprecationWarning)

# Конфигурация
import os as _os
CONFIG_FILE = _os.path.join(_os.environ["CONFIG_DIR"], "archiver_config.json") if _os.environ.get("CONFIG_DIR") \
              else ("/data/archiver_config.json" if _os.path.isdir("/data") else "archiver_config.json")

# E621 API
E621_API_BASE = "https://e621.net"
E621_USER_AGENT = "FurryArchiver/2.0 (by your_username_here)"
E621_RATE_LIMIT = 0.5

# Inkbunny API
INKBUNNY_API_BASE = "https://inkbunny.net/api"
INKBUNNY_RATE_LIMIT = 0.5

# pHash порог схожести (0 = точные копии, 10 = очень похожие)
PHASH_THRESHOLD = 8

# Форматы для которых считаем pHash
IMAGE_FORMATS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
VIDEO_FORMATS = {'webm', 'mp4'}
VIDEOHASH_TIMEOUT = 150  # секунд — довше просто пропускаємо файл, щоб не заморозити всю синхронізацію

# Скачування файлів (I/O) паралелимо агресивно (SYNC_WORKERS, дефолт 8),
# а от саме хешування відео через ffmpeg — це важке навантаження на CPU,
# і якщо запускати 8 таких одночасно на обмеженому контейнері (Docker/Portainer
# часто ставлять ліміт ядер) — вони душать одне одного і жодне не встигає
# вкластись у тайм-аут. Тому хешування відео обмежуємо окремо, набагато
# скромнішим числом, незалежно від того скільки файлів скачується паралельно.
VIDEOHASH_CONCURRENCY = 2
_VIDEO_HASH_SEMAPHORE = threading.Semaphore(VIDEOHASH_CONCURRENCY)

PENDING_DIR = "/data/pending_review" if _os.path.isdir("/data") else "./pending_review"


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

class Config:
    """
    Конфіг архиватора. Все — і 'database', і 'nextcloud'/'e621'/'inkbunny'/
    'download' — читається з одного файлу archiver_config.json (дашборд
    записує туди все через /api/config, /api/user-settings).
    """
    def __init__(self, config_file=CONFIG_FILE, user_id: int | None = None):
        self.config_file = config_file
        self.user_id     = user_id or 1
        self.config      = self._load()
        self._ensure_default_sections()

    def _load(self):
        if os.path.exists(self.config_file):
            with open(self.config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        default = {
            "database": {
                "host": "localhost",
                "port": 3306,
                "user": "furry_user",
                "password": "db_password",
                "database": "furry_archiver"
            }
        }
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(default, f, indent=4, ensure_ascii=False)
        return default

    def _ensure_default_sections(self):
        """Гарантує що всі очікувані секції існують (навіть порожні),
        щоб .get(...) ніде не падав з KeyError."""
        defaults = {
            "nextcloud": {"webdav_url": "", "username": "", "password": "", "base_folder": "",
                          "enabled": False, "local_data_path": ""},
            "local":     {"path": ""},
            "e621":      {"enabled": True, "username": "", "api_key": "", "max_posts_per_artist": None},
            "inkbunny":  {"enabled": True, "username": "", "password": "", "max_submissions_per_artist": None},
            "download":  {"local_temp_dir": "./temp_downloads", "check_duplicates": True,
                          "phash_threshold": PHASH_THRESHOLD,
                          "file_formats": ["png", "jpg", "jpeg", "gif", "webp", "webm", "mp4", "swf"]},
        }
        for section, default_values in defaults.items():
            if section not in self.config or not isinstance(self.config[section], dict):
                self.config[section] = default_values

    def get(self, *keys):
        v = self.config
        for k in keys:
            if not isinstance(v, dict):
                return None
            v = v.get(k)
            if v is None:
                return None
        return v


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────

class Database:
    """Подключение к MariaDB и инициализация таблиц"""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS artists (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        folder_name VARCHAR(255) NOT NULL UNIQUE,
        artist_name VARCHAR(255) NOT NULL,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_folder (folder_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

    -- Таблиці downloads більше немає: файл живе у files (ядро),
    -- а прив'язка до автора й поста — у furry_posts. Обидві створює
    -- міграція, а не архіватор.

    CREATE TABLE IF NOT EXISTS duplicate_ignore_pairs (
        id                BIGINT AUTO_INCREMENT PRIMARY KEY,
        download_id_a     BIGINT NOT NULL,
        download_id_b     BIGINT NOT NULL,
        created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_ignore_pair (download_id_a, download_id_b),
        FOREIGN KEY (download_id_a) REFERENCES files(id) ON DELETE CASCADE,
        FOREIGN KEY (download_id_b) REFERENCES files(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

    CREATE TABLE IF NOT EXISTS duplicate_scan_results (
        id                BIGINT AUTO_INCREMENT PRIMARY KEY,
        artist_id         INT NOT NULL,
        download_id_a     BIGINT NOT NULL,
        download_id_b     BIGINT NOT NULL,
        hamming_distance  TINYINT,
        created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_pair (download_id_a, download_id_b),
        INDEX idx_artist (artist_id),
        FOREIGN KEY (download_id_a) REFERENCES files(id) ON DELETE CASCADE,
        FOREIGN KEY (download_id_b) REFERENCES files(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

    CREATE TABLE IF NOT EXISTS pending_duplicates (
        id               BIGINT AUTO_INCREMENT PRIMARY KEY,
        artist_id        INT NOT NULL,
        post_id          VARCHAR(64) NOT NULL,
        source           ENUM('e621','inkbunny') NOT NULL,
        post_url         VARCHAR(512),
        md5_hash         CHAR(32),
        phash            BIGINT UNSIGNED,
        filename         VARCHAR(255),
        file_ext         VARCHAR(10),
        page_num         TINYINT DEFAULT 1,
        local_path       VARCHAR(512),
        nc_target_path   VARCHAR(512),
        matched_post_id  VARCHAR(64),
        matched_filename VARCHAR(255),
        hamming_distance TINYINT,
        created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_artist (artist_id),
        FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """

    def __init__(self, config: Config):
        db_cfg = config.config.get('database', {})
        self.conn = pymysql.connect(
            host=db_cfg.get('host', 'localhost'),
            port=int(db_cfg.get('port', 3306)),
            user=db_cfg.get('user'),
            password=db_cfg.get('password'),
            database=db_cfg.get('database'),
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False
        )
        self._init_schema()
        print(f"  ✓ БД: подключено к {db_cfg.get('host')}:{db_cfg.get('port')}/{db_cfg.get('database')}")

    def _init_schema(self):
        with self.conn.cursor() as cur:
            for stmt in self.SCHEMA.strip().split(';'):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
        self.conn.commit()

    def cursor(self):
        self.conn.ping(reconnect=True)
        return self.conn.cursor()

    def commit(self):
        self.conn.ping(reconnect=True)
        self.conn.commit()

    def close(self):
        self.conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE TRACKER  (замена DownloadTracker / download.txt)
# ─────────────────────────────────────────────────────────────────────────────

class DatabaseTracker:
    """
    Хранит информацию о скачанных файлах в MariaDB.
    Дедупликация:
      1. по post_id  — не скачиваем одно и то же дважды
      2. по MD5      — точные копии файлов
      3. по pHash    — визуально похожие изображения (как MediaDC)
    """

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.phash_threshold = config.get('download', 'phash_threshold') or PHASH_THRESHOLD
        self._artist_cache: dict[str, int] = {}      # folder_name -> artist_id
        self._phash_cache:  dict[int, list] = {}     # artist_id -> [(phash, post_id, filename)]

    # ── artists ──────────────────────────────────────────────────────────────

    def get_or_create_artist(self, folder_name: str, artist_name: str, user_id: int = 1) -> int:
        cache_key = folder_name
        if cache_key in self._artist_cache:
            return self._artist_cache[cache_key]
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT id FROM artists WHERE folder_name=%s",
                (folder_name,)
            )
            row = cur.fetchone()
            if row:
                aid = row['id']
            else:
                cur.execute(
                    "INSERT INTO artists (folder_name, artist_name) VALUES (%s,%s)",
                    (folder_name, artist_name)
                )
                self.db.commit()
                aid = cur.lastrowid
        self._artist_cache[cache_key] = aid
        return aid

    # ── post_id check ────────────────────────────────────────────────────────

    def is_downloaded(self, artist_id: int, post_id: str, page_num: int = 1) -> bool:
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM furry_posts WHERE artist_id=%s AND post_id=%s AND page_num=%s LIMIT 1",
                (artist_id, post_id, page_num)
            )
            return cur.fetchone() is not None

    def is_post_downloaded(self, artist_id: int, post_id: str) -> bool:
        """Проверяет скачан ли пост (любая страница)"""
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM furry_posts WHERE artist_id=%s AND post_id=%s LIMIT 1",
                (artist_id, post_id)
            )
            return cur.fetchone() is not None

    # ── md5 check ────────────────────────────────────────────────────────────

    def is_md5_duplicate(self, artist_id: int, md5: str) -> bool:
        """Точный дубликат по MD5 среди файлов этого автора"""
        with self.db.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM furry_posts fp JOIN files f ON f.id=fp.file_id
                   WHERE fp.artist_id=%s AND f.md5_hash=%s LIMIT 1""",
                (artist_id, md5)
            )
            return cur.fetchone() is not None

    # ── pHash (MediaDC-style) ─────────────────────────────────────────────────

    def _load_phash_cache(self, artist_id: int):
        if artist_id in self._phash_cache:
            return
        with self.db.cursor() as cur:
            cur.execute(
                """SELECT ph.phash, fp.post_id, f.name AS filename
                   FROM furry_posts fp JOIN files f ON f.id=fp.file_id
                   JOIN file_phash ph ON ph.file_id=f.id
                   WHERE fp.artist_id=%s""",
                (artist_id,)
            )
            rows = cur.fetchall()
        self._phash_cache[artist_id] = [
            (r['phash'], r['post_id'], r['filename']) for r in rows
        ]

    def _load_global_phash_cache(self):
        """Завантажує pHash кеш для ВСІХ авторів — для крос-авторського пошуку дублікатів."""
        if hasattr(self, '_global_phash_loaded') and self._global_phash_loaded:
            return
        with self.db.cursor() as cur:
            cur.execute("""
                SELECT ph.phash, fp.post_id, f.name AS filename, fp.artist_id, a.folder_name
                FROM furry_posts fp
                JOIN files f       ON f.id = fp.file_id
                JOIN file_phash ph ON ph.file_id = f.id
                JOIN artists a     ON a.id = fp.artist_id
            """)
            rows = cur.fetchall()
        # Глобальний список (phash, post_id, filename, artist_id, folder_name)
        self._global_phash_cache = [
            (r['phash'], r['post_id'], r['filename'], r['artist_id'], r['folder_name'])
            for r in rows
        ]
        self._global_phash_loaded = True
        print(f"  → Глобальний pHash кеш: {len(self._global_phash_cache)} записів")

    def find_phash_duplicate(self, artist_id: int, phash_val: int, file_ext: str = '') -> tuple | None:
        """
        Шукає візуально схожий файл ГЛОБАЛЬНО по всіх авторах.
        Для зображень — imagehash hamming distance.
        Для відео — XOR popcount.
        Повертає (post_id, filename, hamming_distance, matched_artist_id) або None.
        """
        # Використовуємо глобальний кеш якщо завантажений, інакше тільки поточного автора
        if hasattr(self, '_global_phash_loaded') and self._global_phash_loaded:
            cache = self._global_phash_cache
            global_search = True
        else:
            self._load_phash_cache(artist_id)
            cache = [(ph, pid, fn, artist_id, '') for ph, pid, fn in self._phash_cache[artist_id]]
            global_search = False

        is_video = file_ext.lower() in VIDEO_FORMATS
        best = None
        best_dist = self.phash_threshold + 1

        for entry in cache:
            stored_phash, post_id, filename = entry[0], entry[1], entry[2]
            stored_artist_id = entry[3] if len(entry) > 3 else artist_id
            stored_folder    = entry[4] if len(entry) > 4 else ''

            stored_ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
            stored_is_video = stored_ext in VIDEO_FORMATS
            if is_video != stored_is_video:
                continue
            try:
                # XOR + підрахунок бітів — той самий hamming distance, що й
                # через imagehash-об'єкти, але без перетворення в hex і назад
                # у важкий numpy-об'єкт 339 тис. разів на кожен новий файл
                # (це раніше й було справжньою причиною жахливої швидкості).
                dist = bin(phash_val ^ stored_phash).count('1')
                if dist <= self.phash_threshold and dist < best_dist:
                    best_dist = dist
                    best = (post_id, filename, dist, stored_artist_id, stored_folder)
            except Exception:
                continue
        return best

    def add_to_phash_cache(self, artist_id: int, phash_val: int, post_id: str, filename: str):
        self._load_phash_cache(artist_id)
        self._phash_cache[artist_id].append((phash_val, post_id, filename))
        # Оновлюємо глобальний кеш якщо він завантажений
        if hasattr(self, '_global_phash_loaded') and self._global_phash_loaded:
            self._global_phash_cache.append((phash_val, post_id, filename, artist_id, ''))

    # ── шлях файлу у сховищі ─────────────────────────────────────────────────

    def _artist_folder(self, artist_id: int):
        """Тека автора у сховищі. Кешуємо — під час скачування питається часто."""
        if not hasattr(self, "_folder_cache"):
            self._folder_cache = {}
        if artist_id not in self._folder_cache:
            with self.db.cursor() as cur:
                cur.execute("SELECT folder_name FROM artists WHERE id=%s", (artist_id,))
                r = cur.fetchone()
            self._folder_cache[artist_id] = r["folder_name"] if r else None
        return self._folder_cache[artist_id]

    def _rel_path(self, artist_id: int, filename: str, subfolder: str | None = None) -> str:
        """
        Шлях від кореня сховища — те, що лежить у files.rel_path.
        Завжди з прямими слешами: це шлях у сховищі, а не в ОС.
        """
        folder = self._artist_folder(artist_id)
        parts  = [] if folder == "_root" else [folder]
        if subfolder:
            parts.append(subfolder)
        parts.append(filename)
        return "/".join(p for p in parts if p)


    def _sync_rel_path(self, file_id: int):
        """
        Перерахувати files.rel_path після переїзду файлу (у pool або назад).

        Фізичне переміщення робить сховище, запис у furry_posts — код вище,
        але ЯДРО має знати нове місце файлу: саме з rel_path потім
        віддається файл. Без цього виклику файл після переїзду в pool
        перестає відкриватись.
        """
        with self.db.cursor() as cur:
            cur.execute("""
                UPDATE files f
                JOIN furry_posts fp ON fp.file_id = f.id
                JOIN artists a      ON a.id       = fp.artist_id
                SET f.rel_path = CONCAT(
                    CASE WHEN a.folder_name = '_root' THEN '' ELSE CONCAT(a.folder_name, '/') END,
                    CASE WHEN fp.subfolder IS NULL OR fp.subfolder = '' THEN '' ELSE CONCAT(fp.subfolder, '/') END,
                    f.name)
                WHERE f.id = %s
            """, (file_id,))

    # ── add download ─────────────────────────────────────────────────────────

    def add_download(self, artist_id: int, post_id: str, source: str,
                     post_url: str, md5_hash: str, filename: str,
                     file_ext: str, page_num: int = 1, phash_val: int | None = None,
                     tags: list | None = None,
                     width: int | None = None, height: int | None = None,
                     subfolder: str | None = None, pool_order: int | None = None,
                     size_bytes: int | None = None):
        rel_path = self._rel_path(artist_id, filename, subfolder)
        with self.db.cursor() as cur:
            # Раніше тут був INSERT IGNORE по ключу (artist_id, post_id, page_num).
            # Тепер той самий ключ живе у furry_posts, тож перевіряємо явно.
            cur.execute("""SELECT file_id FROM furry_posts
                           WHERE artist_id=%s AND post_id=%s AND page_num=%s""",
                        (artist_id, post_id, page_num))
            if cur.fetchone():
                download_id = 0          # вже є — нічого не додаємо
            else:
                cur.execute("""
                    INSERT INTO files (storage_id, rel_path, name, ext, size_bytes,
                                       width, height, md5_hash, created_at)
                    VALUES ('default',%s,%s,%s,%s,%s,%s,%s,NOW())
                """, (rel_path, filename, file_ext, size_bytes, width, height, md5_hash))
                download_id = cur.lastrowid
                cur.execute("""
                    INSERT INTO furry_posts (file_id, artist_id, post_id, source, post_url,
                                             page_num, subfolder, pool_order, downloaded_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                """, (download_id, artist_id, post_id, source, post_url,
                      page_num, subfolder, pool_order))
                if phash_val is not None:
                    cur.execute("""INSERT INTO file_phash (file_id, phash) VALUES (%s,%s)
                                   ON DUPLICATE KEY UPDATE phash=VALUES(phash)""",
                                (download_id, phash_val))
        if phash_val is not None:
            self.add_to_phash_cache(artist_id, phash_val, post_id, filename)
        # Зберігаємо теги якщо є і запис новий
        if tags and download_id:
            self._save_tags(download_id, tags)

    def _save_tags(self, download_id: int, tags: list | dict):
        """
        Зберігає теги для файлу.
        tags може бути:
          - list[str]        → всі з категорією 'other'
          - dict[cat, list]  → {'character': [...], 'species': [...], 'general': [...]}
        """
        if not tags or not download_id:
            return
        # Нормалізуємо до dict
        if isinstance(tags, list):
            tag_map = {'other': tags}
        else:
            tag_map = tags

        with self.db.cursor() as cur:
            for category, names in tag_map.items():
                if category not in ('character', 'species', 'general', 'other'):
                    category = 'other'
                for tag_name in names:
                    tag_name = tag_name.strip().lower()[:100]
                    if not tag_name:
                        continue
                    cur.execute(
                        "INSERT IGNORE INTO tags (name, category) VALUES (%s, %s)",
                        (tag_name, category)
                    )
                    # Якщо тег вже існує але без категорії — оновлюємо
                    cur.execute(
                        "UPDATE tags SET category=%s WHERE name=%s AND category='other'",
                        (category, tag_name)
                    )
                    cur.execute("SELECT id FROM tags WHERE name=%s", (tag_name,))
                    row = cur.fetchone()
                    if row:
                        cur.execute(
                            "INSERT IGNORE INTO download_tags (download_id, tag_id) VALUES (%s,%s)",
                            (download_id, row['id'])
                        )

    def save(self):
        self.db.commit()

    def is_post_pending(self, artist_id: int, post_id: str) -> bool:
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pending_duplicates WHERE artist_id=%s AND post_id=%s LIMIT 1",
                (artist_id, post_id)
            )
            return cur.fetchone() is not None

    def is_rejected(self, artist_id: int, post_id: str, md5: str | None = None) -> bool:
        """Перевіряє чи файл був відхилений як дублікат.
        Перевіряє по post_id і якщо є md5 — по md5 (на випадок зміни файлу)."""
        with self.db.cursor() as cur:
            # По post_id
            cur.execute(
                "SELECT 1 FROM rejected_duplicates WHERE artist_id=%s AND post_id=%s LIMIT 1",
                (artist_id, post_id)
            )
            if cur.fetchone():
                return True
            # По MD5 — якщо файл той самий але з іншим post_id
            if md5:
                cur.execute(
                    "SELECT 1 FROM rejected_duplicates WHERE artist_id=%s AND md5_hash=%s LIMIT 1",
                    (artist_id, md5)
                )
                if cur.fetchone():
                    return True
        return False

    def add_pending(self, artist_id: int, post_id: str, source: str,
                    post_url: str, md5_hash, phash_val, filename: str,
                    file_ext: str, page_num: int, local_path: str,
                    nc_target_path: str, matched_post_id: str,
                    matched_filename: str, hamming_distance: int,
                    matched_folder_name: str = ''):
        with self.db.cursor() as cur:
            cur.execute("""
                INSERT IGNORE INTO pending_duplicates
                    (artist_id, post_id, source, post_url, md5_hash, phash, filename,
                     file_ext, page_num, local_path, nc_target_path,
                     matched_post_id, matched_filename, matched_folder_name, hamming_distance)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (artist_id, post_id, source, post_url, md5_hash, phash_val,
                  filename, file_ext, page_num, local_path, nc_target_path,
                  matched_post_id, matched_filename, matched_folder_name, hamming_distance))

    def update_hashes(self, artist_id: int, post_id: str, page_num: int,
                      md5_hash: str, phash_val: int | None,
                      filename: str, file_ext: str):
        
        with self.db.cursor() as cur:
            cur.execute("""
                UPDATE files f
                JOIN furry_posts fp ON fp.file_id = f.id
                SET
                    f.md5_hash = %s,
                    f.name     = %s,
                    f.ext      = %s
                WHERE
                    fp.artist_id = %s
                    AND fp.post_id  = %s
                    AND fp.page_num = %s
                    AND f.md5_hash IS NULL
            """, (
                md5_hash,
                phash_val,
                filename,
                file_ext,
                artist_id,
                post_id,
                page_num
            ))

            return cur.rowcount

    # ── stats ─────────────────────────────────────────────────────────────────

    def count_downloads(self, artist_id: int) -> int:
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) as c FROM furry_posts WHERE artist_id=%s",
                (artist_id,)
            )
            return cur.fetchone()['c']


# ─────────────────────────────────────────────────────────────────────────────
# pHash утилиты
# ─────────────────────────────────────────────────────────────────────────────

def get_media_dimensions(file_bytes: bytes, ext: str) -> tuple:
    """
    Повертає (width, height) у пікселях для зображення або відео.
    Зображення — через PIL (без запису на диск).
    Відео — через ffprobe (той самий ffmpeg, що вже потрібен для videohash).
    Якщо визначити не вдалося — (None, None).
    """
    ext = ext.lower()
    if ext in IMAGE_FORMATS:
        try:
            img = Image.open(io.BytesIO(file_bytes))
            return img.size  # (width, height)
        except Exception:
            return (None, None)

    if ext in VIDEO_FORMATS:
        import tempfile
        import subprocess
        import json as _json
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=f'.{ext}', delete=False) as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=width,height', '-of', 'json', tmp_path],
                capture_output=True, text=True, timeout=15
            )
            data = _json.loads(result.stdout or '{}')
            streams = data.get('streams', [])
            if streams:
                return streams[0].get('width'), streams[0].get('height')
        except Exception as e:
            print(f"    ⚠ ffprobe error: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    return (None, None)


def compute_phash(file_bytes: bytes, ext: str) -> int | None:
    """
    Вычисляет pHash для изображения або відео.
    Для зображень — imagehash.phash()
    Для відео — videohash (витягує кадри через ffmpeg)
    Повертає int64 або None.
    """
    ext = ext.lower()
    if ext in IMAGE_FORMATS:
        try:
            img = Image.open(io.BytesIO(file_bytes)).convert('RGB')
            h = imagehash.phash(img)
            return int(str(h), 16)
        except Exception:
            return None

    if ext in VIDEO_FORMATS:
        return _compute_videohash(file_bytes, ext)

    return None


def _compute_videohash(file_bytes: bytes, ext: str) -> int | None:
    """
    Обчислює хеш відео через videohash (витягує кадри ffmpeg).

    frame_interval=1.5 замість 0.2 — раніше для довгих відео витягувалось
    по кадру кожні 0.2с (тисячі кадрів на кілька хвилин відео), що дуже
    повільно. Для пошуку дублікатів такий дрібний крок і не потрібен.

    Виконується з жорстким тайм-аутом (VIDEOHASH_TIMEOUT) в окремому потоці —
    якщо ffmpeg/videohash застряг на конкретному файлі (пошкоджений файл,
    занадто довге відео тощо), ми не чекаємо вічно, а пропускаємо цей файл
    з попередженням і йдемо далі. Сам застряглий процес при цьому може
    лишитись висіти у фоні — це прийнятна ціна за те, щоб не заморозити
    всю синхронізацію через один проблемний файл.
    """
    import tempfile
    try:
        from videohash import VideoHash
    except ImportError:
        return None

    def _run(path: str):
        vh = VideoHash(path=path, frame_interval=1.5)
        return str(vh)

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f'.{ext}', delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        # Обмежуємо кількість одночасних ffmpeg-хешувань (CPU), окремо від
        # того, скільки файлів зараз завантажується (I/O) — інакше на
        # обмеженому CPU кілька відео одночасно душать одне одного і жодне
        # не встигає вкластись у тайм-аут.
        with _VIDEO_HASH_SEMAPHORE:
            with ThreadPoolExecutor(max_workers=1) as one:
                future = one.submit(_run, tmp_path)
                try:
                    h_str = future.result(timeout=VIDEOHASH_TIMEOUT)
                except FuturesTimeoutError:
                    print(f"    ⚠ videohash: тайм-аут ({VIDEOHASH_TIMEOUT}с) — пропускаю відео, файл занадто довгий/пошкоджений")
                    return None

        # videohash повертає '0b10110...' — 64-бітний бінарний рядок
        if h_str.startswith('0b'):
            val = int(h_str[2:], 2)  # прибираємо '0b' і парсимо як binary
        elif h_str.startswith('0x'):
            val = int(h_str[2:], 16)
        else:
            # Спробуємо як hex
            try:
                val = int(h_str, 16)
            except ValueError:
                val = int(h_str, 2)
        return val & 0xFFFFFFFFFFFFFFFF  # обмежуємо до 64 біт
    except Exception as e:
        print(f"    ⚠ videohash error: {e}")
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# NEXTCLOUD
# ─────────────────────────────────────────────────────────────────────────────

class NextcloudScanner:
    def __init__(self, config: Config):
        self.config = config
        self.client = Client({
            'webdav_hostname': config.get('nextcloud', 'webdav_url'),
            'webdav_login':    config.get('nextcloud', 'username'),
            'webdav_password': config.get('nextcloud', 'password'),
            'disable_check': True
        })
        # Якщо архіватор працює на тому ж сервері, де стоїть Nextcloud —
        # можна читати файли напряму з диска, оминаючи WebDAV повністю.
        self.local_data_path = (config.get('nextcloud', 'local_data_path') or '').rstrip('/')
        self.nc_username     = config.get('nextcloud', 'username') or ''
        self._direct_ok      = 0
        self._direct_fail    = 0

        if self.local_data_path:
            probe = os.path.join(self.local_data_path, self.nc_username, 'files')
            if os.path.isdir(probe):
                print(f"  ✓ Прямий доступ до диска Nextcloud: {probe} — знайдено, буде використано")
            else:
                print(f"  ⚠ Прямий доступ до диска Nextcloud: {probe} — ПАПКУ НЕ ЗНАЙДЕНО")
                print(f"    Найімовірніша причина, якщо на хості ця папка точно існує:")
                print(f"    архіватор працює у Docker-контейнері, і шлях хоста не змонтований")
                print(f"    усередину контейнера — потрібен volume-маунт у docker-compose.yml,")
                print(f"    що вказує на ту саму папку даних Nextcloud (можна read-only).")
                print(f"    Інакше перевір сам шлях у Конфізі та логін '{self.nc_username}'.")
                print(f"    Поки що всі читання йтимуть через WebDAV (повільніше).")

    def scan_artists(self) -> list:
        base_folder = self.config.get('nextcloud', 'base_folder').strip('/')
        artists = []
        try:
            items = self.client.list(base_folder)
            for item in items:
                item_norm = item.strip('/')
                if item_norm == base_folder:
                    continue
                folder_name = item.rstrip('/').split('/')[-1]
                if folder_name.startswith('@'):
                    artist_name = folder_name[1:]
                    full_path = item if item.startswith(base_folder) else f"{base_folder}/{folder_name}"
                    if not full_path.endswith('/'):
                        full_path += '/'
                    artists.append({
                        'folder_name': folder_name,
                        'artist_name': artist_name,
                        'path': full_path
                    })
            print(f"✓ Найдено {len(artists)} авторов в Nextcloud")
        except Exception as e:
            print(f"✗ Ошибка сканирования Nextcloud: {e}")
        return artists

    def upload_file(self, local_path: str, remote_path: str) -> bool:
        try:
            dest_dir = remote_path.rsplit('/', 1)[0] if '/' in remote_path else ''
            if dest_dir:
                try:
                    self.client.mkdir(dest_dir)
                except Exception:
                    pass  # найчастіше "вже існує" — не критично
            self.client.upload_sync(remote_path=remote_path, local_path=local_path)
            return True
        except Exception as e:
            print(f"✗ Ошибка загрузки {remote_path}: {e}")
            return False

    def download_file(self, remote_path: str, local_path: str):
        self.client.download_sync(
            remote_path=remote_path,
            local_path=local_path
        )

    def move_file(self, remote_path_from: str, remote_path_to: str) -> bool:
        """Переносить файл на самому Nextcloud (без завантаження туди-сюди).
        Використовується наприклад коли синхронізація виявляє, що файл
        належить до e621 pool і його треба перенести у підпапку.
        WebDAV MOVE вимагає щоб цільова папка вже існувала — тому спершу
        завжди пробуємо створити її (без попередньої перевірки через check(),
        яка іноді бреше) і просто ігноруємо помилку якщо вона вже є."""
        try:
            dest_dir = remote_path_to.rsplit('/', 1)[0] if '/' in remote_path_to else ''
            if dest_dir:
                try:
                    self.client.mkdir(dest_dir)
                    print(f"    📁 створено папку: {dest_dir}")
                except Exception as e:
                    # Найчастіше це "вже існує" — не критично, просто фіксуємо
                    print(f"    ℹ mkdir({dest_dir}): {e}")

            self.client.move(remote_path_from=remote_path_from, remote_path_to=remote_path_to)
            return True
        except Exception as e:
            print(f"    ✗ Не вдалося перенести {remote_path_from} → {remote_path_to}: {e}")
            return False

    def list_files(self, folder: str):
        return self.client.list(folder)

    def read_bytes(self, remote_path: str) -> bytes | None:
        """
        Читає вміст файлу. Спершу пробує прямий доступ до диска Nextcloud
        (якщо налаштований local_data_path — той самий сервер, без мережі,
        набагато швидше). Якщо не вийшло (не налаштовано, файла нема саме
        там, зовнішнє сховище тощо) — тихо падає на завантаження через
        WebDAV, як і раніше.
        Використовується синхронізацією для обчислення MD5/pHash/розмірів.
        """
        if self.local_data_path and self.nc_username:
            # Стандартна структура даних Nextcloud (local storage backend):
            # {datadirectory}/{username}/files/{шлях_відносно_кореня_юзера}
            local_path = os.path.join(self.local_data_path, self.nc_username, 'files', remote_path)
            try:
                with open(local_path, 'rb') as f:
                    self._direct_ok += 1
                    return f.read()
            except Exception as e:
                self._direct_fail += 1
                if self._direct_fail <= 3:  # не спамимо лог на кожен файл
                    print(f"    ⚠ Пряме читання не вдалося ({local_path}): {e} — падаю на WebDAV")
                elif self._direct_fail == 4:
                    print(f"    ⚠ (далі такі помилки більше не показую, щоб не засмічувати лог)")

        import tempfile
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = tmp.name
            self.client.download_sync(remote_path=remote_path, local_path=tmp_path)
            with open(tmp_path, 'rb') as f:
                return f.read()
        except Exception as e:
            print(f"    ⚠ Не вдалося скачати {remote_path}: {e}")
            return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

# ─────────────────────────────────────────────────────────────────────────────
# ЛОКАЛЬНЕ СХОВИЩЕ (без Nextcloud)
# ─────────────────────────────────────────────────────────────────────────────

class LocalStorage:
    """
    Альтернатива NextcloudScanner — зберігає файли прямо в папку на диску
    сервера, без Nextcloud/WebDAV. Реалізує той самий інтерфейс
    (scan_artists, upload_file, download_file, list_files), тому решта коду
    (run(), _check_and_upload тощо) працює однаково незалежно від того,
    яке сховище обрав користувач.
    """
    def __init__(self, config: Config):
        self.config = config
        self.root = (config.get('local', 'path') or '').rstrip('/')

    def scan_artists(self) -> list:
        artists = []
        if not self.root or not os.path.isdir(self.root):
            print(f"✗ Локальна папка не знайдена: {self.root}")
            return artists
        try:
            for entry in sorted(os.listdir(self.root)):
                full = os.path.join(self.root, entry)
                if not os.path.isdir(full):
                    continue
                if entry.startswith('@'):
                    artists.append({
                        'folder_name': entry,
                        'artist_name': entry[1:],
                        'path': entry + '/'  # відносний шлях, узгоджено з NextcloudScanner
                    })
            print(f"✓ Найдено {len(artists)} авторов локально")
        except Exception as e:
            print(f"✗ Ошибка сканирования локальной папки: {e}")
        return artists

    def upload_file(self, local_path: str, remote_path: str) -> bool:
        """'Заливка' файлу — насправді просто копіювання у цільову папку на диску."""
        try:
            dest = os.path.join(self.root, remote_path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(local_path, dest)
            return True
        except Exception as e:
            print(f"✗ Ошибка копирования {remote_path}: {e}")
            return False

    def download_file(self, remote_path: str, local_path: str):
        src = os.path.join(self.root, remote_path)
        os.makedirs(os.path.dirname(local_path) or '.', exist_ok=True)
        shutil.copyfile(src, local_path)

    def move_file(self, remote_path_from: str, remote_path_to: str) -> bool:
        """Переносить файл в межах локального сховища (наприклад — у
        підпапку e621 pool, коли синхронізація виявляє належність)."""
        try:
            src = os.path.join(self.root, remote_path_from)
            dst = os.path.join(self.root, remote_path_to)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            return True
        except Exception as e:
            print(f"    ✗ Не вдалося перенести {remote_path_from} → {remote_path_to}: {e}")
            return False

    def read_bytes(self, remote_path: str) -> bytes | None:
        """Читає файл прямо з диска. Використовується синхронізацією
        для обчислення MD5/pHash/розмірів."""
        try:
            with open(os.path.join(self.root, remote_path), 'rb') as f:
                return f.read()
        except Exception as e:
            print(f"    ⚠ Не вдалося прочитати {remote_path}: {e}")
            return None

    def list_files(self, folder: str):
        full = os.path.join(self.root, folder)
        if not os.path.isdir(full):
            return []
        return [f"{folder.rstrip('/')}/{f}" for f in os.listdir(full)]

# ─────────────────────────────────────────────────────────────────────────────
# E621
# ─────────────────────────────────────────────────────────────────────────────

class E621Downloader:
    def __init__(self, config: Config, username_override: str | None = None, api_key_override: str | None = None):
        self.config = config
        self.session = requests.Session()
        self.session.headers['User-Agent'] = E621_USER_AGENT
        self.last_request = 0
        self._pool_name_cache = {}
        username = username_override or config.get('e621', 'username')
        api_key  = api_key_override or config.get('e621', 'api_key')
        if username and api_key:
            self.session.auth = (username, api_key)
            print(f"  ✓ E621: авторизован как {username}")
        else:
            print(f"  ⚠ E621: без авторизации")

    def get_favorites(self, limit: int = 75, page: int = 1) -> list:
        """Улюблене АВТОРИЗОВАНОГО користувача e621 (те саме, що на
        https://e621.net/favorites) — напряму з їхнього API, не локальна копія."""
        self._rate_limit()
        try:
            r = self.session.get(f"{E621_API_BASE}/favorites.json", params={'limit': limit, 'page': page})
            r.raise_for_status()
            return r.json().get('posts', [])
        except Exception as e:
            print(f"  ✗ E621 улюблене: {e}")
            return []

    def add_favorite(self, post_id: int) -> bool:
        self._rate_limit()
        try:
            r = self.session.post(f"{E621_API_BASE}/favorites.json", data={'post_id': post_id})
            r.raise_for_status()
            return True
        except Exception as e:
            print(f"  ✗ E621 додати в улюблене: {e}")
            return False

    def remove_favorite(self, post_id: int) -> bool:
        self._rate_limit()
        try:
            r = self.session.delete(f"{E621_API_BASE}/favorites/{post_id}.json")
            r.raise_for_status()
            return True
        except Exception as e:
            print(f"  ✗ E621 прибрати з улюбленого: {e}")
            return False

    def _rate_limit(self):
        elapsed = time.time() - self.last_request
        if elapsed < E621_RATE_LIMIT:
            time.sleep(E621_RATE_LIMIT - elapsed)
        self.last_request = time.time()

    def search_posts(self, tags_query: str, limit: int = 60, page: int = 1) -> list:
        """
        Пошук постів за довільним рядком тегів e621 (той самий синтаксис, що й
        на сайті: 'artist:name rating:safe order:score' тощо). На відміну від
        search_artist_posts — це для "живого" перегляду (одна сторінка за раз),
        не для повного скачування всієї колекції автора.
        """
        self._rate_limit()
        try:
            r = self.session.get(f"{E621_API_BASE}/posts.json",
                                 params={'tags': tags_query, 'limit': limit, 'page': page})
            r.raise_for_status()
            return r.json().get('posts', [])
        except Exception as e:
            print(f"  ✗ E621 live-пошук: {e}")
            return []

    def search_artist_posts(self, artist_name: str, limit=None) -> list:
        all_posts = []
        before_id = None
        page = 1
        per_page = 320
        while True:
            if limit and len(all_posts) >= limit:
                break
            self._rate_limit()
            params = {'tags': artist_name, 'limit': per_page}
            if before_id:
                params['page'] = f'b{before_id}'
            try:
                r = self.session.get(f"{E621_API_BASE}/posts.json", params=params)
                r.raise_for_status()
                posts = r.json().get('posts', [])
                if not posts:
                    break
                all_posts.extend(posts)
                before_id = posts[-1]['id']
                print(f"  → E621: стр.{page}, +{len(posts)} (всего {len(all_posts)})")
                page += 1
                if len(posts) < per_page:
                    break
            except Exception as e:
                print(f"  ✗ E621 ошибка стр.{page}: {e}")
                break
        if limit:
            all_posts = all_posts[:limit]
        print(f"  → E621: итого {len(all_posts)} постов для {artist_name}")
        return all_posts

    def _extract_e621_tags(self, post: dict) -> dict:
        """Витягує теги з поста e621 по категоріях — character, species, general."""
        tags_obj = post.get('tags', {})
        return {
            'character': tags_obj.get('character', []),
            'species':   tags_obj.get('species',   []),
            'general':   tags_obj.get('general',   []),
        }

    def _get_pool_info(self, post: dict) -> tuple:
        """
        Якщо пост належить до pool (серії/коміксу) на e621 — повертає
        (безпечна_назва_pool, порядковий_номер_у_pool).

        Назва йде в підпапку: @автор/НазваPool/файл.jpg
        Порядковий номер — це позиція поста у ВПОРЯДКОВАНОМУ списку post_ids,
        який e621 повертає разом з даними pool. Використовується щоб у
        вкладці "Арти" при фільтрі по pool файли йшли по черзі (як сторінки
        коміксу), а не в випадковому порядку.

        Якщо пост належить одразу до кількох pool — береться перший.
        Дані кешуються (id → {name, post_ids}), щоб не робити зайвий запит
        на кожен пост із того самого pool.
        """
        pool_ids = post.get('pools') or []
        if not pool_ids:
            return None, None
        pool_id = pool_ids[0]

        if pool_id not in self._pool_name_cache:
            self._rate_limit()
            try:
                r = self.session.get(f"{E621_API_BASE}/pools/{pool_id}.json")
                r.raise_for_status()
                data = r.json()
                name = data.get('name', '') or f"pool_{pool_id}"
                post_ids = data.get('post_ids', []) or []
            except Exception as e:
                print(f"    ⚠ Не вдалося отримати дані pool {pool_id}: {e}")
                name, post_ids = f"pool_{pool_id}", []

            # e621 зберігає пробіли в назвах pool як підкреслення — повертаємо назад
            name = name.replace('_', ' ').strip()
            # Прибираємо символи, неприпустимі у шляхах файлової системи
            safe = re.sub(r'[\\/:*?"<>|]', '', name).strip()
            safe = safe[:100] or f"pool_{pool_id}"

            self._pool_name_cache[pool_id] = {'name': safe, 'post_ids': post_ids}

        info  = self._pool_name_cache[pool_id]
        order = None
        if post.get('id') in info['post_ids']:
            order = info['post_ids'].index(post['id']) + 1  # 1-based, зручніше для людини
        return info['name'], order

    def get_pool_posts(self, pool_id: int) -> tuple:
        """
        Для вкладки 'Онлайн': повертає (назва_pool, [пости_у_правильному_порядку])
        для конкретного pool — щоб можна було відкрити весь комікс/серію одразу.
        """
        if pool_id not in self._pool_name_cache:
            self._rate_limit()
            try:
                r = self.session.get(f"{E621_API_BASE}/pools/{pool_id}.json")
                r.raise_for_status()
                data = r.json()
                name = data.get('name', '') or f"pool_{pool_id}"
                post_ids = data.get('post_ids', []) or []
            except Exception as e:
                print(f"  ✗ Не вдалося отримати pool {pool_id}: {e}")
                return None, []
            name = name.replace('_', ' ').strip()
            safe = re.sub(r'[\\/:*?"<>|]', '', name).strip()[:100] or f"pool_{pool_id}"
            self._pool_name_cache[pool_id] = {'name': safe, 'post_ids': post_ids}

        info = self._pool_name_cache[pool_id]
        if not info['post_ids']:
            return info['name'], []

        self._rate_limit()
        try:
            r = self.session.get(f"{E621_API_BASE}/posts.json",
                                 params={'tags': f'pool:{pool_id}', 'limit': 320})
            r.raise_for_status()
            posts = r.json().get('posts', [])
        except Exception as e:
            print(f"  ✗ Не вдалося отримати пости pool {pool_id}: {e}")
            return info['name'], []

        by_id = {p['id']: p for p in posts}
        ordered = [by_id[pid] for pid in info['post_ids'] if pid in by_id]
        return info['name'], ordered

    def download_post(self, post: dict, local_dir: str) -> dict | None:
        self._rate_limit()
        file_info = post.get('file', {})
        file_url  = file_info.get('url')
        file_ext  = file_info.get('ext', 'jpg').lower()
        if not file_url:
            return None
        allowed = self.config.get('download', 'file_formats') or ["png", "jpg", "jpeg", "gif", "webp", "webm", "mp4", "swf"]
        if file_ext not in allowed:
            return None
        try:
            r = self.session.get(file_url)
            r.raise_for_status()
            content   = r.content
            md5_hash  = hashlib.md5(content).hexdigest()
            phash_val = compute_phash(content, file_ext)
            width, height = get_media_dimensions(content, file_ext)
            filename    = f"e621_{post['id']}.{file_ext}"
            filepath    = os.path.join(local_dir, filename)
            pool_folder, pool_order = self._get_pool_info(post)
            with open(filepath, 'wb') as f:
                f.write(content)
            return {
                'post_id':     f"e621_{post['id']}",
                'source':      'e621',
                'post_url':    f"https://e621.net/posts/{post['id']}",
                'md5_hash':    md5_hash,
                'phash':       phash_val,
                'width':       width,
                'height':      height,
                'file_path':   filepath,
                'filename':    filename,
                'file_ext':    file_ext,
                'page_num':    1,
                'pool_folder': pool_folder,
                'pool_order':  pool_order,
                'tags':        self._extract_e621_tags(post),
            }
        except Exception as e:
            print(f"    ✗ Ошибка скачивания e621 {post['id']}: {e}")
            return None


# ─────────────────────────────────────────────────────────────────────────────
# INKBUNNY
# ─────────────────────────────────────────────────────────────────────────────

class InkbunnyDownloader:
    def __init__(self, config: Config, username_override: str | None = None, password_override: str | None = None):
        self.config = config
        self.session = requests.Session()
        self.last_request = 0
        self.sid = None
        self._username_override = username_override
        self._password_override = password_override
        self._pool_order_cache = {}  # pool_id -> впорядкований список submission_id
        self._login()

    def _rate_limit(self):
        elapsed = time.time() - self.last_request
        if elapsed < INKBUNNY_RATE_LIMIT:
            time.sleep(INKBUNNY_RATE_LIMIT - elapsed)
        self.last_request = time.time()

    def _login(self):
        self._rate_limit()
        username = self._username_override or self.config.get('inkbunny', 'username')
        password = self._password_override or self.config.get('inkbunny', 'password')
        if not username or not password:
            print("  ⚠ Inkbunny: не указаны credentials")
            return
        try:
            r = self.session.post(f"{INKBUNNY_API_BASE}_login.php",
                                  data={'username': username, 'password': password})
            r.raise_for_status()
            data = r.json()
            if 'sid' in data:
                self.sid = data['sid']
                print(f"  ✓ Inkbunny: авторизован как {username}")
            else:
                print(f"  ✗ Inkbunny: {data.get('error_message', 'ошибка')}")
        except Exception as e:
            print(f"  ✗ Inkbunny: {e}")

    def search_by_keywords(self, query: str, limit: int = 60, page: int = 1, orderby: str = 'create_datetime') -> list:
        """
        Inkbunny не має структурованого тег-пошуку як e621 (де можна
        'artist:x species:y rating:safe'). Це найближчий робочий аналог —
        повнотекстовий пошук по title/keywords через їхнє API. Параметри
        підібрані за реальним робочим запитом з їхнього ж сайту (пошук
        через звичайний UI), не з документації — раніше бракувало
        stringtype/title/description, через що пошук фактично не працював.

        Якщо query порожній — замість помилки показує останні завантаження
        на Inkbunny (сортування по даті створення), як дефолтний перегляд —
        аналог order:score у e621, коли нічого не вказано.

        Може повертати менш точні результати ніж e621, це очікувано.
        """
        if not self.sid:
            return []
        self._rate_limit()
        try:
            data_params = {
                'sid':          self.sid,
                'stringtype':   'and',
                'keywords':     'yes',
                'title':        'yes',
                'description':  'no',
                'submissions_per_page': limit,
                'page':         str(page),
                'get_rid':      'yes',
                'show_files':   'yes',
                'orderby':      orderby,
            }
            if query:
                data_params['text'] = query
            r = self.session.post(f"{INKBUNNY_API_BASE}_search.php", data=data_params)
            r.raise_for_status()
            data = r.json()
            if data.get('error_code'):
                print(f"  ✗ Inkbunny пошук: {data.get('error_message', data)}")
                return []
            return (data.get('submissions', []) or [])[:limit]
        except Exception as e:
            print(f"  ✗ Inkbunny пошук за ключовими словами: {e}")
            return []

    def _resolve_username_to_id(self, username: str) -> tuple:
        """Повертає (user_id, справжній_нік) для юзернейма — Inkbunny не має
        прямого 'username → id' виклику, тому просимо 1 сабмішн цього
        юзера, звідки й дізнаємось user_id."""
        self._rate_limit()
        try:
            r = self.session.post(f"{INKBUNNY_API_BASE}_search.php", data={
                'sid': self.sid, 'username': username,
                'submissions_per_page': 1, 'page': '1',
                'tag_filter': 'no', 'get_rid': 'yes', 'show_files': 'yes'
            })
            r.raise_for_status()
            subs = r.json().get('submissions', [])
            if not subs:
                return None, None
            return subs[0].get('user_id'), subs[0].get('username')
        except Exception as e:
            print(f"  ✗ Inkbunny резолв юзернейму {username}: {e}")
            return None, None

    def get_favorites_of_user(self, username: str, limit: int = 60, page: int = 1) -> list:
        """Улюблене КОНКРЕТНОГО (будь-якого) юзера Inkbunny — публічна
        інформація, доступна через їхній сайт (Search favorites by).
        На відміну від власного улюбленого (яке в нас локальна таблиця,
        бо немає впевненого способу керувати ним через API), це лише
        ЧИТАННЯ чужого — офіційно підтримана можливість пошуку."""
        if not self.sid:
            return []
        user_id, _ = self._resolve_username_to_id(username)
        if not user_id:
            print(f"  → Inkbunny: користувача {username} не знайдено")
            return []
        self._rate_limit()
        try:
            r = self.session.post(f"{INKBUNNY_API_BASE}_search.php", data={
                'sid': self.sid, 'favs_user_id': user_id,
                'submissions_per_page': limit, 'page': str(page),
                'get_rid': 'yes', 'show_files': 'yes'
            })
            r.raise_for_status()
            return (r.json().get('submissions', []) or [])[:limit]
        except Exception as e:
            print(f"  ✗ Inkbunny улюблене {username}: {e}")
            return []

    def search_artist_submissions(self, artist_name: str, limit=None) -> list:
        if not self.sid:
            return []
        print(f"  → Inkbunny: поиск пользователя {artist_name}...")
        self._rate_limit()
        try:
            r = self.session.post(f"{INKBUNNY_API_BASE}_search.php", data={
                'sid': self.sid, 'username': artist_name,
                'submissions_per_page': 1, 'page': '1',
                'tag_filter': 'no', 'get_rid': 'yes', 'show_files': 'yes'
            })
            r.raise_for_status()
            subs = r.json().get('submissions', [])
            if not subs:
                print(f"  → Inkbunny: пользователь {artist_name} не найден")
                return []
            user_id = subs[0].get('user_id')
            actual  = subs[0].get('username')
            print(f"  → Inkbunny: найден '{actual}' (user_id={user_id})")
        except Exception as e:
            print(f"  ✗ Inkbunny поиск: {e}")
            return []

        all_subs = []
        seen_ids = set()
        page = 1
        per_page = 100
        while True:
            self._rate_limit()
            try:
                r = self.session.post(f"{INKBUNNY_API_BASE}_search.php", data={
                    'sid': self.sid, 'user_id': user_id,
                    'submissions_per_page': per_page, 'page': str(page),
                    'tag_filter': 'no', 'get_rid': 'yes', 'show_files': 'yes'
                })
                r.raise_for_status()
                page_subs = r.json().get('submissions', [])
                if not page_subs:
                    break
                new_subs = []
                dups = 0
                for s in page_subs:
                    sid = s.get('submission_id')
                    if sid not in seen_ids:
                        seen_ids.add(sid)
                        new_subs.append(s)
                    else:
                        dups += 1
                if dups == len(page_subs):
                    break
                all_subs.extend(new_subs)
                print(f"  → Inkbunny: стр.{page}, +{len(new_subs)} (всего {len(all_subs)})")
                if len(page_subs) < per_page or not new_subs:
                    break
                if limit and len(all_subs) >= limit:
                    all_subs = all_subs[:limit]
                    break
                page += 1
            except Exception as e:
                print(f"  ✗ Inkbunny стр.{page}: {e}")
                break
        print(f"  → Inkbunny: итого {len(all_subs)} работ для {artist_name}")
        return all_subs

    def _get_pool_info(self, sub_id, pools_list: list) -> tuple:
        """
        Повертає (безпечна_назва_pool, порядковий_номер) для першого pool
        з масиву submission['pools'] (якщо є). Сам API деталей поста не
        віддає порядковий номер напряму — тому робимо окремий пошук по
        pool_id з orderby=pool_order (один раз на pool, кешується), і
        визначаємо позицію конкретного поста в цьому впорядкованому списку.
        """
        if not pools_list:
            return None, None
        pool_id   = pools_list[0].get('pool_id')
        pool_name = pools_list[0].get('name', '') or f"pool_{pool_id}"
        safe_name = re.sub(r'[\\/:*?"<>|]', '', pool_name).strip()[:100] or f"pool_{pool_id}"

        if pool_id not in self._pool_order_cache:
            self._rate_limit()
            try:
                r = self.session.post(f"{INKBUNNY_API_BASE}_search.php", data={
                    'sid': self.sid, 'pool_id': pool_id, 'orderby': 'pool_order',
                    'submissions_per_page': 100, 'get_rid': 'yes'
                })
                r.raise_for_status()
                subs = r.json().get('submissions', []) or []
                ordered_ids = [str(s.get('submission_id')) for s in subs]
            except Exception as e:
                print(f"  ✗ Inkbunny pool {pool_id}: {e}")
                ordered_ids = []
            self._pool_order_cache[pool_id] = ordered_ids

        ordered_ids = self._pool_order_cache[pool_id]
        order = None
        sid_str = str(sub_id)
        if sid_str in ordered_ids:
            order = ordered_ids.index(sid_str) + 1  # 1-based, зручніше для людини
        return safe_name, order

    def get_pool_posts(self, pool_id: int) -> tuple:
        """
        Для 'Онлайн': повертає (назва_pool, [пости_у_правильному_порядку])
        для конкретного pool — той самий аналог, що вже є для e621.
        """
        if pool_id not in self._pool_order_cache:
            self._rate_limit()
            try:
                r = self.session.post(f"{INKBUNNY_API_BASE}_search.php", data={
                    'sid': self.sid, 'pool_id': pool_id, 'orderby': 'pool_order',
                    'submissions_per_page': 100, 'get_rid': 'yes'
                })
                r.raise_for_status()
                subs = r.json().get('submissions', []) or []
                ordered_ids = [str(s.get('submission_id')) for s in subs]
            except Exception as e:
                print(f"  ✗ Inkbunny pool {pool_id}: {e}")
                return None, []
            self._pool_order_cache[pool_id] = ordered_ids

        ordered_ids = self._pool_order_cache[pool_id]
        if not ordered_ids:
            return None, []

        # Тягнемо повні деталі (file_url/thumb_url тощо) одним пакетним
        # запитом на всі пости pool одразу, з show_pools=yes щоб одразу
        # знати назву pool з відповіді
        self._rate_limit()
        pool_name = None
        full_by_id = {}
        for i in range(0, len(ordered_ids), 100):
            chunk = ordered_ids[i:i + 100]
            try:
                r = self.session.post(f"{INKBUNNY_API_BASE}_submissions.php", data={
                    'sid': self.sid, 'submission_ids': ','.join(chunk),
                    'show_description': 'no', 'show_pools': 'yes'
                })
                r.raise_for_status()
                for full in r.json().get('submissions', []):
                    full_by_id[str(full.get('submission_id'))] = full
                    if pool_name is None:
                        for p in (full.get('pools') or []):
                            if str(p.get('pool_id')) == str(pool_id):
                                pool_name = p.get('name')
            except Exception as e:
                print(f"  ✗ Inkbunny деталі pool {pool_id}: {e}")

        ordered = [full_by_id[sid] for sid in ordered_ids if sid in full_by_id]
        return pool_name, ordered

    def download_submission(self, submission: dict, local_dir: str) -> dict | None:
        self._rate_limit()
        sub_id = submission['submission_id']
        try:
            r = self.session.post(f"{INKBUNNY_API_BASE}_submissions.php", data={
                'sid': self.sid, 'submission_ids': sub_id, 'show_description': 'no', 'show_pools': 'yes'
            })
            r.raise_for_status()
            subs_list = r.json().get('submissions', [])
            if subs_list:
                submission = subs_list[0]
        except Exception as e:
            print(f"    ⚠ Детали submission {sub_id}: {e}")

        pool_folder, pool_order = self._get_pool_info(sub_id, submission.get('pools') or [])

        files = submission.get('files', [])
        if not files:
            return None

        allowed = self.config.get('download', 'file_formats') or ["png", "jpg", "jpeg", "gif", "webp", "webm", "mp4", "swf"]
        downloaded = []
        for page_num, fi in enumerate(files, start=1):
            file_url = fi.get('file_url_full') or fi.get('file_url_screen')
            if not file_url:
                continue
            file_ext = file_url.split('.')[-1].lower()
            if file_ext not in allowed:
                continue
            try:
                r = self.session.get(file_url)
                r.raise_for_status()
                content   = r.content
                md5_hash  = hashlib.md5(content).hexdigest()
                phash_val = compute_phash(content, file_ext)
                width, height = get_media_dimensions(content, file_ext)
                filename  = (f"ib_{sub_id}-p{page_num}.{file_ext}"
                             if len(files) > 1 else f"ib_{sub_id}.{file_ext}")
                filepath  = os.path.join(local_dir, filename)
                with open(filepath, 'wb') as f:
                    f.write(content)
                downloaded.append({
                    'md5_hash':  md5_hash,
                    'phash':     phash_val,
                    'width':     width,
                    'height':    height,
                    'file_path': filepath,
                    'filename':  filename,
                    'file_ext':  file_ext,
                    'page_num':  page_num,
                    'pool_folder': pool_folder,
                    'pool_order':  pool_order,
                })
            except Exception as e:
                print(f"    ✗ Inkbunny {sub_id} стр.{page_num}: {e}")
        if not downloaded:
            return None
        # Теги Inkbunny — поле keywords
        ib_tags = []
        for kw in submission.get('keywords', []):
            name = kw.get('keyword_name', '').strip()
            if name:
                ib_tags.append(name)

        return {
            'post_id':      f"ib_{sub_id}",
            'source':       'inkbunny',
            'post_url':     f"https://inkbunny.net/s/{sub_id}",
            'files':        downloaded,
            'is_multipage': len(downloaded) > 1,
            'tags':         ib_tags,
        }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ARCHIVER
# ─────────────────────────────────────────────────────────────────────────────
def parse_filename(filename: str):
    """
    Возвращает:
        post_id
        page_num
        extension
    """

    name = Path(filename).name

    m = re.match(r"^([a-zA-Z0-9]+_\d+)\.(\w+)$", name)
    if m:
        return m.group(1), 1, m.group(2).lower()

    m = re.match(r"^([a-zA-Z0-9]+_\d+)-p(\d+)\.(\w+)$", name)
    if m:
        return m.group(1), int(m.group(2)), m.group(3).lower()

    if m:
        return m.group(1), 1, m.group(2).lower()

    return None


def _sync_parse(filename: str, subfolder: str | None = None):
    """
    Для довільного файлу (не обов'язково завантаженого архіватором) повертає
    (post_id, page_num, file_ext, source, post_url).

    Якщо ім'я відповідає звичному шаблону e621_XXXX(.ext) / ib_XXXX(-pN).ext —
    використовує розпізнаний post_id і джерело як є (щоб зберегти сумісність
    з іншими функціями — Cleanup, Міграція тегів тощо), і будує посилання на
    оригінал за тим самим шаблоном, що й звичайне завантаження — числовий id
    у назві файлу однозначно відповідає id поста на e621/Inkbunny.

    Якщо файл був покладений вручну і шаблону не відповідає — генерує
    синтетичний post_id з самого імені файлу, а джерело і посилання
    лишаються невідомими (None → NULL у БД), щоб не показувати хибні дані
    там, де насправді невідомо звідки файл. Адмін може потім проставити
    джерело/посилання/автора вручну через редагування файлу.
    """
    parsed = parse_filename(filename)
    if parsed:
        post_id, page_num, ext = parsed
        source = 'inkbunny' if post_id.startswith('ib_') else 'e621'
        numeric_id = post_id.split('_', 1)[1] if '_' in post_id else None
        if numeric_id and numeric_id.isdigit():
            post_url = (f"https://e621.net/posts/{numeric_id}" if source == 'e621'
                        else f"https://inkbunny.net/s/{numeric_id}")
        else:
            post_url = None
        return post_id, page_num, ext, source, post_url

    ext  = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    base = filename.rsplit('.', 1)[0] if '.' in filename else filename
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', base)[:55]
    # Якщо файл лежить у pool — додаємо назву pool у синтетичний ID,
    # інакше "000.png" з РІЗНИХ pool одного автора генерували б ОДНАКОВИЙ
    # ID (тільки з імені файлу) і конфліктували між собою — другий, третій
    # і т.д. pool з такими самими "000.png/001.png..." просто тихо
    # відкидались через UNIQUE-обмеження (artist_id, post_id, page_num).
    if subfolder:
        safe_sub = re.sub(r'[^a-zA-Z0-9_-]', '_', subfolder)[:40]
        post_id = f"sync_{safe_sub}_{safe}"
    else:
        post_id = f"sync_{safe}"
    return post_id, 1, ext, None, None

class FurryArchiver:
    def __init__(self, user_id: int = 1):
        self.user_id     = user_id
        self.config      = Config(user_id=user_id)
        self.db          = Database(self.config)
        self.tracker     = DatabaseTracker(self.db, self.config)

        nc_cfg     = self.config.config.get('nextcloud', {}) or {}
        nc_enabled = nc_cfg.get('enabled', bool(nc_cfg.get('webdav_url')))

        if nc_enabled and nc_cfg.get('webdav_url'):
            self.nc_scanner   = NextcloudScanner(self.config)
            self.storage_mode = 'nextcloud'
            print("  Сховище: Nextcloud")
        elif self.config.get('local', 'path'):
            self.nc_scanner   = LocalStorage(self.config)
            self.storage_mode = 'local'
            print(f"  Сховище: локальна папка ({self.config.get('local', 'path')})")
        else:
            self.nc_scanner   = NextcloudScanner(self.config)
            self.storage_mode = 'nextcloud'
            print("  ⚠ Сховище не налаштоване (ні Nextcloud, ні локальна папка)")

        self.e621_dl = None
        # Папки, які юзер явно позначив "не сканувати" — напр. особисті
        # файли, що лежать у тій самій кореневій папці, що й архів
        self.excluded_folders = set(self.config.config.get('scan_exclude', {}).get('folders', []) or [])
        if self.excluded_folders:
            print(f"  Виключено з синхронізації: {', '.join(self.excluded_folders)}")
        e621_enabled = self.config.get('e621', 'enabled')
        print(f"  E621 enabled: {e621_enabled!r}")
        if e621_enabled:
            self.e621_dl = E621Downloader(self.config)

        self.ib_dl = None
        ib_enabled = self.config.get('inkbunny', 'enabled')
        print(f"  Inkbunny enabled: {ib_enabled!r}")
        if ib_enabled:
            self.ib_dl = InkbunnyDownloader(self.config)

        tmp = self.config.get('download', 'local_temp_dir')
        print(f"  Temp dir: {tmp!r}")

    def _get_nc_db(self):
        """Підключення до БД Nextcloud (read-only) — для швидкого масового
        порівняння замість повільної перевірки кожного файлу через WebDAV."""
        cfg = self.config.config.get("nextcloud_db", {})
        if not cfg.get("host") or not cfg.get("user") or not cfg.get("database"):
            return None
        return pymysql.connect(
            host=cfg.get("host", "localhost"),
            port=int(cfg.get("port", 3306)),
            user=cfg.get("user", ""),
            password=cfg.get("password", ""),
            database=cfg.get("database", ""),
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
        )


    def _check_and_upload(self, artist_id: int, artist_path: str,
                           result_file: dict, post_id: str, source: str,
                           post_url: str, dry_run: bool,
                           check_duplicates: bool,
                           tags: list | None = None) -> str:
        """
        Проверяет дубликаты (MD5 + pHash) и загружает файл в Nextcloud.
        Возвращает: 'uploaded' | 'md5_dup' | 'phash_dup' | 'upload_error'
        """
        md5    = result_file['md5_hash']
        phash  = result_file.get('phash')
        fname  = result_file['filename']
        fext   = result_file['file_ext']
        fpath  = result_file['file_path']
        pnum   = result_file.get('page_num', 1)
        width  = result_file.get('width')
        height = result_file.get('height')
        try:
            size_bytes = os.path.getsize(fpath)
        except OSError:
            size_bytes = None
        # Якщо пост належить до pool на e621 — файл кладеться в підпапку з
        # назвою pool всередині папки автора: @автор/НазваPool/файл.jpg
        pool_folder = result_file.get('pool_folder')
        pool_order  = result_file.get('pool_order')
        subfolder_prefix = f"{pool_folder}/" if pool_folder else ""

        if check_duplicates:
            # 1. MD5 — точный дубликат
            if self.tracker.is_md5_duplicate(artist_id, md5):
                os.remove(fpath)
                return 'md5_dup'

            # 1b. Раніше відхилений як дублікат — пропускаємо
            if self.tracker.is_rejected(artist_id, post_id, md5):
                os.remove(fpath)
                return 'rejected'

            # 2. pHash — визуально похожий → на ручную проверку
            if phash is not None:
                dup = self.tracker.find_phash_duplicate(artist_id, phash, fext)
                if dup:
                    matched_post_id, matched_filename, hamming_dist = dup[0], dup[1], dup[2]
                    matched_artist_id     = dup[3] if len(dup) > 3 else artist_id
                    matched_folder_actual = dup[4] if len(dup) > 4 else ''
                    os.makedirs(PENDING_DIR, exist_ok=True)
                    pending_path = os.path.join(PENDING_DIR, fname)
                    os.replace(fpath, pending_path)
                    self.tracker.add_pending(
                        artist_id=artist_id, post_id=post_id, source=source,
                        post_url=post_url, md5_hash=md5, phash_val=phash,
                        filename=fname, file_ext=fext, page_num=pnum,
                        local_path=pending_path,
                        nc_target_path=f"{artist_path}{subfolder_prefix}{fname}",
                        matched_post_id=matched_post_id,
                        matched_filename=matched_filename,
                        hamming_distance=hamming_dist,
                        matched_folder_name=matched_folder_actual
                    )
                    return f'phash_pending:{matched_filename}:{hamming_dist}'

        if dry_run:
            return 'dry_run'

        remote_path = f"{artist_path}{subfolder_prefix}{fname}"
        if self.nc_scanner.upload_file(fpath, remote_path):
            self.tracker.add_download(
                artist_id=artist_id,
                post_id=post_id,
                source=source,
                post_url=post_url,
                md5_hash=md5,
                filename=fname,
                file_ext=fext,
                page_num=pnum,
                phash_val=phash,
                tags=tags,
                width=width,
                height=height,
                subfolder=pool_folder,
                pool_order=pool_order,
                size_bytes=size_bytes,
            )
            os.remove(fpath)
            return 'uploaded'
        else:
            os.remove(fpath)
            return 'upload_error'

    def run(self, dry_run=False, artist_name: str | None = None, artist_names: list | None = None):
        print("\n" + "="*60)
        print("  FURRY ART ARCHIVER v2 — MariaDB + pHash дедупликация")
        print("="*60 + "\n")

        if artist_names:
            # Режим списку авторів (авто-оновлення) — той самий підхід, що й
            # для одного автора нижче, тільки для декількох одразу
            print(f"[1/3] Режим авто-оновлення: {len(artist_names)} авторів")
            artists = []
            for name in artist_names:
                folder_name = f"@{name}"
                if self.storage_mode == 'nextcloud':
                    base = self.config.get('nextcloud', 'base_folder').strip('/')
                    full_folder = f"{base}/{folder_name}"
                    try:
                        self.nc_scanner.client.mkdir(full_folder)
                    except Exception:
                        pass
                    artist_path = f"{full_folder}/"
                elif self.storage_mode == 'local':
                    os.makedirs(os.path.join(self.nc_scanner.root, folder_name), exist_ok=True)
                    artist_path = folder_name + '/'
                else:
                    artist_path = folder_name + '/'
                artists.append({'artist_name': name, 'folder_name': folder_name, 'path': artist_path})
        elif artist_name:
            # Режим одного автора — качаємо всю бібліотеку конкретного автора,
            # створюючи папку якщо її ще нема (наприклад, з вкладки "Онлайн").
            folder_name = f"@{artist_name}"
            print(f"[1/3] Режим одного автора: {artist_name}")
            if self.storage_mode == 'nextcloud':
                base = self.config.get('nextcloud', 'base_folder').strip('/')
                full_folder = f"{base}/{folder_name}"
                try:
                    self.nc_scanner.client.mkdir(full_folder)
                    print(f"  📁 створено папку: {full_folder}")
                except Exception as e:
                    print(f"  ℹ mkdir({full_folder}): {e}")
                artist_path = f"{full_folder}/"
            elif self.storage_mode == 'local':
                os.makedirs(os.path.join(self.nc_scanner.root, folder_name), exist_ok=True)
                artist_path = folder_name + '/'
            else:
                artist_path = folder_name + '/'
            artists = [{'artist_name': artist_name, 'folder_name': folder_name, 'path': artist_path}]
        else:
            print("[1/3] Сканирование Nextcloud...")
            artists = self.nc_scanner.scan_artists()
            if not artists:
                print("✗ Авторы не найдены.")
                return

        print(f"\n[2/3] Завантаження глобального pHash кешу...")
        self.tracker._load_global_phash_cache()

        print(f"\n[3/3] Обработка {len(artists)} авторов...\n")
        check_dup = self.config.get('download', 'check_duplicates')
        total_new = 0
        total_skip = 0
        total_pending = 0

        for artist in artists:
            artist_name   = artist['artist_name']
            artist_folder = artist['folder_name']
            artist_path   = artist['path']

            artist_id = self.tracker.get_or_create_artist(artist_folder, artist_name, self.user_id)
            already   = self.tracker.count_downloads(artist_id)
            print(f"→ {artist_name}  (в БД: {already} файлов)")

            temp_dir = os.path.join(
                self.config.get('download', 'local_temp_dir') or './temp_downloads',
                artist_name
            )
            os.makedirs(temp_dir, exist_ok=True)

            new_files = 0
            skipped   = 0
            pending   = 0
            autosave_counter = 0

            # ── E621 ──────────────────────────────────────────────────────────
            if self.e621_dl:
                posts = self.e621_dl.search_artist_posts(
                    artist_name, limit=self.config.get('e621', 'max_posts_per_artist')
                )
                for post in posts:
                    post_id = f"e621_{post['id']}"
                    if self.tracker.is_post_downloaded(artist_id, post_id):
                        skipped += 1
                        continue
                    if self.tracker.is_post_pending(artist_id, post_id):
                        skipped += 1
                        continue
                    if self.tracker.is_rejected(artist_id, post_id):
                        skipped += 1
                        continue
                    if dry_run:
                        print(f"    [DRY RUN] e621 {post['id']}")
                        new_files += 1
                        continue

                    result = self.e621_dl.download_post(post, temp_dir)
                    if not result:
                        continue

                    status = self._check_and_upload(
                        artist_id, artist_path, result,
                        post_id=post_id, source='e621',
                        post_url=result['post_url'],
                        dry_run=dry_run, check_duplicates=check_dup,
                        tags=result.get('tags'),
                    )

                    if status == 'uploaded':
                        print(f"    ✓ e621 {post['id']}")
                        new_files += 1
                        autosave_counter += 1
                    elif status == 'md5_dup':
                        print(f"    ⊘ MD5 дубликат e621 {post['id']}")
                        skipped += 1
                    elif status.startswith('phash_pending'):
                        _, mfile, dist = status.split(':', 2)
                        print(f"    ⏳ pHash e621 {post['id']} ≈ {mfile} (d={dist})")
                        pending += 1
                    elif status == 'rejected':
                        skipped += 1
                    else:
                        print(f"    ✗ {status} e621 {post['id']}")

                    if autosave_counter and autosave_counter % 100 == 0:
                        self.tracker.save()
                        print(f"    💾 Автосохранение: {new_files} файлов")

            # ── Inkbunny ──────────────────────────────────────────────────────
            if self.ib_dl and self.ib_dl.sid:
                submissions = self.ib_dl.search_artist_submissions(
                    artist_name, limit=self.config.get('inkbunny', 'max_submissions_per_artist')
                )
                for sub in submissions:
                    post_id = f"ib_{sub['submission_id']}"
                    if self.tracker.is_post_downloaded(artist_id, post_id):
                        skipped += 1
                        continue
                    if self.tracker.is_post_pending(artist_id, post_id):
                        skipped += 1
                        continue
                    if self.tracker.is_rejected(artist_id, post_id):
                        skipped += 1
                        continue
                    if dry_run:
                        print(f"    [DRY RUN] ib {sub['submission_id']}")
                        new_files += 1
                        continue

                    result = self.ib_dl.download_submission(sub, temp_dir)
                    if not result:
                        continue

                    uploaded_pages = 0
                    for file_data in result['files']:
                        file_data['page_num'] = file_data.get('page_num', 1)
                        status = self._check_and_upload(
                            artist_id, artist_path, file_data,
                            post_id=post_id, source='inkbunny',
                            post_url=result['post_url'],
                            dry_run=dry_run, check_duplicates=check_dup,
                            tags=result.get('tags'),
                        )
                        if status == 'uploaded':
                            uploaded_pages += 1
                            autosave_counter += 1
                        elif status == 'md5_dup':
                            print(f"    ⊘ MD5 дубл. ib {sub['submission_id']}-p{file_data['page_num']}")
                            skipped += 1
                        elif status.startswith('phash_pending'):
                            _, mfile, dist = status.split(':', 2)
                            print(f"    ⏳ pHash ib {sub['submission_id']}-p{file_data['page_num']} ≈ {mfile} (d={dist})")
                            pending += 1

                    if uploaded_pages:
                        label = f"({uploaded_pages} стр.)" if result['is_multipage'] else ""
                        print(f"    ✓ ib {sub['submission_id']} {label}")
                        new_files += uploaded_pages

                    if autosave_counter and autosave_counter % 100 == 0:
                        self.tracker.save()
                        print(f"    💾 Автосохранение: {new_files} файлов")

            if new_files > 0 or pending > 0:
                self.tracker.save()

            print(f"  Итого: +{new_files} новых, {pending} на проверку, пропущено {skipped}\n")
            total_new     += new_files
            total_skip    += skipped
            total_pending += pending

        print("=" * 60)
        print("  ЗАВЕРШЕНО")
        print("=" * 60)
        print(f"Скачано:       {total_new}")
        print(f"На проверку:   {total_pending}")
        print(f"Пропущено:     {total_skip}")

    # ── MIGRATE: перенос старых download.txt → БД ─────────────────────────────

    def migrate_from_txt(self):
        """
Читает download.txt из каждой папки автора в Nextcloud.

Переносит записи в MariaDB,
после чего скачивает существующие файлы из Nextcloud,
вычисляет MD5 и pHash,
определяет имя файла и расширение
и обновляет записи в базе данных.
"""
        tmp_dir = "./tmp_hashes"
        os.makedirs(tmp_dir, exist_ok=True)

        print("\n" + "="*60)
        print("  МИГРАЦИЯ download.txt → MariaDB")
        print("="*60 + "\n")

        if self.storage_mode != 'nextcloud':
            print("✗ Ця функція підтримує тільки Nextcloud-сховище (потрібен старий download.txt).")
            print("  Для локального сховища скористайся кнопкою «Синхронізація».")
            return

        artists = self.nc_scanner.scan_artists()
        if not artists:
            print("✗ Авторы не найдены.")
            return

        total_migrated = 0

        for artist in artists:
            folder = artist['folder_name']
            name   = artist['artist_name']
            base   = self.config.get('nextcloud', 'base_folder').strip('/')
            tracker_path = f"{base}/{folder}/download.txt"

            print(f"→ {name}...")

            try:
                if not self.nc_scanner.client.check(tracker_path):
                    print(f"  ⊘ download.txt не найден — пропускаем")
                    continue

                tmp = f"./tmp_migrate_{folder}.txt"
                self.nc_scanner.client.download_sync(
                    remote_path=tracker_path, local_path=tmp
                )

                artist_id = self.tracker.get_or_create_artist(folder, name, self.user_id)
                migrated  = 0
                migration_ok = True

                with open(tmp, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        parts = [p.strip() for p in line.split('|')]
                        if len(parts) < 2:
                            continue

                        post_id = parts[0]
                        url     = parts[1]

                        # Нормализуем старый формат (просто число → e621_XXXXX)
                        if post_id.isdigit():
                            post_id = f"e621_{post_id}"

                        # Определяем source
                        source = 'inkbunny' if post_id.startswith('ib_') else 'e621'

                        if not self.tracker.is_post_downloaded(artist_id, post_id):
                            self.tracker.add_download(
                                artist_id=artist_id,
                                post_id=post_id,
                                source=source,
                                post_url=url,
                                md5_hash=None,
                                filename=None,
                                file_ext=None,
                                page_num=1,
                                phash_val=None
                            )
                            migrated += 1

                os.remove(tmp)
                print("  → Вычисление MD5 и pHash...")

                files = self.nc_scanner.list_files(f"{base}/{folder}")

                for remote in files:

                    remote_name = os.path.basename(remote.rstrip("/"))

                    if not remote_name:
                        continue

                    remote_file = f"{base}/{folder}/{remote_name}"

                    if remote_name == "download.txt":
                        continue

                    parsed = parse_filename(remote_name)

                    if not parsed:
                        continue

                    post_id, page_num, ext = parsed

                    tmp_file = os.path.join(tmp_dir, remote_name)

                    try:

                        self.nc_scanner.download_file(remote_file, tmp_file)

                        with open(tmp_file, "rb") as f:
                            data = f.read()

                        md5 = hashlib.md5(data).hexdigest()

                        phash = compute_phash(data, ext)

                        updated = self.tracker.update_hashes(
                            artist_id,
                            post_id,
                            page_num,
                            md5,
                            phash,
                            remote_name,
                            ext
                        )

                        if updated:
                            if phash is not None:
                                self.tracker.add_to_phash_cache(
                                    artist_id,
                                    phash,
                                    post_id,
                                    remote_name
                                )
                        else:
                            print(f"     ⚠ Не найдена запись {post_id} page {page_num}")

                        print(f"     ✓ {remote_name}")



                    except Exception as e:

                        migration_ok = False

                        print(f"     ✗ {remote_name}: {e}")


                    finally:
                        if os.path.exists(tmp_file):
                            os.remove(tmp_file)
                self.tracker.save()

                # Проверяем, что миграция прошла успешно
                db_count = self.tracker.count_downloads(artist_id)

                if migration_ok and db_count >= migrated:

                    try:
                        self.nc_scanner.client.clean(tracker_path)
                        print("  ✓ download.txt удалён")

                    except Exception as e:
                        print(f"  ⚠ Не удалось удалить download.txt: {e}")

                else:
                    print("  ⚠ download.txt оставлен (миграция завершилась с ошибками)")

                print(f"  ✓ Перенесено записей: {migrated}")
                total_migrated += migrated

            except Exception as e:
                print(f"  ✗ Ошибка: {e}")
                continue

        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass

        print(f"\n{'='*60}")
        print(f"  МИГРАЦИЯ ЗАВЕРШЕНА. Всего перенесено: {total_migrated} записей")
        print(f"{'='*60}\n")

        # Автоматично скачуємо теги для щойно перенесених файлів
        print("Запускаємо міграцію тегів для перенесених файлів...")
        self.migrate_tags()

    def close(self):
        self.db.close()

    # ── CLEANUP: видаляє з БД записи яких немає на Nextcloud ─────────────────

    def backfill_file_sizes(self):
        """
        Довантажує вагу (size_bytes) для файлів, які були скачані ще ДО того,
        як зʼявилось відстежування ваги — тому в них 0/NULL і вони псують
        підсумкову цифру 'Вага' в списку авторів.
        """
        print("\n" + "="*60)
        print("  ДОВАНТАЖЕННЯ ВАГИ ФАЙЛІВ")
        print("="*60 + "\n")

        with self.db.cursor() as cur:
            cur.execute("""
                SELECT f.id, f.name AS filename, fp.subfolder, a.folder_name
                FROM files f
                LEFT JOIN furry_posts fp ON fp.file_id = f.id
                LEFT JOIN artists a      ON a.id = fp.artist_id
                WHERE f.size_bytes IS NULL OR f.size_bytes = 0
            """)
            rows = cur.fetchall()

        if not rows:
            print("Усі файли вже мають записану вагу.")
            return
        print(f"Файлів без ваги: {len(rows)}\n")

        if self.storage_mode == 'local':
            root = self.config.get('local', 'path') or ''
            updated = 0
            with self.db.cursor() as cur:
                for row in rows:
                    parts = [root, row['folder_name']]
                    if row.get('subfolder'):
                        parts.append(row['subfolder'])
                    parts.append(row['filename'])
                    fpath = os.path.join(*parts)
                    try:
                        size = os.path.getsize(fpath)
                        cur.execute("UPDATE files SET size_bytes=%s WHERE id=%s", (size, row['id']))
                        updated += 1
                    except OSError:
                        pass
            self.db.commit()
            print(f"Оновлено: {updated} з {len(rows)}")
            return

        # Nextcloud — спершу пробуємо швидкий шлях через власну БД Nextcloud
        # (той самий підхід, що вже й так є для cleanup) — один масовий
        # запит замість тисяч окремих WebDAV-звернень
        nc_conn = self._get_nc_db()
        if nc_conn:
            base = self.config.get('nextcloud', 'base_folder').strip('/')
            print("Тягну розміри файлів з Nextcloud DB (швидкий шлях)...")
            with nc_conn.cursor() as cur:
                cur.execute("SELECT path, size FROM oc_filecache WHERE path LIKE %s", (f"files/{base}/%",))
                size_by_pair = {}
                for r in cur.fetchall():
                    parts = r['path'].split('/')
                    if len(parts) >= 2:
                        parent = parts[-2]
                        if not parent.startswith('@') and len(parts) >= 3 and parts[-3].startswith('@'):
                            parent = parts[-3]  # pool-підпапка — беремо папку автора рівнем вище
                        size_by_pair[(parent, parts[-1])] = r['size']
            nc_conn.close()

            updated = 0
            with self.db.cursor() as cur:
                for row in rows:
                    size = size_by_pair.get((row['folder_name'], row['filename']))
                    if size is not None:
                        cur.execute("UPDATE files SET size_bytes=%s WHERE id=%s", (size, row['id']))
                        updated += 1
            self.db.commit()
            print(f"Оновлено: {updated} з {len(rows)}")
            if updated < len(rows):
                print(f"Не знайдено в Nextcloud DB: {len(rows)-updated} (можливо видалені файли чи розбіжність шляхів)")
            return

        # Фолбек — повільніше, по одному файлу через WebDAV
        print("БД Nextcloud не налаштована — читаю розміри через WebDAV (повільніше)...")
        base = self.config.get('nextcloud', 'base_folder').strip('/')
        updated = 0
        with self.db.cursor() as cur:
            for row in rows:
                sub = f"{row['subfolder']}/" if row.get('subfolder') else ""
                remote_path = f"{base}/{row['folder_name']}/{sub}{row['filename']}"
                try:
                    info = self.nc_scanner.client.info(remote_path)
                    size = int(info.get('size', 0) or 0)
                    if size:
                        cur.execute("UPDATE files SET size_bytes=%s WHERE id=%s", (size, row['id']))
                        updated += 1
                except Exception:
                    pass
        self.db.commit()
        print(f"Оновлено: {updated} з {len(rows)}")

    def scan_for_duplicates(self, artist_name: str | None = None, subfolder: str | None = None):
        """
        Ретроактивний пошук візуальних дублікатів у вже наявній бібліотеці —
        на відміну від звичайної перевірки (яка спрацьовує лише в момент
        нового скачування), тут звіряються файли, що вже давно лежать у
        БД. Можна обмежити конкретним автором і/або конкретним pool
        усередині нього. Знайдені пари записуються в duplicate_scan_results
        для перегляду й вирішення через дашборд — нічого не видаляється
        автоматично.
        """
        print("\n" + "="*60)
        print("  ПОШУК ДУБЛІКАТІВ У БІБЛІОТЕЦІ")
        print("="*60 + "\n")

        where = ["ph.phash IS NOT NULL"]
        args  = []
        artist_id = None
        if artist_name:
            with self.db.cursor() as cur:
                cur.execute("SELECT id FROM artists WHERE artist_name=%s OR folder_name=%s",
                            (artist_name, f"@{artist_name}"))
                row = cur.fetchone()
            if not row:
                print(f"✗ Автора '{artist_name}' не знайдено.")
                return
            artist_id = row['id']
            where.append("fp.artist_id=%s")
            args.append(artist_id)
            print(f"Обмежено автором: {artist_name} (artist_id={artist_id})")

            # Діагностика: чи є інші записи автора зі схожою назвою — типовий
            # випадок, коли роботи того самого художника фактично розкидані
            # по РІЗНИХ artist_id (недооб'єднаний акаунт), через що скан
            # ніколи їх не порівнює між собою, бо звужується лише на один ID
            with self.db.cursor() as cur:
                cur.execute("""
                    SELECT id, artist_name, folder_name FROM artists
                    WHERE id != %s AND (artist_name LIKE %s OR folder_name LIKE %s)
                """, (artist_id, f"%{artist_name}%", f"%{artist_name}%"))
                similar = cur.fetchall()
            if similar:
                print(f"⚠ УВАГА: знайдено {len(similar)} інших записів автора зі схожою назвою:")
                for s in similar:
                    print(f"    → id={s['id']}  {s['artist_name']} ({s['folder_name']})")
                print("    Якщо це той самий художник під іншим акаунтом — дублікати між ними "
                      "цей скан НЕ побачить, доки їх не об'єднати (кнопка 🔗 в 'Автори').")

        if subfolder:
            where.append("fp.subfolder=%s")
            args.append(subfolder)
            print(f"Обмежено pool: {subfolder}")
        if not artist_name:
            print("Сканується ВСЯ бібліотека — це може зайняти час на великих архівах.")

        # Скільки файлів у цій вибірці взагалі не мають pHash (і тому не
        # братимуть участі в порівнянні) — щоб було видно, чому якась пара
        # могла не спрацювати
        no_phash_where = [w for w in where if w != "ph.phash IS NOT NULL"]
        no_phash_where.append("(ph.phash IS NULL)")
        with self.db.cursor() as cur:
            cur.execute(f"""SELECT COUNT(*) AS c FROM files f
                                JOIN furry_posts fp ON fp.file_id = f.id
                                LEFT JOIN file_phash ph ON ph.file_id = f.id
                                WHERE {' AND '.join(no_phash_where)}""", args)
            no_phash_count = cur.fetchone()['c']

        with self.db.cursor() as cur:
            cur.execute(f"""
                SELECT f.id, fp.artist_id, ph.phash, f.name AS filename, f.ext AS file_ext
                FROM files f
                JOIN furry_posts fp ON fp.file_id = f.id
                JOIN file_phash ph  ON ph.file_id = f.id
                WHERE {' AND '.join(where)}
                ORDER BY fp.artist_id
            """, args)
            rows = cur.fetchall()

        # Пари, які раніше явно позначили "це не дублікат" — назавжди
        # ігноруються, щоб скан не пропонував їх знову й знову щоразу
        with self.db.cursor() as cur:
            cur.execute("SELECT download_id_a, download_id_b FROM duplicate_ignore_pairs")
            ignored_pairs = {(r['download_id_a'], r['download_id_b']) for r in cur.fetchall()}
        if ignored_pairs:
            print(f"Ігнорованих пар (позначені раніше як 'не дублікат'): {len(ignored_pairs)}")

        print(f"Файлів для порівняння: {len(rows)}")
        if no_phash_count:
            print(f"⚠ Пропущено {no_phash_count} файл(ів) без pHash (не братимуть участі в порівнянні — "
                  f"скористайся синхронізацією, щоб дорахувати хеші для них)")
        print()
        if len(rows) < 2:
            print("Замало файлів для порівняння.")
            return

        found = 0
        already_known = 0
        checked_pairs = 0
        skipped_bad_ext = 0
        closest_dist = None
        closest_pair = None
        # Порівнюємо тільки в межах ОДНОГО автора — дублікат між різними
        # авторами тут не шукаємо (для цього є об'єднання авторів)
        by_artist = {}
        for r in rows:
            by_artist.setdefault(r['artist_id'], []).append(r)

        total_planned = sum(len(items) * (len(items) - 1) // 2 for items in by_artist.values())
        print(f"Пар до перевірки: {total_planned}\n")

        last_progress_report = time.time()
        with self.db.cursor() as cur:
            for aid, items in by_artist.items():
                for i in range(len(items)):
                    for j in range(i + 1, len(items)):
                        a, b = items[i], items[j]
                        ext_a = (a.get('file_ext') or '').lower()
                        ext_b = (b.get('file_ext') or '').lower()
                        if not ext_a or not ext_b:
                            skipped_bad_ext += 1
                            continue
                        if (ext_a in VIDEO_FORMATS) != (ext_b in VIDEO_FORMATS):
                            continue
                        checked_pairs += 1
                        dist = bin(a['phash'] ^ b['phash']).count('1')
                        if closest_dist is None or dist < closest_dist:
                            closest_dist = dist
                            closest_pair = (a['filename'], b['filename'])
                        if dist <= self.tracker.phash_threshold:
                            id_a, id_b = sorted((a['id'], b['id']))
                            if (id_a, id_b) in ignored_pairs:
                                continue
                            cur.execute("""
                                INSERT IGNORE INTO duplicate_scan_results
                                    (artist_id, download_id_a, download_id_b, hamming_distance)
                                VALUES (%s,%s,%s,%s)
                            """, (aid, id_a, id_b, dist))
                            if cur.rowcount:
                                found += 1
                                print(f"  🔍 Схожі: {a['filename']} ↔ {b['filename']} (d={dist})")
                            else:
                                already_known += 1
                        # Періодичний коміт + звіт про прогрес — щоб на великих
                        # бібліотеках було видно, що процес живий, а не завис,
                        # і щоб не тримати одну гігантську транзакцію відкритою
                        if checked_pairs % 5000 == 0:
                            self.db.commit()
                            now = time.time()
                            if now - last_progress_report > 5:
                                pct = (checked_pairs / total_planned * 100) if total_planned else 100
                                print(f"    … перевірено {checked_pairs}/{total_planned} ({pct:.1f}%)")
                                last_progress_report = now
        self.db.commit()
        total_dupes = found + already_known
        print(f"\nПеревірено пар: {checked_pairs}. Нових дублікатів: {found}."
              + (f" Уже відомих з минулого разу: {already_known}." if already_known else ""))
        if skipped_bad_ext:
            print(f"⚠ Пропущено {skipped_bad_ext} пар через відсутнє розширення файлу в БД.")
        if closest_dist is not None and total_dupes == 0:
            print(f"\n📏 Найближча пара з усіх перевірених: {closest_pair[0]} ↔ {closest_pair[1]} "
                  f"(відстань {closest_dist}, поріг {self.tracker.phash_threshold})")
            if closest_dist > self.tracker.phash_threshold:
                print(f"    Це ВИЩЕ порогу — тому й не зафіксовано як дублікат. Якщо на вигляд це той самий "
                      f"арт (інша якість/стиснення), можна тимчасово підняти поріг у Конфізі і повторити скан.")
        if total_dupes:
            print(f"\n✓ Разом {total_dupes} пар(и) чекають на рішення — переглянь список на сторінці автора.")
        print("Переглянь і виріши в дашборді — нічого не видалено автоматично.\n")

    def cleanup_missing(self, dry_run: bool = False):
        mode = "DRY RUN — " if dry_run else ""
        print("\n" + "="*60)
        print(f"  {mode}CLEANUP: перевірка файлів у сховищі")
        print("="*60 + "\n")

        if self.storage_mode == 'local':
            self._cleanup_missing_via_local(dry_run)
            return

        nc_conn = self._get_nc_db()
        if nc_conn:
            self._cleanup_missing_via_db(nc_conn, dry_run)
        else:
            print("  ⚠ nextcloud_db не налаштована — використовую повільний WebDAV-метод")
            print("    (рекомендую заповнити Nextcloud DB в Конфізі дашборда для миттєвої перевірки)\n")
            self._cleanup_missing_via_webdav(dry_run)

    def _cleanup_missing_via_local(self, dry_run: bool):
        """Cleanup для локального сховища — перевіряє наявність файлів на диску."""
        root = self.config.get('local', 'path') or ''
        with self.db.cursor() as cur:
            cur.execute("""
                SELECT f.id, fp.post_id, f.name AS filename, fp.subfolder, a.folder_name, a.artist_name
                FROM furry_posts fp
                JOIN files f   ON f.id = fp.file_id
                JOIN artists a ON a.id = fp.artist_id
                ORDER BY a.artist_name, f.id
            """)
            rows = cur.fetchall()

        total = len(rows)
        missing = []
        print(f"Записів з filename у БД: {total}\n")

        for row in rows:
            # Файли у pool лежать на рівень глибше: @автор/НазваPool/файл.jpg —
            # раніше subfolder тут не враховувався, тому такі файли хибно
            # позначались як відсутні.
            parts = [root, row['folder_name']]
            if row.get('subfolder'):
                parts.append(row['subfolder'])
            parts.append(row['filename'])
            fpath = os.path.join(*parts)
            if not os.path.exists(fpath):
                missing.append(row)
                print(f"  ✗ Відсутній: {row['artist_name']} / {row['filename']} ({row['post_id']})")

        print(f"\nПеревірено: {total}, відсутніх: {len(missing)}")

        if missing and not dry_run:
            with self.db.cursor() as cur:
                for row in missing:
                    cur.execute("DELETE FROM files WHERE id=%s", (row['id'],))
            self.db.commit()

        empty_artists = 0
        if not dry_run:
            empty_artists = self._cleanup_empty_artists()

        print(f"\n{'='*60}")
        print(f"  {'[DRY RUN] ' if dry_run else ''}CLEANUP ЗАВЕРШЕНО (локальний диск)")
        print(f"{'='*60}")
        print(f"Перевірено:  {total}")
        print(f"Відсутніх:   {len(missing)}")
        if dry_run:
            print(f"\n→ Запусти з --cleanup (без --cleanup-dry) щоб реально видалити")
        else:
            print(f"Видалено з БД: {len(missing)}")
            print(f"Видалено порожніх авторів: {empty_artists}")

    def _cleanup_missing_via_db(self, nc_conn, dry_run: bool):
        """Швидкий cleanup — звіряє нашу БД з реальними шляхами файлів в БД Nextcloud
        одним масовим запитом, замість тисяч окремих WebDAV-перевірок.

        Структура папок під кореневим каталогом може бути будь-якої глибини
        (root/@author/file.jpg, root/1/@author/file.jpg, root/a/b/@author/file.jpg тощо),
        тому звіряємо не точний шлях, а пару (папка_автора, ім'я_файлу) — шукаємо
        чи існує ДЕ-НЕБУДЬ під коренем шлях що закінчується на /@author/filename."""
        base = self.config.get('nextcloud', 'base_folder').strip('/')
        # Корінь — це вся базова папка з Конфігу (не тільки перший сегмент,
        # інакше зачіпає сторонні папки поза нею). Глибина ПІД цим коренем
        # може бути будь-якою — саме для цього нижче звіряємо пару
        # (папка_автора, ім'я_файлу), а не точний шлях.
        root = base

        print(f"Завантажую список реальних шляхів файлів з Nextcloud DB (корінь: {root})...")
        with nc_conn.cursor() as cur:
            cur.execute("SELECT path FROM oc_filecache WHERE path LIKE %s", (f"files/{root}/%",))
            all_paths = [row['path'] for row in cur.fetchall()]
        nc_conn.close()
        print(f"  → знайдено {len(all_paths)} файлів/папок на Nextcloud під {root}\n")

        # Будуємо множину пар (folder_name, filename) — останні два сегменти шляху
        # для файлів прямо в папці автора. АЛЕ якщо файл лежить у pool-підпапці
        # (root/@автор/НазваPool/файл.jpg), останні два сегменти дадуть
        # (НазваPool, файл.jpg) — це НЕ те саме, що (@автор, файл.jpg) з БД,
        # і файл хибно вважався б відсутнім. Тому перевіряємо: якщо
        # безпосередній батько НЕ починається на '@' (отже це підпапка pool) —
        # беремо той сегмент, що на рівень вище.
        nc_pairs = set()
        for p in all_paths:
            parts = p.split('/')
            if len(parts) >= 2:
                parent = parts[-2]
                if not parent.startswith('@') and len(parts) >= 3 and parts[-3].startswith('@'):
                    parent = parts[-3]  # це pool-підпапка — беремо папку автора рівнем вище
                nc_pairs.add((parent, parts[-1]))  # (@author, file.jpg)

        with self.db.cursor() as cur:
            cur.execute("""
                SELECT f.id, fp.post_id, f.name AS filename, a.folder_name, a.artist_name
                FROM furry_posts fp
                JOIN files f   ON f.id = fp.file_id
                JOIN artists a ON a.id = fp.artist_id
                ORDER BY a.artist_name, f.id
            """)
            rows = cur.fetchall()

        total   = len(rows)
        missing = []
        print(f"Записів з filename у БД: {total}\n")

        for row in rows:
            pair = (row['folder_name'], row['filename'])
            if pair not in nc_pairs:
                missing.append(row)
                print(f"  ✗ Відсутній: {row['artist_name']} / {row['filename']} ({row['post_id']})")

        print(f"\nПеревірено: {total}, відсутніх: {len(missing)}")

        if missing and not dry_run:
            with self.db.cursor() as cur:
                for row in missing:
                    cur.execute("DELETE FROM files WHERE id=%s", (row['id'],))
            self.db.commit()

        empty_artists = 0
        if not dry_run:
            empty_artists = self._cleanup_empty_artists()

        print(f"\n{'='*60}")
        print(f"  {'[DRY RUN] ' if dry_run else ''}CLEANUP ЗАВЕРШЕНО (через NC DB)")
        print(f"{'='*60}")
        print(f"Перевірено:  {total}")
        print(f"Відсутніх:   {len(missing)}")
        if dry_run:
            print(f"\n→ Запусти з --cleanup (без --cleanup-dry) щоб реально видалити")
        else:
            print(f"Видалено з БД: {len(missing)}")
            print(f"Видалено порожніх авторів: {empty_artists}")

    def _cleanup_missing_via_webdav(self, dry_run: bool):
        """Старий повільний метод — перевіряє кожен файл окремим WebDAV-запитом.
        Використовується тільки якщо nextcloud_db не налаштована.

        УВАГА: цей метод припускає що структура папок завжди
        {base_folder}/{folder_name}/{filename} — фіксованої глибини.
        Якщо реальна структура глибша (наприклад base/1/@author/file)
        перевірка може давати хибні 'відсутній'. Для коректної роботи
        з будь-якою глибиною вкладеності налаштуй Nextcloud DB в Конфізі —
        тоді спрацює швидкий і надійний _cleanup_missing_via_db."""
        base = self.config.get('nextcloud', 'base_folder').strip('/')

        # Беремо всі записи з БД де є filename
        with self.db.cursor() as cur:
            cur.execute("""
                SELECT f.id, fp.post_id, f.name AS filename, fp.subfolder, fp.source,
                       a.folder_name, a.artist_name
                FROM furry_posts fp
                JOIN files f   ON f.id = fp.file_id
                JOIN artists a ON a.id = fp.artist_id
                ORDER BY a.artist_name, f.id
            """)
            rows = cur.fetchall()

        total     = len(rows)
        missing   = 0
        checked   = 0
        errors    = 0
        to_delete = []

        print(f"Записів з filename у БД: {total}\n")

        current_artist = None
        for row in rows:
            checked += 1
            artist  = row['artist_name']
            sub     = f"{row['subfolder']}/" if row.get('subfolder') else ""
            nc_path = f"{base}/{row['folder_name']}/{sub}{row['filename']}"

            if artist != current_artist:
                if to_delete and not dry_run:
                    # Зберігаємо після кожного автора
                    self.db.commit()
                current_artist = artist
                print(f"→ {artist}...")

            try:
                exists = self.nc_scanner.client.check(nc_path)
            except Exception as e:
                errors += 1
                if errors <= 5:
                    print(f"  ⚠ Помилка перевірки {row['filename']}: {e}")
                continue

            if not exists:
                missing += 1
                to_delete.append(row['id'])
                print(f"  ✗ Відсутній: {row['filename']} ({row['post_id']})")

                if not dry_run:
                    with self.db.cursor() as cur:
                        cur.execute("DELETE FROM files WHERE id=%s", (row['id'],))

            # Прогрес кожні 500 файлів
            if checked % 500 == 0:
                pct = checked * 100 // total
                print(f"  [{pct:3d}%] {checked}/{total} перевірено, відсутніх: {missing}")

        if not dry_run:
            self.db.commit()

        empty_artists = 0
        if not dry_run:
            empty_artists = self._cleanup_empty_artists()

        print(f"\n{'='*60}")
        print(f"  {'[DRY RUN] ' if dry_run else ''}CLEANUP ЗАВЕРШЕНО")
        print(f"{'='*60}")
        print(f"Перевірено:  {checked}")
        print(f"Відсутніх:   {missing}")
        print(f"Помилок:     {errors}")
        if dry_run:
            print(f"\n→ Запусти з --cleanup (без --cleanup-dry) щоб реально видалити")
        else:
            print(f"Видалено з БД: {missing}")
            print(f"Видалено порожніх авторів: {empty_artists}")

    def _cleanup_empty_artists(self) -> int:
        """
        Видаляє з БД авторів, у яких більше не лишилось ЖОДНОГО файлу —
        тобто всі їх записи в downloads вже видалені (через cleanup).
        Завдяки ON DELETE CASCADE на artist_id, видалення автора автоматично
        прибирає і всі його pending_duplicates, rejected_duplicates записи —
        тобто від нього не лишається жодного сліду в БД.
        """
        with self.db.cursor() as cur:
            cur.execute("""
                SELECT a.id, a.artist_name FROM artists a
                LEFT JOIN furry_posts fp ON fp.artist_id = a.id
                WHERE fp.file_id IS NULL
            """)
            empty = cur.fetchall()

        if not empty:
            return 0

        with self.db.cursor() as cur:
            for row in empty:
                print(f"  🗑 Видаляю порожнього автора: {row['artist_name']}")
                cur.execute("DELETE FROM artists WHERE id=%s", (row['id'],))
        self.db.commit()
        return len(empty)

    # ── SYNC ALL: сканує ВСЮ кореневу папку і просто заносить усі файли ────────

    SYNC_WORKERS = 8  # паралельних скачувань/читань під час синхронізації

    def sync_all_files(self, dry_run: bool = False, compute_hashes: bool = True):
        """
        'Синхронізація' — рекурсивно обходить кореневу папку сховища
        (Nextcloud або локальний диск) і всі каталоги в ній.

        compute_hashes=True (за замовчуванням у CLI):
            Для нових файлів і для тих, що вже в БД але без хешу — реально
            читає вміст (паралельно, кілька файлів одночасно) і рахує MD5,
            pHash та розміри в пікселях. Для впізнаних імен файлів (e621_XXXX,
            ib_XXXX-pN) джерело теж проставляється. Це точно, але для дуже
            великих бібліотек (сотні тисяч файлів на Nextcloud) може тривати
            дуже довго, бо кожен файл треба реально скачати по мережі.

        compute_hashes=False ("швидкий" режим):
            Просто реєструє файли по імені — без читання вмісту, без
            MD5/pHash/розмірів. Миттєво навіть для сотень тисяч файлів.
            Хеші можна дозаповнити пізніше, повторно запустивши з
            compute_hashes=True — вона зачепить тільки те, чого ще бракує.
        """
        mode = "DRY RUN — " if dry_run else ""
        hmode = "з хешами" if compute_hashes else "швидко, без хешів"
        print("\n" + "="*60)
        print(f"  {mode}СИНХРОНІЗАЦІЯ ({hmode}): сканування кореневої папки сховища ({self.storage_mode})")
        print("="*60 + "\n")

        if self.storage_mode == 'nextcloud':
            nc_conn = self._get_nc_db()
            if nc_conn:
                self._sync_all_via_db(nc_conn, dry_run, compute_hashes)
            else:
                print("  ⚠ nextcloud_db не налаштована — використовую повільний WebDAV-метод")
                print("    (рекомендую заповнити Nextcloud DB в Конфізі дашборда для швидкого сканування)\n")
                self._sync_all_via_webdav(dry_run, compute_hashes)
        elif self.storage_mode == 'local':
            self._sync_all_via_local(dry_run, compute_hashes)
        else:
            print("  ✗ Не налаштовано ні Nextcloud, ні локальну папку — перевір Конфіг")

    def _sync_all_via_local(self, dry_run: bool, compute_hashes: bool):
        """Сканує локальну папку на диску сервера (сховище без Nextcloud)."""
        root = self.config.get('local', 'path')
        print(f"Сканую локальну папку: {root}")
        if not root or not os.path.isdir(root):
            print(f"  ✗ Папка не знайдена або недоступна: {root}")
            return

        by_folder: dict = {}
        dirs_seen = 0
        for dirpath, dirnames, filenames in os.walk(root):
            abs_dirpath = os.path.abspath(dirpath)
            abs_root    = os.path.abspath(root)
            # На кореневому рівні — прибираємо виключені папки з dirnames
            # ПРЯМО ТУТ, щоб os.walk у них узагалі не заходив (а не просто
            # перевіряти назву найближчої папки нижче — та підхід пропускав
            # усе, що лежить у ПІДПАПКАХ виключеної папки, бо там перевірялась
            # назва підпапки, а не батьківської виключеної папки)
            if abs_dirpath == abs_root and self.excluded_folders:
                before = len(dirnames)
                dirnames[:] = [d for d in dirnames if d not in self.excluded_folders]
                if len(dirnames) < before:
                    print(f"  ⏭ Виключено з обходу: {', '.join(self.excluded_folders)}")
            dirs_seen += 1
            if not filenames:
                continue
            if abs_dirpath == abs_root:
                # Файли прямо в корені (без окремої папки автора) —
                # все одно заносимо, у спеціальну папку "_root"
                folder_name, subfolder = "_root", None
            else:
                # Перший сегмент відносного шляху від кореня — завжди
                # "автор" (незалежно від того, є там '@' чи ні), усе, що
                # глибше — subfolder, який може містити кілька рівнів
                # одразу (напр. "pool/серія2"). Це замінює стару перевірку
                # лише НАЙБЛИЖЧОЇ батьківської папки на '@' — та ламалась
                # на будь-якій вкладеності глибше 2 рівнів (файл просто не
                # знаходився потім при показі, бо шлях відновлювався неправильно).
                rel   = os.path.relpath(dirpath, root)
                parts = rel.split(os.sep)
                folder_name = parts[0]
                subfolder   = "/".join(parts[1:]) if len(parts) > 1 else None
            print(f"  … {dirpath}: {len(filenames)} файл(ів)")
            for fname in filenames:
                if fname == 'download.txt':
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fname), root)
                by_folder.setdefault(folder_name, []).append({'name': fname, 'rel': rel, 'subfolder': subfolder})

        total_files = sum(len(v) for v in by_folder.values())
        print(f"\n  → обійдено {dirs_seen} каталог(ів), знайдено {total_files} файлів для імпорту\n")
        if total_files == 0:
            print("  ⚠ Файлів не знайдено. Перевір що:")
            print(f"    - шлях у Конфізі («Локальна папка») дійсно вказує на {root}")
            print("    - процес дашборда має права на читання цієї папки")
            print("    - файли справді лежать усередині (в підпапках або прямо в корені)")
            return
        self._sync_finish(by_folder, dry_run, via="локальний диск", compute_hashes=compute_hashes)

    def _sync_check_pool_relocate(self, artist_id: int, folder_name: str) -> int:
        """
        Для вже наявних у БД файлів цього автора з джерелом e621, які ще не
        лежать у підпапці pool (subfolder IS NULL) — переносить у
        @автор/НазваPool/ (на диску/Nextcloud) і оновлює БД.

        Замість окремого запиту на КОЖЕН файл (повільно і б'є по rate-limit),
        один раз забираємо ВСІ пости автора через search_artist_posts —
        pools там уже включені в кожен пост, той самий виклик, що й при
        звичайному скачуванні. Далі локально зіставляємо post_id з іменем
        файлу — жодних додаткових запитів на файл, крім розшифровки назви
        pool за id (і та кешується в E621Downloader._pool_name_cache).
        """
        if not self.e621_dl:
            return 0
        with self.db.cursor() as cur:
            cur.execute("""
                SELECT f.id, fp.post_id, f.name AS filename, fp.subfolder
                FROM furry_posts fp JOIN files f ON f.id = fp.file_id
                WHERE artist_id=%s AND source='e621'
                  AND (subfolder IS NULL OR pool_order IS NULL)
            """, (artist_id,))
            rows = cur.fetchall()
        if not rows:
            return 0

        artist_name = folder_name[1:] if folder_name.startswith('@') else folder_name
        posts = self.e621_dl.search_artist_posts(artist_name)
        pools_by_post = {p['id']: (p.get('pools') or []) for p in posts}

        moved      = 0  # реально перенесені файли (subfolder щойно проставлено)
        ordered    = 0  # тільки дозаповнено pool_order (файл вже на місці)
        not_found  = []   # пости, яких немає серед результатів пошуку за тегом
        no_pool    = 0    # пости знайдені, але pools=[]
        errors     = []   # move_file() чи _get_pool_info() провалились

        for row in rows:
            post_id = row['post_id']  # напр. "e621_12345"
            numeric = post_id.split('_', 1)[1] if '_' in post_id else None
            if not numeric or not numeric.isdigit():
                continue

            if int(numeric) not in pools_by_post:
                not_found.append(f"{row['filename']} (post {numeric})")
                continue

            pool_ids = pools_by_post.get(int(numeric))
            if not pool_ids:
                no_pool += 1
                continue
            pool_folder, pool_order = self.e621_dl._get_pool_info({'id': int(numeric), 'pools': pool_ids})
            if not pool_folder:
                errors.append(row['filename'])
                continue

            if row['subfolder'] == pool_folder:
                # Файл уже фізично лежить у правильній підпапці (наприклад,
                # це щойно виявив звичайний скан структури) — переміщувати
                # нічого не треба, лишається тільки дозаповнити pool_order.
                with self.db.cursor() as cur:
                    cur.execute("UPDATE furry_posts SET pool_order=%s WHERE file_id=%s",
                                (pool_order, row['id']))
                ordered += 1
                continue

            if row['subfolder']:
                # Лежить в ІНШІЙ підпапці ніж очікувалось (малоймовірно,
                # напр. вручну перейменований pool) — не чіпаємо фізично,
                # тільки лог, щоб не наробити плутанини автоматичним рухом.
                errors.append(f"{row['filename']} (в «{row['subfolder']}», очікувалось «{pool_folder}»)")
                continue

            if self.storage_mode == 'nextcloud':
                base = self.config.get('nextcloud', 'base_folder').strip('/')
                old_rel = f"{base}/{folder_name}/{row['filename']}"
                new_rel = f"{base}/{folder_name}/{pool_folder}/{row['filename']}"
            else:
                old_rel = f"{folder_name}/{row['filename']}"
                new_rel = f"{folder_name}/{pool_folder}/{row['filename']}"
            if self.nc_scanner.move_file(old_rel, new_rel):
                with self.db.cursor() as cur:
                    cur.execute("UPDATE furry_posts SET subfolder=%s, pool_order=%s WHERE file_id=%s",
                                (pool_folder, pool_order, row['id']))
                self._sync_rel_path(row['id'])   # файл переїхав — оновлюємо шлях у ядрі
                moved += 1
                print(f"    📦 {row['filename']} → «{pool_folder}» (#{pool_order})")
            else:
                errors.append(row['filename'])

        # Компактний підсумок замість рядка на кожен файл
        summary = (f"    🔍 pool-перевірка {folder_name}: {len(rows)} файл(ів) перевірено, "
                   f"{len(posts)} постів знайдено за тегом «{artist_name}», "
                   f"перенесено {moved}, дозаповнено порядок {ordered}, без pool {no_pool}")
        if not_found: summary += f", не знайдено в пошуку {len(not_found)}"
        if errors:    summary += f", помилок {len(errors)}"
        print(summary)

        if not_found:
            sample = ', '.join(not_found[:3])
            more = f" (+{len(not_found)-3} ще)" if len(not_found) > 3 else ""
            print(f"      ⚠ не знайдено серед постів за тегом «{artist_name}»: {sample}{more}")
            print(f"      → якщо це помилково — тег автора в назві папки міг відрізнятись "
                  f"від реального тегу на e621 (alias/перейменування)")
        if errors:
            sample = ', '.join(errors[:3])
            print(f"      ⚠ помилки перенесення: {sample}")

        if moved or ordered:
            self.db.commit()
        return moved + ordered

    def _sync_check_pool_relocate_inkbunny(self, artist_id: int, folder_name: str) -> int:
        """
        Той самий retro-активний перенос у pool-підпапки, що вже є для e621
        (_sync_check_pool_relocate), тепер і для Inkbunny — для файлів,
        скачаних ще ДО того, як з'явилось визначення pool для цього джерела.
        """
        if not self.ib_dl:
            return 0
        with self.db.cursor() as cur:
            cur.execute("""
                SELECT f.id, fp.post_id, f.name AS filename, fp.subfolder
                FROM furry_posts fp JOIN files f ON f.id = fp.file_id
                WHERE artist_id=%s AND source='inkbunny'
                  AND (subfolder IS NULL OR pool_order IS NULL)
            """, (artist_id,))
            rows = cur.fetchall()
        if not rows:
            return 0

        sub_ids = []
        for row in rows:
            numeric = row['post_id'].split('_', 1)[1] if '_' in row['post_id'] else None
            if numeric and numeric.isdigit():
                sub_ids.append(numeric)
        if not sub_ids:
            return 0

        # Одним (чи кількома, максимум 100 за раз — обмеження самого API)
        # пакетним запитом з show_pools=yes, замість запиту на КОЖЕН файл
        full_by_id = {}
        for i in range(0, len(sub_ids), 100):
            chunk = sub_ids[i:i + 100]
            self.ib_dl._rate_limit()
            try:
                r = self.ib_dl.session.post(f"{INKBUNNY_API_BASE}_submissions.php", data={
                    'sid': self.ib_dl.sid, 'submission_ids': ','.join(chunk),
                    'show_description': 'no', 'show_pools': 'yes'
                })
                r.raise_for_status()
                for full in r.json().get('submissions', []):
                    full_by_id[str(full.get('submission_id'))] = full
            except Exception as e:
                print(f"    ✗ Inkbunny деталі (pool-перевірка): {e}")

        moved, ordered, no_pool = 0, 0, 0
        not_found, errors = [], []

        for row in rows:
            numeric = row['post_id'].split('_', 1)[1] if '_' in row['post_id'] else None
            if not numeric or not numeric.isdigit():
                continue
            full = full_by_id.get(numeric)
            if not full:
                not_found.append(f"{row['filename']} (post {numeric})")
                continue
            pools_list = full.get('pools') or []
            if not pools_list:
                no_pool += 1
                continue
            pool_folder, pool_order = self.ib_dl._get_pool_info(int(numeric), pools_list)
            if not pool_folder:
                errors.append(row['filename'])
                continue

            if row['subfolder'] == pool_folder:
                with self.db.cursor() as cur:
                    cur.execute("UPDATE furry_posts SET pool_order=%s WHERE file_id=%s",
                                (pool_order, row['id']))
                ordered += 1
                continue

            if row['subfolder']:
                errors.append(f"{row['filename']} (в «{row['subfolder']}», очікувалось «{pool_folder}»)")
                continue

            if self.storage_mode == 'nextcloud':
                base = self.config.get('nextcloud', 'base_folder').strip('/')
                old_rel = f"{base}/{folder_name}/{row['filename']}"
                new_rel = f"{base}/{folder_name}/{pool_folder}/{row['filename']}"
            else:
                old_rel = f"{folder_name}/{row['filename']}"
                new_rel = f"{folder_name}/{pool_folder}/{row['filename']}"
            if self.nc_scanner.move_file(old_rel, new_rel):
                with self.db.cursor() as cur:
                    cur.execute("UPDATE furry_posts SET subfolder=%s, pool_order=%s WHERE file_id=%s",
                                (pool_folder, pool_order, row['id']))
                self._sync_rel_path(row['id'])   # файл переїхав — оновлюємо шлях у ядрі
                moved += 1
                print(f"    📦 {row['filename']} → «{pool_folder}» (#{pool_order})")
            else:
                errors.append(row['filename'])

        summary = (f"    🔍 pool-перевірка (Inkbunny) {folder_name}: {len(rows)} файл(ів) перевірено, "
                   f"перенесено {moved}, дозаповнено порядок {ordered}, без pool {no_pool}")
        if not_found: summary += f", не знайдено {len(not_found)}"
        if errors:    summary += f", помилок {len(errors)}"
        print(summary)

        if moved or ordered:
            self.db.commit()
        return moved + ordered

    def _sync_hash_one(self, f: dict) -> dict:
        """Виконується в окремому потоці: читає файл і рахує MD5/pHash/розміри."""
        filename = f['name']
        content = self.nc_scanner.read_bytes(f['rel'])
        if content is None:
            return {'name': filename, 'ok': False}
        md5_hash = hashlib.md5(content).hexdigest()
        file_ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        phash_val = width = height = None
        if file_ext in (IMAGE_FORMATS | VIDEO_FORMATS):
            phash_val     = compute_phash(content, file_ext)
            width, height = get_media_dimensions(content, file_ext)
        return {'name': filename, 'ok': True, 'md5_hash': md5_hash, 'file_ext': file_ext,
                'phash_val': phash_val, 'width': width, 'height': height}

    def _sync_process_folder(self, folder_name: str, files: list, dry_run: bool,
                             compute_hashes: bool = True) -> tuple[int, int]:
        """Обробляє один каталог: додає файли яких ще нема в БД, і (якщо
        compute_hashes) дозаповнює MD5/pHash/розміри тим записам, де їх
        бракує. Повертає (додано+дозаповнено, без змін).

        existing_map ключується парою (filename, subfolder), а НЕ просто
        іменем файлу — інакше '000.png' з РІЗНИХ pool одного автора (типове
        для вручну зроблених коміксів, де кожна серія має свою просту
        нумерацію сторінок) вважались би ОДНИМ і тим самим файлом, і другий
        pool просто ніколи не додавався б."""
        artist_name = folder_name[1:] if folder_name.startswith('@') else folder_name

        def _key(f_or_name, sub=None):
            if isinstance(f_or_name, dict):
                return (f_or_name['name'], f_or_name.get('subfolder'))
            return (f_or_name, sub)

        if dry_run:
            with self.db.cursor() as cur:
                cur.execute("SELECT id FROM artists WHERE folder_name=%s", (folder_name,))
                existing = cur.fetchone()
            existing_map = {}
            if existing:
                with self.db.cursor() as cur:
                    cur.execute("""SELECT f.name AS filename, fp.subfolder, f.md5_hash
                                  FROM furry_posts fp JOIN files f ON f.id=fp.file_id
                                  WHERE fp.artist_id=%s""", (existing['id'],))
                    existing_map = {(r['filename'], r['subfolder']): r['md5_hash'] for r in cur.fetchall()}
            new_count = sum(1 for f in files if _key(f) not in existing_map)
            if not compute_hashes:
                return new_count, len(files) - new_count
            backfill_count = sum(1 for f in files if _key(f) in existing_map and existing_map[_key(f)] is None)
            return new_count + backfill_count, len(files) - new_count - backfill_count

        artist_id = self.tracker.get_or_create_artist(folder_name, artist_name)
        with self.db.cursor() as cur:
            cur.execute("""SELECT f.id, f.name AS filename, f.md5_hash, fp.post_url, fp.subfolder
                              FROM furry_posts fp JOIN files f ON f.id=fp.file_id
                              WHERE fp.artist_id=%s""", (artist_id,))
            existing_map = {(r['filename'], r['subfolder']): r for r in cur.fetchall()}

        # ── Дозаповнюємо post_url/source там де їх бракує — це чисто
        #    обчислення з імені файлу, читати сам файл для цього не треба,
        #    тож робимо це завжди, незалежно від fast/full режиму ─────────
        for f in files:
            filename = f['name']
            row = existing_map.get(_key(f))
            if row is None or row['post_url'] is not None:
                continue
            _, _, _, source, post_url = _sync_parse(filename, f.get('subfolder'))
            if not post_url:
                continue
            with self.db.cursor() as cur:
                cur.execute("UPDATE furry_posts SET source=%s, post_url=%s WHERE file_id=%s",
                            (source, post_url, row['id']))
            row['post_url'] = post_url  # щоб нижче не намагались перезаписати ще раз

        if files:
            self.db.commit()

        # ── Швидкий режим: без читання вмісту файлів ────────────────────
        if not compute_hashes:
            added = 0
            for f in files:
                filename = f['name']
                if _key(f) in existing_map:
                    continue
                post_id, page_num, file_ext, source, post_url = _sync_parse(filename, f.get('subfolder'))
                self.tracker.add_download(
                    artist_id=artist_id, post_id=post_id, source=source,
                    post_url=post_url, md5_hash=None, filename=filename,
                    file_ext=file_ext, page_num=page_num, phash_val=None,
                    subfolder=f.get('subfolder'),
                )
                added += 1
            self.db.commit()
            return added, len(files) - added

        # ── Повний режим: MD5/pHash/розміри, паралельно ─────────────────
        todo = [f for f in files
                if _key(f) not in existing_map or existing_map[_key(f)]['md5_hash'] is None]
        if not todo:
            return 0, len(files)

        print(f"    … починаю обробку {len(todo)} файлів (до {self.SYNC_WORKERS} паралельно)…")
        results   = {}
        processed = 0
        last_print_time = time.time()
        with ThreadPoolExecutor(max_workers=self.SYNC_WORKERS) as pool:
            # Ключуємо future_map тим самим (ім'я, subfolder), щоб не плутати
            # однойменні файли з різних pool і тут теж
            future_map = {pool.submit(self._sync_hash_one, f): _key(f) for f in todo}
            for future in as_completed(future_map):
                key = future_map[future]
                try:
                    results[key] = future.result()
                except Exception:
                    results[key] = {'name': key[0], 'ok': False}
                processed += 1
                now = time.time()
                # друкуємо прогрес кожні 10 файлів АБО якщо минуло більше 5с
                # з попереднього друку — щоб папки з важкими відео теж
                # показували що щось відбувається, а не виглядали "мертвими"
                if processed % 10 == 0 or processed == len(todo) or (now - last_print_time) >= 5:
                    print(f"    … оброблено {processed}/{len(todo)}")
                    last_print_time = now

        added = 0
        for f in todo:
            filename     = f['name']
            key          = _key(f)
            res          = results.get(key) or {'ok': False}
            existing_row = existing_map.get(key)

            if not res.get('ok'):
                if existing_row is None:
                    post_id, page_num, file_ext, source, post_url = _sync_parse(filename, f.get('subfolder'))
                    self.tracker.add_download(
                        artist_id=artist_id, post_id=post_id, source=source,
                        post_url=post_url, md5_hash=None, filename=filename,
                        file_ext=file_ext, page_num=page_num, phash_val=None,
                        subfolder=f.get('subfolder'),
                    )
                    added += 1
                # інакше — файл вже був у БД, хеш лишається порожнім, спробуємо наступного разу
                continue

            if existing_row is not None:
                with self.db.cursor() as cur:
                    cur.execute("""
                        UPDATE files SET md5_hash=%s, width=%s, height=%s
                        WHERE id=%s
                    """, (res['md5_hash'], res['phash_val'], res['width'], res['height'], existing_row['id']))
                added += 1
            else:
                post_id, page_num, _, source, post_url = _sync_parse(filename, f.get('subfolder'))
                self.tracker.add_download(
                    artist_id=artist_id, post_id=post_id, source=source,
                    post_url=post_url, md5_hash=res['md5_hash'], filename=filename,
                    file_ext=res['file_ext'], page_num=page_num, phash_val=res['phash_val'],
                    width=res['width'], height=res['height'],
                    subfolder=f.get('subfolder'),
                )
                added += 1
        self.db.commit()
        return added, len(files) - added

    def _sync_all_via_db(self, nc_conn, dry_run: bool, compute_hashes: bool):
        """Швидкий шлях — бере повний список файлів одним запитом до Nextcloud DB
        (oc_filecache), замість тисяч окремих WebDAV-запитів для самого списку."""
        root = self.config.get('nextcloud', 'base_folder').strip('/')

        print(f"Завантажую список файлів з Nextcloud DB (корінь: {root})...")
        with nc_conn.cursor() as cur:
            cur.execute("""
                SELECT fc.path FROM oc_filecache fc
                WHERE fc.path LIKE %s
                  AND fc.mimetype != (SELECT id FROM oc_mimetypes WHERE mimetype='httpd/unix-directory')
                  AND fc.name != 'download.txt'
            """, (f"files/{root}/%",))
            rows = cur.fetchall()
        nc_conn.close()
        print(f"  → знайдено {len(rows)} файлів на Nextcloud під {root}\n")

        by_folder: dict = {}
        root_prefix = f"files/{root}/"
        for row in rows:
            p = row['path']
            # Перевіряємо ПЕРШИЙ сегмент шляху ПІСЛЯ кореня — а не найближчу
            # папку — інакше підпапки виключеної папки (Private/SubFolder/…)
            # проходили б перевірку під власною назвою SubFolder, а не Private
            if self.excluded_folders and p.startswith(root_prefix):
                top_segment = p[len(root_prefix):].split('/', 1)[0]
                if top_segment in self.excluded_folders:
                    continue
            parts = p.split('/')
            if len(parts) < 2:
                continue
            filename = parts[-1]
            # Перший сегмент шляху ПІСЛЯ кореня — завжди "автор", усе між
            # ним і файлом — subfolder (може бути кілька рівнів одразу).
            # Замінює стару перевірку лише найближчого рівня, яка ламалась
            # на вкладеності глибше 2 рівнів.
            rel_after_root = p[len(root_prefix):] if p.startswith(root_prefix) else "/".join(parts[-2:])
            rel_parts = rel_after_root.split('/')
            folder_name = rel_parts[0]
            subfolder   = "/".join(rel_parts[1:-1]) if len(rel_parts) > 2 else None
            # 'files/' — внутрішній префікс Nextcloud, у WebDAV-шляху його нема
            rel = p[len('files/'):] if p.startswith('files/') else p
            by_folder.setdefault(folder_name, []).append({'name': filename, 'rel': rel, 'subfolder': subfolder})

        self._sync_finish(by_folder, dry_run, via="NC DB", compute_hashes=compute_hashes)

    def _sync_all_via_webdav(self, dry_run: bool, compute_hashes: bool):
        """Повільний fallback — рекурсивно обходить папки через WebDAV."""
        base = self.config.get('nextcloud', 'base_folder').strip('/')
        print(f"Рекурсивно сканую {base} через WebDAV (це може зайняти час)...")
        items = self._walk_webdav(base)
        print(f"  → знайдено {len(items)} файлів\n")

        by_folder: dict = {}
        for it in items:
            by_folder.setdefault(it['folder_name'], []).append(
                {'name': it['name'], 'rel': it['rel'], 'subfolder': it.get('subfolder')})

        self._sync_finish(by_folder, dry_run, via="WebDAV", compute_hashes=compute_hashes)

    def _walk_webdav(self, path: str, artist_folder: str | None = None, sub_parts: list | None = None) -> list:
        """
        Рекурсивно обходить папку через WebDAV. Перший рівень під коренем
        сховища завжди стає "автором" (artist_folder), усе глибше
        накопичується в sub_parts і йде як subfolder (може містити кілька
        рівнів одразу, напр. "pool/серія2") — узгоджено з локальним і
        DB-варіантами синхронізації. Раніше перевірявся лише НАЙБЛИЖЧИЙ
        рівень на '@', що ламалось на будь-якій вкладеності глибше 2 рівнів.
        """
        results = []
        try:
            items = self.nc_scanner.client.list(path)
        except Exception as e:
            print(f"  ⚠ Не вдалося прочитати {path}: {e}")
            return results

        if artist_folder is None:
            sub_parts = []

        for item in items:
            if item.strip('/') == path.strip('/'):
                continue
            name = item.rstrip('/').split('/')[-1]
            if item.endswith('/'):
                if artist_folder is None:
                    # Прямий нащадок кореня сховища — стає новим "автором"
                    if name in self.excluded_folders:
                        continue
                    results.extend(self._walk_webdav(f"{path.rstrip('/')}/{name}", name, []))
                else:
                    results.extend(self._walk_webdav(f"{path.rstrip('/')}/{name}", artist_folder, sub_parts + [name]))
            else:
                if name == 'download.txt':
                    continue
                rel = f"{path.rstrip('/')}/{name}"
                if artist_folder is None:
                    # Файл прямо в корені сховища, без окремої папки автора
                    results.append({'folder_name': '_root', 'name': name, 'rel': rel, 'subfolder': None})
                else:
                    this_sub = "/".join(sub_parts) if sub_parts else None
                    results.append({'folder_name': artist_folder, 'name': name, 'rel': rel, 'subfolder': this_sub})
        return results

    def _sync_finish(self, by_folder: dict, dry_run: bool, via: str, compute_hashes: bool = True):
        total_folders = len(by_folder)
        total_added   = 0
        total_skipped = 0
        print(f"Знайдено {total_folders} каталогів\n")

        for i, (folder_name, files) in enumerate(sorted(by_folder.items()), 1):
            print(f"→ [{i}/{total_folders}] {folder_name} ({len(files)} файлів)...")
            added, skipped = self._sync_process_folder(folder_name, files, dry_run, compute_hashes)
            total_added   += added
            total_skipped += skipped
            label = ('нових/без хешу' if dry_run else 'додано/дозаповнено') if compute_hashes else \
                    ('нових' if dry_run else 'додано')
            print(f"    {label}: {added}, без змін: {skipped}")

            if compute_hashes and not dry_run and folder_name != "_root":
                with self.db.cursor() as cur:
                    cur.execute("SELECT id FROM artists WHERE folder_name=%s", (folder_name,))
                    artist_row = cur.fetchone()
                if artist_row:
                    moved = self._sync_check_pool_relocate(artist_row['id'], folder_name)
                    if moved:
                        print(f"    📦 перенесено в pool-підпапки: {moved}")
                    moved_ib = self._sync_check_pool_relocate_inkbunny(artist_row['id'], folder_name)
                    if moved_ib:
                        print(f"    📦 перенесено в pool-підпапки (Inkbunny): {moved_ib}")

        print(f"\n{'='*60}")
        print(f"  {'[DRY RUN] ' if dry_run else ''}СИНХРОНІЗАЦІЯ ЗАВЕРШЕНА (через {via})")
        print(f"{'='*60}")
        print(f"Каталогів оброблено: {total_folders}")
        print(f"Файлів {'буде додано' if dry_run else 'додано'}:       {total_added}")
        print(f"Вже було в БД:       {total_skipped}")

        if hasattr(self.nc_scanner, '_direct_ok'):
            ok, fail = self.nc_scanner._direct_ok, self.nc_scanner._direct_fail
            if ok or fail:
                print(f"Прочитано напряму з диска: {ok}")
                print(f"Довантажено через WebDAV:  {fail}")
                if fail and not ok:
                    print("  ⚠ Жодного файлу не вдалось прочитати напряму — перевір шлях у Конфізі")

    # ── MIGRATE TAGS: скачивает теги из API для уже записанных файлов ─────────

    def migrate_tags(self):
        print("\n" + "="*60)
        print("  МІГРАЦІЯ ТЕГІВ з e621 / Inkbunny")
        print("="*60 + "\n")

        with self.db.cursor() as cur:
            cur.execute("""
                SELECT fp.file_id AS id, fp.post_id, fp.source, a.artist_name
                FROM furry_posts fp
                JOIN artists a ON a.id = fp.artist_id
                WHERE fp.post_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM download_tags dt WHERE dt.download_id = fp.file_id
                  )
                ORDER BY fp.source, a.artist_name, fp.file_id
            """)
            rows = cur.fetchall()

        total = len(rows)
        print(f"Файлів без тегів: {total}\n")
        if not total:
            print("✓ Всі файли вже мають теги!")
            return

        # Групуємо по (джерело, автор) — замість одного API-запиту на КОЖЕН
        # файл робимо один ПАКЕТНИЙ запит на автора і зіставляємо теги
        # локально. Раніше на кожен файл був окремий запит + пауза
        # rate-limit — на тисячах файлів це й було справжньою причиною
        # жахливої швидкості.
        groups: dict[tuple, list] = {}
        for row in rows:
            key = (row['source'], row['artist_name'])
            groups.setdefault(key, []).append(row)

        done = 0
        errors = 0
        artists_done = 0
        total_artists = len(groups)

        for (source, artist_name), group_rows in groups.items():
            artists_done += 1
            try:
                tags_by_numeric_id = {}

                if source == 'e621' and self.e621_dl:
                    posts = self.e621_dl.search_artist_posts(artist_name)
                    for post in posts:
                        tags_by_numeric_id[post['id']] = self.e621_dl._extract_e621_tags(post)

                elif source == 'inkbunny' and self.ib_dl and self.ib_dl.sid:
                    subs = self.ib_dl.search_artist_submissions(artist_name)
                    sub_ids = [str(s.get('submission_id')) for s in subs if s.get('submission_id')]
                    if sub_ids:
                        time.sleep(INKBUNNY_RATE_LIMIT)
                        r = self.ib_dl.session.post(
                            f"{INKBUNNY_API_BASE}_submissions.php",
                            data={'sid': self.ib_dl.sid, 'submission_ids': ','.join(sub_ids),
                                  'show_description': 'no'}
                        )
                        r.raise_for_status()
                        for full in r.json().get('submissions', []):
                            sid = full.get('submission_id')
                            if sid is None:
                                continue
                            kws = [kw.get('keyword_name', '').strip()
                                   for kw in (full.get('keywords') or []) if kw.get('keyword_name', '').strip()]
                            tags_by_numeric_id[int(sid)] = kws
                else:
                    continue

                # Тепер уже без жодних додаткових запитів — просто зіставляємо
                # локально по post_id, який і так уже знаємо для кожного файлу
                for row in group_rows:
                    download_id = row['id']
                    post_id     = row['post_id']
                    numeric_id_str = post_id.replace('e621_', '').replace('ib_', '').split('-')[0]
                    if not numeric_id_str.isdigit():
                        continue
                    numeric_id = int(numeric_id_str)
                    tags = tags_by_numeric_id.get(numeric_id)
                    if tags:
                        self.tracker._save_tags(download_id, tags)
                    done += 1

                self.db.commit()
                pct     = artists_done * 100 // total_artists
                bar_len = 20
                filled  = int(bar_len * artists_done // total_artists)
                bar     = '█' * filled + '░' * (bar_len - filled)
                print(f"  [{bar}] {pct:3d}% автор {artists_done}/{total_artists}  {artist_name} ({len(group_rows)} файлів)")

            except Exception as e:
                errors += len(group_rows)
                if errors <= 10:
                    print(f"  ✗ {artist_name}: {e}")
                continue

        self.db.commit()
        print(f"\n{'='*60}")
        print(f"  МІГРАЦІЯ ТЕГІВ ЗАВЕРШЕНА")
        print(f"{'='*60}")
        print(f"Оброблено:  {done}")
        print(f"Помилок:    {errors}")


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Furry Art Archiver v2')
    parser.add_argument('--dry-run',       action='store_true', help='Тестовый запуск без скачивания')
    parser.add_argument('--setup',         action='store_true', help='Создать шаблон конфига')
    parser.add_argument('--migrate',       action='store_true', help='Перенести download.txt → MariaDB')
    parser.add_argument('--migrate-tags',  action='store_true', help='Скачать теги из e621/Inkbunny для уже скачанных файлов')
    parser.add_argument('--sync-all',      action='store_true', help='Просканувати всю кореневу папку і каталоги в ній, додати всі знайдені файли в БД')
    parser.add_argument('--artist',        type=str, default=None, help='Завантажити всю бібліотеку одного конкретного автора (створює папку якщо треба)')
    parser.add_argument('--auto-update',   action='store_true', help='Перевірити на нове ТІЛЬКИ авторів з увімкненим авто-оновленням (для щоденного планувальника)')
    parser.add_argument('--find-duplicates', action='store_true', help='Ретроактивний пошук дублікатів у вже наявній бібліотеці (обмежується --artist і/або --subfolder, якщо задані)')
    parser.add_argument('--subfolder',     type=str, default=None, help='Обмежити --find-duplicates конкретним pool усередині автора')
    parser.add_argument('--backfill-sizes', action='store_true', help='Довантажити вагу (size_bytes) для файлів, скачаних до появи цього відстежування')
    parser.add_argument('--cleanup',       action='store_true', help='Видалити з БД записи файлів яких немає на Nextcloud')
    parser.add_argument('--cleanup-dry',   action='store_true', help='Показати що буде видалено без реального видалення')
    parser.add_argument('--user-id', type=int, default=1,
                        help='ID користувача (з таблиці users), чиї nextcloud/e621/inkbunny налаштування використовувати. За замовчуванням 1 (адмін).')
    args = parser.parse_args()

    if args.setup:
        Config()
        print(f"✓ Создан шаблон конфига: {CONFIG_FILE}")
        print("  Заполните секцию [database].")
        print("  Решта параметрів (nextcloud/e621/inkbunny) налаштовуються через дашборд.")
        return

    archiver = FurryArchiver(user_id=args.user_id)
    try:
        if args.migrate:
            archiver.migrate_from_txt()
        elif args.migrate_tags:
            archiver.migrate_tags()
        elif args.sync_all:
            archiver.sync_all_files(dry_run=args.dry_run)
        elif args.cleanup or args.cleanup_dry:
            archiver.cleanup_missing(dry_run=args.cleanup_dry)
        elif args.find_duplicates:
            archiver.scan_for_duplicates(artist_name=args.artist, subfolder=args.subfolder)
        elif args.backfill_sizes:
            archiver.backfill_file_sizes()
        elif args.artist:
            archiver.run(dry_run=args.dry_run, artist_name=args.artist)
        elif args.auto_update:
            with archiver.db.cursor() as cur:
                cur.execute("SELECT artist_name FROM artists WHERE auto_update=1")
                names = [row['artist_name'] for row in cur.fetchall()]
            if not names:
                print("Немає авторів з увімкненим авто-оновленням.")
            else:
                archiver.run(dry_run=args.dry_run, artist_names=names)
        else:
            archiver.run(dry_run=args.dry_run)
    except KeyboardInterrupt:
        print("\n\n✗ Прервано пользователем")
    finally:
        archiver.close()


if __name__ == "__main__":
    main()