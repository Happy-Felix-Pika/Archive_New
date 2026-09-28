-- Власні джерела, додані через форму (без написання застосунку).
CREATE TABLE IF NOT EXISTS live_sources (
    id         VARCHAR(32) PRIMARY KEY,
    label      VARCHAR(120) NOT NULL,
    kind       VARCHAR(16)  NOT NULL,      -- booru / json / rss
    config     TEXT,                        -- JSON з адресами й полями
    created_by INT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
