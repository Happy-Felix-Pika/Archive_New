-- «Прочитано»: для вихідних — коли співрозмовник відкрив розмову
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS read_at DATETIME NULL;
-- Одноразові токени, якими наш архів підтверджує свої службові запити (квитанції про прочитання)
CREATE TABLE IF NOT EXISTS chat_verify_tokens (
    token_hash    CHAR(64) PRIMARY KEY,
    owner_user_id INT NOT NULL,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
