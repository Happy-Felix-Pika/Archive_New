-- Переписка між архівами просто за адресою, без взаємного підключення ключами.
--   peer_url      — адреса архіву співрозмовника (https://…);
--   deliver_token — sha256 одноразового токена надісланого повідомлення: архів
--                   отримувача перевіряє ним, що лист справді від цієї адреси.
ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS peer_url      VARCHAR(512) NULL,
    ADD COLUMN IF NOT EXISTS deliver_token CHAR(64)     NULL,
    ADD INDEX IF NOT EXISTS idx_deliver_token (deliver_token);

-- Заблоковані архіви (кожен користувач блокує для себе)
CREATE TABLE IF NOT EXISTS chat_blocked (
    owner_user_id INT          NOT NULL,
    peer_url      VARCHAR(255) NOT NULL,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (owner_user_id, peer_url)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
