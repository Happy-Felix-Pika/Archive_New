-- ============================================================
-- Ядро: фонові задачі.
--
-- Досі запуск архіватора був "запусти процес і стрім stdout":
-- жодного сліду в базі, лог живе в пам'яті й зникає при
-- перезапуску. Через це, наприклад, неможливо було перевірити,
-- чи спрацювало нічне авто-оновлення.
--
-- Таблиця універсальна: ядро нічого не знає про furry. Тип
-- задачі — просто рядок, який реєструє застосунок
-- ("furry.sync", "duplicates.scan", "backup.run" тощо).
-- ============================================================

CREATE TABLE IF NOT EXISTS jobs (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    type        VARCHAR(64)  NOT NULL,            -- хто виконує: 'furry.sync' і т.п.
    status      ENUM('pending','running','done','failed','cancelled')
                NOT NULL DEFAULT 'pending',
    progress    TINYINT UNSIGNED NOT NULL DEFAULT 0,   -- 0..100
    message     VARCHAR(512),                     -- що робиться просто зараз
    params      TEXT,                             -- JSON з параметрами запуску
    result      TEXT,                             -- JSON з підсумком
    error       TEXT,
    created_by  INT,                              -- хто запустив; NULL = за розкладом
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    started_at  DATETIME,
    finished_at DATETIME,
    INDEX idx_status  (status),
    INDEX idx_type    (type),
    INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
