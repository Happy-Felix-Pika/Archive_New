-- «На перевірку» знає, чи новий файл — сторінка pool (серії) на сайті.
--   pool_folder / pool_order — назва pool і номер сторінки нового файлу;
--   pool_priority = 1 — новий у pool, а збережений ні, і це та сама робота:
--   такі пари замінюються автоматично після завершення архіватора.
ALTER TABLE pending_duplicates
    ADD COLUMN IF NOT EXISTS pool_folder   VARCHAR(255) NULL,
    ADD COLUMN IF NOT EXISTS pool_order    INT NULL,
    ADD COLUMN IF NOT EXISTS pool_priority TINYINT NOT NULL DEFAULT 0;
