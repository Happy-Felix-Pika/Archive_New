-- Вкладення (файл з архіву) у повідомленні: JSON {name, ext, url}
ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS attachment TEXT NULL;

-- Нотатки: свої записи й збережені повідомлення (як «Обране» в Telegram)
CREATE TABLE IF NOT EXISTS chat_notes (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    owner_user_id INT NOT NULL,
    body          TEXT NOT NULL,
    attachment    TEXT NULL,
    source_label  VARCHAR(255) NULL,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_owner (owner_user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Файли, надіслані в чат: доступ за неосяжним посиланням (токен — лише sha256)
CREATE TABLE IF NOT EXISTS chat_shares (
    token_hash    CHAR(64) PRIMARY KEY,
    file_id       BIGINT NOT NULL,
    owner_user_id INT NOT NULL,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
