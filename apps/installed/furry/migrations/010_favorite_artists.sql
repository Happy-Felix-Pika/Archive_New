-- Улюблені автори — у кожного користувача свої.
CREATE TABLE IF NOT EXISTS furry_favorite_artists (
    user_id    INT      NOT NULL,
    artist_id  INT      NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, artist_id),
    FOREIGN KEY (user_id)   REFERENCES users(id)   ON DELETE CASCADE,
    FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
