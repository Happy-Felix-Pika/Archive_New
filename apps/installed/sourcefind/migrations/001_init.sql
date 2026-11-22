-- Результат пошуку джерела для файлу (один рядок на файл)
CREATE TABLE IF NOT EXISTS srcfind_results (
    file_id     BIGINT PRIMARY KEY,
    status      VARCHAR(16) NOT NULL,          -- applied | review | none | error | rejected
    site        VARCHAR(32) NULL,              -- e621 | inkbunny | furaffinity | twitter | weasyl | …
    site_id     VARCHAR(64) NULL,
    url         VARCHAR(512) NULL,             -- сторінка оригіналу
    artists     VARCHAR(512) NULL,
    method      VARCHAR(16) NULL,              -- md5 | fuzzy | iqdb
    score       FLOAT NULL,                    -- iqdb: 0–100; fuzzy: відстань (менше — краще)
    candidates  MEDIUMTEXT NULL,               -- JSON: усі знайдені варіанти
    tags_added  INT DEFAULT 0,
    error       VARCHAR(512) NULL,
    checked_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
