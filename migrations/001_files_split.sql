-- ============================================================
-- Етап 1, фаза «розширення».
--
-- Створює універсальні таблиці ядра поруч зі старими і наповнює їх
-- з downloads. Таблиця downloads НЕ ЗМІНЮЄТЬСЯ і лишається джерелом
-- правди — застосунок після цієї міграції працює як раніше.
--
-- Ключове рішення: files.id = downloads.id. Завдяки цьому всі наявні
-- зовнішні ключі (user_favorites, download_tags, duplicate_*) і всі
-- URL фронтенду (/api/gallery/meta/<id>) лишаються чинними.
--
-- Міграція ідемпотентна: можна запускати повторно.
-- ============================================================

-- ── Ядро: файл як самостійна сутність ───────────────────────
-- Ні автора, ні поста — тільки те, що є в будь-якого файлу.
CREATE TABLE IF NOT EXISTS files (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    storage_id    VARCHAR(64)   NOT NULL DEFAULT 'default',
    rel_path      VARCHAR(1024) NOT NULL,   -- шлях від кореня сховища, разом з іменем
    path_hash     CHAR(32) AS (MD5(rel_path)) STORED,
    name          VARCHAR(255)  NOT NULL,
    ext           VARCHAR(10),
    size_bytes    BIGINT UNSIGNED,
    width         INT UNSIGNED,
    height        INT UNSIGNED,
    md5_hash      CHAR(32),
    owner_user_id INT,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_path (storage_id, path_hash),   -- знімається в 002 (див. коментар там)
    INDEX idx_md5  (md5_hash),
    INDEX idx_name (name),
    INDEX idx_ext  (ext)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── Furry-застосунок: що цей файл означає на зовнішньому сайті ──
CREATE TABLE IF NOT EXISTS furry_posts (
    file_id       BIGINT PRIMARY KEY,
    artist_id     INT NOT NULL,
    post_id       VARCHAR(64) NOT NULL,
    source        ENUM('e621','inkbunny'),
    post_url      VARCHAR(512),
    page_num      TINYINT DEFAULT 1,
    subfolder     VARCHAR(255),
    pool_order    SMALLINT UNSIGNED,
    downloaded_at DATETIME,
    UNIQUE KEY uniq_post (artist_id, post_id, page_num),
    INDEX idx_artist    (artist_id),
    INDEX idx_post_id   (post_id),
    INDEX idx_subfolder (subfolder),
    FOREIGN KEY (file_id)   REFERENCES files(id)   ON DELETE CASCADE,
    FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── Застосунок дублікатів: перцептивний хеш ─────────────────
-- Це не властивість файлу, а робочі дані конкретної функції.
CREATE TABLE IF NOT EXISTS file_phash (
    file_id BIGINT PRIMARY KEY,
    phash   BIGINT UNSIGNED,
    INDEX idx_phash (phash),
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ── Наповнення ──────────────────────────────────────────────
-- rel_path будується точно так само, як local_file_path() у коді:
--   '_root'          -> [підпапка/]файл
--   звичайний автор  -> тека_автора/[підпапка/]файл
INSERT INTO files (id, storage_id, rel_path, name, ext, size_bytes,
                   width, height, md5_hash, owner_user_id, created_at)
SELECT
    d.id,
    'default',
    CONCAT(
        CASE WHEN a.folder_name = '_root' THEN '' ELSE CONCAT(a.folder_name, '/') END,
        CASE WHEN d.subfolder IS NULL OR d.subfolder = '' THEN '' ELSE CONCAT(d.subfolder, '/') END,
        d.filename
    ),
    d.filename,
    d.file_ext,
    d.size_bytes,
    d.width,
    d.height,
    d.md5_hash,
    NULL,
    d.downloaded_at
FROM downloads d
JOIN artists a ON a.id = d.artist_id
WHERE d.filename IS NOT NULL AND d.filename <> ''
ON DUPLICATE KEY UPDATE id = files.id;

INSERT INTO furry_posts (file_id, artist_id, post_id, source, post_url,
                         page_num, subfolder, pool_order, downloaded_at)
SELECT d.id, d.artist_id, d.post_id, d.source, d.post_url,
       d.page_num, d.subfolder, d.pool_order, d.downloaded_at
FROM downloads d
JOIN files f ON f.id = d.id
ON DUPLICATE KEY UPDATE file_id = furry_posts.file_id;

INSERT INTO file_phash (file_id, phash)
SELECT d.id, d.phash
FROM downloads d
JOIN files f ON f.id = d.id
WHERE d.phash IS NOT NULL
ON DUPLICATE KEY UPDATE phash = VALUES(phash);

-- AUTO_INCREMENT files не повинен видавати id, які вже зайняті в downloads
-- (поки обидві таблиці живі, id мають лишатись спільними).
SET @next = (SELECT IFNULL(MAX(id), 0) + 1 FROM downloads);
SET @sql = CONCAT('ALTER TABLE files AUTO_INCREMENT = ', @next);
PREPARE s FROM @sql; EXECUTE s; DEALLOCATE PREPARE s;
