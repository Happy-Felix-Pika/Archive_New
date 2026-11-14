-- Картки e621 / Inkbunny на «Огляді» рахують пости за джерелом —
-- без індексу кожен підрахунок проходив усю таблицю.
CREATE INDEX IF NOT EXISTS idx_source ON furry_posts (source);
