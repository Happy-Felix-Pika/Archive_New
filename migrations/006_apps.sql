-- ============================================================
-- Ядро: система застосунків.
--
-- Три невеликі таблиці — більше для цього нічого не треба.
--   apps            — що встановлено і в якому стані
--   app_migrations  — які SQL-міграції застосунку вже виконані
--   app_settings    — налаштування застосунку, кожен у своєму кутку
--
-- Статуси 'installed' і 'update available' навмисно не зберігаємо:
-- перше — це просто наявність рядка, друге рахується порівнянням
-- версій. Зберігати те, що обчислюється, — джерело розсинхронів.
-- ============================================================

CREATE TABLE IF NOT EXISTS apps (
    id            VARCHAR(64) PRIMARY KEY,        -- збігається з ім'ям теки
    version       VARCHAR(32) NOT NULL,
    status        ENUM('enabled','disabled','incompatible','error')
                  NOT NULL DEFAULT 'enabled',
    error_message TEXT,                           -- чому саме не завантажився
    installed_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS app_migrations (
    app_id     VARCHAR(64)  NOT NULL,
    filename   VARCHAR(255) NOT NULL,
    applied_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (app_id, filename)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS app_settings (
    app_id VARCHAR(64)  NOT NULL,
    `key`  VARCHAR(128) NOT NULL,
    value  TEXT,
    PRIMARY KEY (app_id, `key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
