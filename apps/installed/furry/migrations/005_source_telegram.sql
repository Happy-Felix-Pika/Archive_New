-- Третє джерело — Telegram (застосунок telegram). ENUM розширюємо,
-- старі значення лишаються як були.
ALTER TABLE furry_posts MODIFY source ENUM('e621','inkbunny','telegram') NULL;
ALTER TABLE pending_duplicates MODIFY source ENUM('e621','inkbunny','telegram') NULL;
