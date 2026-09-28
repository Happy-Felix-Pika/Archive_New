-- Улюблене з зовнішніх сайтів — для джерел без власного API улюбленого.
-- Раніше таблицю створював furry; на наявній базі тут нічого не станеться.

CREATE TABLE IF NOT EXISTS live_favorites (
        id           BIGINT AUTO_INCREMENT PRIMARY KEY,
        source       VARCHAR(20) NOT NULL,
        post_id      BIGINT NOT NULL,
        artist_name  VARCHAR(255),
        thumb_url    TEXT,
        file_url     TEXT,
        post_url     TEXT,
        is_video     TINYINT(1) DEFAULT 0,
        ext          VARCHAR(10),
        created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_fav (source, post_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
