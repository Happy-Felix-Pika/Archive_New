-- Власні джерела: окрема таблиця з однозначним іменем.
-- Ім'я live_sources у деяких базах уже було зайняте (з іншими колонками),
-- тому CREATE TABLE IF NOT EXISTS мовчки нічого не робив, а запити падали
-- з "Unknown column 'label'". Тут ім'я гарантовано вільне.
CREATE TABLE IF NOT EXISTS live_custom_sources (
    id         VARCHAR(32) PRIMARY KEY,
    label      VARCHAR(120) NOT NULL,
    kind       VARCHAR(16)  NOT NULL,      -- booru / json / rss
    config     TEXT,                        -- JSON з адресами й полями
    created_by INT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
