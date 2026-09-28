-- Таблиця чату. Раніше її створювало ядро; тепер це справа застосунку.
CREATE TABLE IF NOT EXISTS chat_messages (
        id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
        owner_user_id       INT NOT NULL,
        direction           ENUM('in','out') NOT NULL,
        peer_label          VARCHAR(255) NOT NULL,
        peer_local_user_id  INT NULL,
        peer_archive_id     VARCHAR(64) NULL,
        peer_identity       VARCHAR(255) NULL,
        body                TEXT NOT NULL,
        created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
        is_read             TINYINT(1) DEFAULT 0,
        INDEX idx_owner (owner_user_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
