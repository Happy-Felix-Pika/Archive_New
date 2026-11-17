-- Пули як окремі таблиці (див. backend/poolstore.py):
-- пул — один рядок, у нього може бути кілька назв (різні сайти),
-- сторінки — «пул → файл → номер». Заповнюються при старті сервера
-- з furry_posts і старої furry_pool_links (яка після цього прибирається).
CREATE TABLE IF NOT EXISTS furry_pools (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    artist_id  INT          NOT NULL,
    name       VARCHAR(255) NOT NULL,
    source     VARCHAR(16)  NULL,
    cnt        INT          NOT NULL DEFAULT 0,
    created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_artist_name (artist_id, name),
    INDEX idx_cnt (cnt),
    FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS furry_pool_names (
    artist_id  INT          NOT NULL,
    name       VARCHAR(255) NOT NULL,
    pool_id    INT          NOT NULL,
    PRIMARY KEY (artist_id, name),
    INDEX idx_pool (pool_id),
    FOREIGN KEY (pool_id) REFERENCES furry_pools(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS furry_pool_items (
    pool_id    INT     NOT NULL,
    file_id    BIGINT  NOT NULL,
    page       SMALLINT UNSIGNED NULL,
    via_folder TINYINT NOT NULL DEFAULT 0,
    PRIMARY KEY (pool_id, file_id),
    INDEX idx_file (file_id),
    INDEX idx_page (pool_id, page),
    FOREIGN KEY (pool_id) REFERENCES furry_pools(id) ON DELETE CASCADE,
    FOREIGN KEY (file_id) REFERENCES files(id)       ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
