-- Таблиці застосунку. Раніше їх створювало ядро.
-- IF NOT EXISTS: на наявній базі нічого не станеться.

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
        matched_folder_name VARCHAR(255),
        hamming_distance TINYINT,
        created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_artist (artist_id),
        FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS rejected_duplicates (
        id          BIGINT AUTO_INCREMENT PRIMARY KEY,
        artist_id   INT NOT NULL,
        post_id     VARCHAR(64) NOT NULL,
        page_num    TINYINT DEFAULT 1,
        md5_hash    CHAR(32),
        rejected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_post (artist_id, post_id, page_num),
        INDEX idx_md5 (md5_hash),
        FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE
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

CREATE TABLE IF NOT EXISTS duplicate_ignore_pairs (
        id                BIGINT AUTO_INCREMENT PRIMARY KEY,
        download_id_a     BIGINT NOT NULL,
        download_id_b     BIGINT NOT NULL,
        created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_ignore_pair (download_id_a, download_id_b),
        FOREIGN KEY (download_id_a) REFERENCES files(id) ON DELETE CASCADE,
        FOREIGN KEY (download_id_b) REFERENCES files(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS file_phash (
    file_id BIGINT PRIMARY KEY,
    phash   BIGINT UNSIGNED,
    INDEX idx_phash (phash),
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
