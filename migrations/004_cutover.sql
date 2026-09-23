-- ============================================================
-- Етап 1, ПЕРЕКЛЮЧЕННЯ. Останній крок.
--
-- Знімає риштування: тригери подвійного запису і саму таблицю
-- downloads. Після цього єдине джерело правди — files (ядро)
-- і furry_posts (furry-частина).
--
-- ⚠ ЗАПУСКАТИ ТІЛЬКИ ПІСЛЯ ТОГО, ЯК ОНОВЛЕНО КОД.
-- Порядок саме такий:
--   1. зупинити архів
--   2. замінити файли коду (blueprints/, core/, furry_archiver.py)
--   3. виконати цю міграцію
--   4. запустити архів
-- Якщо виконати її на старому коді — архів перестане працювати,
-- бо той код пише в downloads, якої вже не буде.
--
-- ⚠ ЗВОРОТНОГО ХОДУ НЕМАЄ. Зроби бекап бази перед запуском.
-- ============================================================

-- ── 1. Знімаємо тригери ─────────────────────────────────────
-- Вони дзеркалили downloads -> files. Тепер код пише в files
-- напряму, дзеркалити нічого.
DROP TRIGGER IF EXISTS downloads_ai;
DROP TRIGGER IF EXISTS downloads_au;
DROP TRIGGER IF EXISTS downloads_ad;
DROP TRIGGER IF EXISTS artists_bd;
DROP TRIGGER IF EXISTS artists_au;

-- ── 2. Остання звірка перед видаленням ──────────────────────
-- Якщо тут не 0 — НЕ видаляй downloads, спершу розберись:
-- значить якийсь запис не доїхав у files.
SELECT COUNT(*) AS 'НЕ_ПЕРЕНЕСЕНО_РЯДКІВ'
FROM downloads d
LEFT JOIN files f ON f.id = d.id
WHERE f.id IS NULL AND d.filename IS NOT NULL AND d.filename <> '';

-- ── 3. Прибираємо стару таблицю ─────────────────────────────
DROP TABLE downloads;

-- ── 4. Повертаємо унікальність шляху ────────────────────────
-- Знімали її в 002, бо downloads дозволяла двом записам мати
-- однакове ім'я в одній теці. Тепер files — джерело правди,
-- і два файли за одним шляхом означають помилку.
--
-- Якщо цей крок впаде з 'Duplicate entry' — у тебе є файли з
-- однаковим шляхом. Знайти їх:
--   SELECT rel_path, COUNT(*) c FROM files
--   GROUP BY storage_id, path_hash HAVING c > 1;
-- Розберись з ними (видали зайві) і повтори лише цей ALTER.
ALTER TABLE files DROP INDEX IF EXISTS idx_path;
ALTER TABLE files ADD UNIQUE KEY uniq_path (storage_id, path_hash);
