-- Пул = автор + назва. Підрахунок сторінок пулу (картка арта, редактор)
-- іде саме за цією парою — окремих індексів на кожну колонку мало.
CREATE INDEX IF NOT EXISTS idx_artist_sub ON furry_posts (artist_id, subfolder);
