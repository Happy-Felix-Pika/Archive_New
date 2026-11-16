-- Одна робота — кілька пулів.
-- Пост на сайті може входити одразу в кілька пулів (або та сама робота
-- лежить на e621 в одному пулі, а на Inkbunny — в іншому). Фізично файл
-- один і лежить у теці першого пулу (furry_posts.subfolder); решта пулів
-- посилаються на нього тут — зі своїм номером сторінки.
CREATE TABLE IF NOT EXISTS furry_pool_links (
    file_id    BIGINT       NOT NULL,
    artist_id  INT          NOT NULL,
    pool_name  VARCHAR(255) NOT NULL,
    pool_order SMALLINT UNSIGNED NULL,
    source     VARCHAR(16)  NULL,
    created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (file_id, pool_name),
    INDEX idx_pool (artist_id, pool_name),
    FOREIGN KEY (file_id)   REFERENCES files(id)   ON DELETE CASCADE,
    FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
