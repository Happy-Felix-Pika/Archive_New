-- ============================================================
-- Етап 1, підготовка до зняття риштувань.
--
-- Теги, улюблене й таблиці дублікатів досі прив'язані зовнішнім
-- ключем до downloads. Поки це так, downloads не можна прибрати.
-- Оскільки files.id = downloads.id, перевести ключі на files
-- можна без жодної зміни даних.
--
-- Запускати ПІСЛЯ 001 і 002. Повторний запуск безпечний.
-- ============================================================

-- Спершу прибираємо рядки-сироти: якщо на щось немає файлу в ядрі,
-- новий зовнішній ключ просто не створиться.
DELETE dt FROM download_tags dt
    LEFT JOIN files f ON f.id = dt.download_id WHERE f.id IS NULL;
DELETE uf FROM user_favorites uf
    LEFT JOIN files f ON f.id = uf.download_id WHERE f.id IS NULL;
DELETE x FROM duplicate_scan_results x
    LEFT JOIN files a ON a.id = x.download_id_a
    LEFT JOIN files b ON b.id = x.download_id_b
    WHERE a.id IS NULL OR b.id IS NULL;
DELETE x FROM duplicate_ignore_pairs x
    LEFT JOIN files a ON a.id = x.download_id_a
    LEFT JOIN files b ON b.id = x.download_id_b
    WHERE a.id IS NULL OR b.id IS NULL;

-- Теги файлу
ALTER TABLE download_tags DROP FOREIGN KEY download_tags_ibfk_1;
ALTER TABLE download_tags
    ADD CONSTRAINT download_tags_file_fk
    FOREIGN KEY (download_id) REFERENCES files(id) ON DELETE CASCADE;

-- Улюблене користувача
ALTER TABLE user_favorites DROP FOREIGN KEY user_favorites_ibfk_1;
ALTER TABLE user_favorites
    ADD CONSTRAINT user_favorites_file_fk
    FOREIGN KEY (download_id) REFERENCES files(id) ON DELETE CASCADE;

-- Результати пошуку дублікатів
ALTER TABLE duplicate_scan_results DROP FOREIGN KEY duplicate_scan_results_ibfk_1;
ALTER TABLE duplicate_scan_results DROP FOREIGN KEY duplicate_scan_results_ibfk_2;
ALTER TABLE duplicate_scan_results
    ADD CONSTRAINT duplicate_scan_a_file_fk
    FOREIGN KEY (download_id_a) REFERENCES files(id) ON DELETE CASCADE,
    ADD CONSTRAINT duplicate_scan_b_file_fk
    FOREIGN KEY (download_id_b) REFERENCES files(id) ON DELETE CASCADE;

-- Пари, позначені як "це не дублікат"
ALTER TABLE duplicate_ignore_pairs DROP FOREIGN KEY duplicate_ignore_pairs_ibfk_1;
ALTER TABLE duplicate_ignore_pairs DROP FOREIGN KEY duplicate_ignore_pairs_ibfk_2;
ALTER TABLE duplicate_ignore_pairs
    ADD CONSTRAINT duplicate_ignore_a_file_fk
    FOREIGN KEY (download_id_a) REFERENCES files(id) ON DELETE CASCADE,
    ADD CONSTRAINT duplicate_ignore_b_file_fk
    FOREIGN KEY (download_id_b) REFERENCES files(id) ON DELETE CASCADE;
