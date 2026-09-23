-- Пошук дублікатів по ВСЬОМУ архіву, а не тільки в межах одного автора.
-- Для таких пар автора немає, тож artist_id має вміти бути порожнім.
ALTER TABLE duplicate_scan_results MODIFY artist_id INT NULL;
