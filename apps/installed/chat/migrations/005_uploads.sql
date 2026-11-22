-- Файли «з ПК» для чату: лежать у службовій теці сховища .chat_uploads (поза архівом)
CREATE TABLE IF NOT EXISTS chat_uploads (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    owner_user_id INT NOT NULL,
    rel_path      VARCHAR(512) NOT NULL,
    name          VARCHAR(255) NOT NULL,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
ALTER TABLE chat_shares MODIFY file_id BIGINT NULL;
ALTER TABLE chat_shares ADD COLUMN IF NOT EXISTS upload_id BIGINT NULL;
