-- Черга ручного розбору і список свідомих відмов.
--
-- На старих базах ці таблиці вже є — їх колись створювало ядро.
-- На чистій установці їх не створював ніхто, і архіватор падав на
-- першому ж пості, бо is_rejected() читає rejected_duplicates.
--
-- rejected_duplicates заповнюється двома шляхами: користувач натиснув
-- «це дублікат» у черзі, або архіватор сам відкинув роботу, яка вже є
-- з іншого джерела.

CREATE TABLE IF NOT EXISTS pending_duplicates (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    artist_id           INT NOT NULL,
    post_id             VARCHAR(64) NOT NULL,
    source              ENUM('e621','inkbunny'),
    post_url            VARCHAR(512),
    md5_hash            CHAR(32),
    phash               BIGINT UNSIGNED,
    filename            VARCHAR(255),
    file_ext            VARCHAR(10),
    page_num            TINYINT DEFAULT 1,
    local_path          VARCHAR(1024),
    nc_target_path      VARCHAR(1024),
    matched_post_id     VARCHAR(64),
    matched_filename    VARCHAR(255),
    matched_folder_name VARCHAR(255),
    hamming_distance    SMALLINT UNSIGNED,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_post (artist_id, post_id, page_num),
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
