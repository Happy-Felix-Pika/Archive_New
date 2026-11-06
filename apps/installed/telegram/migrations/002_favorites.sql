-- «Улюблене» для Telegram у вкладці «Онлайн». Свого API улюбленого в
-- Telegram немає, а спільна live_favorites тримає post_id числом — у
-- Telegram же пост визначається парою (канал, повідомлення).
CREATE TABLE IF NOT EXISTS telegram_favorites (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id     INT NOT NULL,
    channel_id  BIGINT NOT NULL,
    msg_id      BIGINT NOT NULL,
    ext         VARCHAR(10),
    is_video    TINYINT NOT NULL DEFAULT 0,
    post_url    VARCHAR(512),
    title       VARCHAR(255),
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_fav (user_id, channel_id, msg_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
