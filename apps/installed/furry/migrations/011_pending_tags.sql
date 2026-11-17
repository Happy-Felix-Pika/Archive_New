-- Теги нового файлу в черзі «На перевірку» (JSON: список або {категорія: [..]}),
-- щоб після «Зберегти новий» / «Замінити старий» файл не лишався без тегів.
ALTER TABLE pending_duplicates
    ADD COLUMN IF NOT EXISTS tags TEXT NULL;
