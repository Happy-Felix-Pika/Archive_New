-- Акаунт Telegram кожного користувача архіву. api_hash і рядок сесії —
-- лише зашифровані (ключ — секрет сервера на томі /data, не в базі).
CREATE TABLE IF NOT EXISTS telegram_accounts (
    user_id       INT PRIMARY KEY,
    api_id        INT NOT NULL,
    api_hash_enc  TEXT NOT NULL,
    session_enc   TEXT NULL,
    phone         VARCHAR(32),
    tg_name       VARCHAR(255),
    updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Канал → автор архіву. Читається акаунтом того, хто канал додав.
-- last_message_id — до якого повідомлення вже все переглянуто.
CREATE TABLE IF NOT EXISTS telegram_channels (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    user_id          INT NOT NULL,
    channel_ref      VARCHAR(255) NOT NULL,
    channel_id       BIGINT NOT NULL,
    username         VARCHAR(255) NULL,
    title            VARCHAR(255) NULL,
    artist_id        INT NOT NULL,
    only_telegram    TINYINT NOT NULL DEFAULT 0,
    enabled          TINYINT NOT NULL DEFAULT 1,
    last_message_id  BIGINT NOT NULL DEFAULT 0,
    last_checked_at  DATETIME NULL,
    last_error       VARCHAR(500) NULL,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_user_channel (user_id, channel_id),
    INDEX idx_artist (artist_id),
    FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
