-- Таблиці застосунку. Раніше їх створювало ядро.
-- IF NOT EXISTS: на наявній базі нічого не станеться.

CREATE TABLE IF NOT EXISTS artists (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        folder_name VARCHAR(255) NOT NULL UNIQUE,
        artist_name VARCHAR(255) NOT NULL,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_folder (folder_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS artist_aliases (
        id           INT AUTO_INCREMENT PRIMARY KEY,
        artist_id    INT NOT NULL,
        alias_name   VARCHAR(255) NOT NULL,
        created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_alias (alias_name),
        INDEX idx_artist (artist_id),
        FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS merge_duplicate_log (
        id              BIGINT AUTO_INCREMENT PRIMARY KEY,
        artist_id       INT NOT NULL,
        kept_post_id    VARCHAR(64) NOT NULL,
        kept_filename   VARCHAR(255),
        removed_post_id VARCHAR(64) NOT NULL,
        removed_filename VARCHAR(255),
        hamming_distance SMALLINT UNSIGNED,
        created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_artist (artist_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;



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
    INDEX idx_artist (artist_id),
    INDEX idx_post_id (post_id),
    INDEX idx_subfolder (subfolder),
    FOREIGN KEY (file_id)   REFERENCES files(id)   ON DELETE CASCADE,
    FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
