-- ============================================================
-- Етап 1, фаза «подвійний запис».
--
-- Тримає files / furry_posts / file_phash в актуальному стані,
-- поки джерелом правди лишається downloads. Код НЕ ЗМІНЮЄТЬСЯ:
-- і веб-сервер, і окремий процес furry_archiver.py пишуть у
-- downloads як раніше, а нові таблиці оновлюються самі.
--
-- Це ТИМЧАСОВІ риштування. На кроці 4, коли downloads зникне,
-- усі ці тригери видаляються (див. кінець файлу).
--
-- Запускати ПІСЛЯ 001_files_split.sql. Повторний запуск безпечний.
-- ============================================================

-- ── Спершу: знімаємо унікальність шляху на час риштувань ────
-- Поки джерело правди — downloads, вона дозволяє двом різним записам
-- мати однакове ім'я файлу в одній теці. Якщо лишити UNIQUE на шляху,
-- тригер не зможе створити рядок з потрібним id, і вставка в downloads
-- ВПАДЕ — тобто зламається скачування. Унікальність повернемо на
-- кроці 4, коли downloads зникне (конфлікти до того показує
-- 001_verify.py, пункт 5).
ALTER TABLE files DROP INDEX IF EXISTS uniq_path;
ALTER TABLE files ADD INDEX IF NOT EXISTS idx_path (storage_id, path_hash);

DROP TRIGGER IF EXISTS downloads_ai;
DROP TRIGGER IF EXISTS downloads_au;
DROP TRIGGER IF EXISTS downloads_ad;
DROP TRIGGER IF EXISTS artists_bd;
DROP TRIGGER IF EXISTS artists_au;

DELIMITER $$

-- ── Новий рядок у downloads ─────────────────────────────────
CREATE TRIGGER downloads_ai AFTER INSERT ON downloads
FOR EACH ROW
BEGIN
    DECLARE v_folder VARCHAR(255);
    DECLARE v_path   VARCHAR(1024);

    IF NEW.filename IS NOT NULL AND NEW.filename <> '' THEN
        SELECT folder_name INTO v_folder FROM artists WHERE id = NEW.artist_id;

        SET v_path = CONCAT(
            CASE WHEN v_folder = '_root' THEN '' ELSE CONCAT(v_folder, '/') END,
            CASE WHEN NEW.subfolder IS NULL OR NEW.subfolder = '' THEN '' ELSE CONCAT(NEW.subfolder, '/') END,
            NEW.filename);

        -- ON DUPLICATE KEY, щоб конфлікт шляхів НІКОЛИ не ламав
        -- вставку в downloads: архів має працювати за будь-яких умов.
        INSERT INTO files (id, storage_id, rel_path, name, ext, size_bytes,
                           width, height, md5_hash, created_at)
        VALUES (NEW.id, 'default', v_path, NEW.filename, NEW.file_ext, NEW.size_bytes,
                NEW.width, NEW.height, NEW.md5_hash, NEW.downloaded_at)
        ON DUPLICATE KEY UPDATE
            rel_path   = VALUES(rel_path),
            name       = VALUES(name),
            ext        = VALUES(ext),
            size_bytes = VALUES(size_bytes),
            width      = VALUES(width),
            height     = VALUES(height),
            md5_hash   = VALUES(md5_hash);

        INSERT INTO furry_posts (file_id, artist_id, post_id, source, post_url,
                                 page_num, subfolder, pool_order, downloaded_at)
        VALUES (NEW.id, NEW.artist_id, NEW.post_id, NEW.source, NEW.post_url,
                NEW.page_num, NEW.subfolder, NEW.pool_order, NEW.downloaded_at)
        ON DUPLICATE KEY UPDATE
            file_id    = VALUES(file_id),
            source     = VALUES(source),
            post_url   = VALUES(post_url),
            subfolder  = VALUES(subfolder),
            pool_order = VALUES(pool_order);

        IF NEW.phash IS NOT NULL THEN
            INSERT INTO file_phash (file_id, phash) VALUES (NEW.id, NEW.phash)
            ON DUPLICATE KEY UPDATE phash = VALUES(phash);
        END IF;
    END IF;
END$$

-- ── Зміна рядка в downloads ─────────────────────────────────
-- Перейменування файлу, переїзд у pool-підпапку, злиття авторів
-- (зміна artist_id), дозаповнення розмірів — усе міняє шлях.
CREATE TRIGGER downloads_au AFTER UPDATE ON downloads
FOR EACH ROW
BEGIN
    DECLARE v_folder VARCHAR(255);
    DECLARE v_path   VARCHAR(1024);

    IF NEW.filename IS NOT NULL AND NEW.filename <> '' THEN
        SELECT folder_name INTO v_folder FROM artists WHERE id = NEW.artist_id;

        SET v_path = CONCAT(
            CASE WHEN v_folder = '_root' THEN '' ELSE CONCAT(v_folder, '/') END,
            CASE WHEN NEW.subfolder IS NULL OR NEW.subfolder = '' THEN '' ELSE CONCAT(NEW.subfolder, '/') END,
            NEW.filename);

        INSERT INTO files (id, storage_id, rel_path, name, ext, size_bytes,
                           width, height, md5_hash, created_at)
        VALUES (NEW.id, 'default', v_path, NEW.filename, NEW.file_ext, NEW.size_bytes,
                NEW.width, NEW.height, NEW.md5_hash, NEW.downloaded_at)
        ON DUPLICATE KEY UPDATE
            rel_path   = VALUES(rel_path),
            name       = VALUES(name),
            ext        = VALUES(ext),
            size_bytes = VALUES(size_bytes),
            width      = VALUES(width),
            height     = VALUES(height),
            md5_hash   = VALUES(md5_hash);

        INSERT INTO furry_posts (file_id, artist_id, post_id, source, post_url,
                                 page_num, subfolder, pool_order, downloaded_at)
        VALUES (NEW.id, NEW.artist_id, NEW.post_id, NEW.source, NEW.post_url,
                NEW.page_num, NEW.subfolder, NEW.pool_order, NEW.downloaded_at)
        ON DUPLICATE KEY UPDATE
            file_id    = VALUES(file_id),
            source     = VALUES(source),
            post_url   = VALUES(post_url),
            subfolder  = VALUES(subfolder),
            pool_order = VALUES(pool_order);

        -- artist_id живе в furry_posts і не входить у ключ file_id,
        -- тому оновлюємо окремо (злиття авторів саме це й робить)
        UPDATE furry_posts SET artist_id = NEW.artist_id, page_num = NEW.page_num
        WHERE file_id = NEW.id;

        IF NEW.phash IS NOT NULL THEN
            INSERT INTO file_phash (file_id, phash) VALUES (NEW.id, NEW.phash)
            ON DUPLICATE KEY UPDATE phash = VALUES(phash);
        ELSE
            DELETE FROM file_phash WHERE file_id = NEW.id;
        END IF;
    END IF;
END$$

-- ── Видалення рядка з downloads ─────────────────────────────
-- furry_posts і file_phash підуть слідом самі (ON DELETE CASCADE).
CREATE TRIGGER downloads_ad AFTER DELETE ON downloads
FOR EACH ROW
BEGIN
    DELETE FROM files WHERE id = OLD.id;
END$$

-- ── Видалення автора ────────────────────────────────────────
-- ВАЖЛИВО: каскадне видалення по зовнішньому ключу (artists ->
-- downloads) у MySQL тригери НЕ запускає. Тому файли зниклих
-- завантажень треба прибрати тут, ДО каскаду.
CREATE TRIGGER artists_bd BEFORE DELETE ON artists
FOR EACH ROW
BEGIN
    DELETE FROM files WHERE id IN (SELECT id FROM downloads WHERE artist_id = OLD.id);
END$$

-- ── Перейменування теки автора ──────────────────────────────
-- Змінює шлях усім його файлам одразу.
CREATE TRIGGER artists_au AFTER UPDATE ON artists
FOR EACH ROW
BEGIN
    IF NOT (NEW.folder_name <=> OLD.folder_name) THEN
        UPDATE files f
        JOIN downloads d ON d.id = f.id
        SET f.rel_path = CONCAT(
            CASE WHEN NEW.folder_name = '_root' THEN '' ELSE CONCAT(NEW.folder_name, '/') END,
            CASE WHEN d.subfolder IS NULL OR d.subfolder = '' THEN '' ELSE CONCAT(d.subfolder, '/') END,
            d.filename)
        WHERE d.artist_id = NEW.id;
    END IF;
END$$

DELIMITER ;

-- ============================================================
-- ВІДКАТ (на кроці 4 або якщо щось піде не так):
--   DROP TRIGGER IF EXISTS downloads_ai;
--   DROP TRIGGER IF EXISTS downloads_au;
--   DROP TRIGGER IF EXISTS downloads_ad;
--   DROP TRIGGER IF EXISTS artists_bd;
--   DROP TRIGGER IF EXISTS artists_au;
-- Дані при цьому не змінюються.
-- ============================================================
