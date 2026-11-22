-- Чат через взаємне підключення архівів прибрано: лишаємо тільки «за адресою».
-- Старі розмови переносимо на адресу того архіву, решту (архів уже відключено) — прибираємо.
UPDATE chat_messages m JOIN external_archives ea ON ea.id = m.peer_archive_id
   SET m.peer_url = LOWER(TRIM(TRAILING '/' FROM ea.base_url)), m.peer_archive_id = NULL
 WHERE m.peer_archive_id IS NOT NULL AND m.peer_url IS NULL;
DELETE FROM chat_messages WHERE peer_archive_id IS NOT NULL AND peer_url IS NULL;
