"""
Пули (серії): окремі таблиці, звідки все про пули й читається.

    furry_pools       — один пул = один рядок (автор, назва для показу,
                        сайт, скільки сторінок у архіві — cnt);
    furry_pool_names  — усі назви, під якими цей пул трапляється: назва
                        теки, назва на e621, на Inkbunny… Різні написання
                        тієї самої серії («before_he_left» і «Before He Left
                        (ashzxuxu)») ведуть на ОДИН пул;
    furry_pool_items  — сторінки: пул → файл → номер сторінки. Одна робота
                        в кількох пулах — кілька рядків на той самий файл.

Фізично файл лежить в одній теці (furry_posts.subfolder — «рідний» пул);
такі сторінки позначені via_folder=1 і підтягуються з furry_posts самі
(sync_file / reconcile_artist). Решта (via_folder=0) — додаткові пули.

Модуль без Flask і без ядра — ним користуються і сервер, і архіватор
(окремий процес підвантажує цей файл за шляхом). Усі функції приймають
курсор з DictCursor; коміт — справа того, хто викликає.
"""

import re

_SPLIT = re.compile(r"[^\w]+", re.UNICODE)


def _letters(s: str) -> str:
    return _SPLIT.sub("", (s or "").lower())


def norm_name(name: str, artist: str = "") -> str:
    """
    Ключ для порівняння назв пулу: без регістру, підкреслень і розділових
    знаків, без хвоста з ніком автора — «(ashzxuxu)», «[by ashzxuxu]»,
    «by ashzxuxu». Хвіст у дужках прибирається ЛИШЕ коли там нік автора:
    «Part (2)» і «Part (3)» лишаються різними пулами.
    """
    s = (name or "").lower().replace("_", " ").strip()
    a = _letters((artist or "").lstrip("@"))
    while a:
        m = re.search(r"\s*[\(\[]([^)\]]*)[\)\]]\s*$", s)
        if m and a in _letters(m.group(1)):
            s = s[:m.start()].rstrip()
            continue
        m = re.search(r"\s+by\s+@?(\S+)\s*$", s)
        if m and a in _letters(m.group(1)):
            s = s[:m.start()].rstrip()
            continue
        break
    return " ".join(_SPLIT.sub(" ", s).split())


def _artist_name(cur, artist_id) -> str:
    cur.execute("SELECT artist_name, folder_name FROM artists WHERE id=%s", (artist_id,))
    r = cur.fetchone()
    return (r["artist_name"] or r["folder_name"] or "") if r else ""


def find_pool(cur, artist_id, name):
    """id пулу за будь-якою з його назв (точно або за ключем) — або None."""
    if not name:
        return None
    cur.execute("SELECT pool_id FROM furry_pool_names WHERE artist_id=%s AND name=%s", (artist_id, name))
    r = cur.fetchone()
    if r:
        return r["pool_id"]
    artist = _artist_name(cur, artist_id)
    key = norm_name(name, artist)
    if not key:
        return None
    cur.execute("SELECT name, pool_id FROM furry_pool_names WHERE artist_id=%s", (artist_id,))
    for r in list(cur.fetchall()):
        if norm_name(r["name"], artist) == key:
            return r["pool_id"]
    return None


def ensure_pool(cur, artist_id, name, source=None) -> int | None:
    """id пулу для цієї назви; нова назва тієї самої серії стає ще одним
    іменем наявного пулу, а не окремим пулом."""
    if not name or not artist_id:
        return None
    pid = find_pool(cur, artist_id, name)
    if pid is None:
        cur.execute("INSERT IGNORE INTO furry_pools (artist_id, name, source) VALUES (%s,%s,%s)",
                    (artist_id, name, source))
        cur.execute("SELECT id FROM furry_pools WHERE artist_id=%s AND name=%s", (artist_id, name))
        pid = cur.fetchone()["id"]
    cur.execute("INSERT IGNORE INTO furry_pool_names (artist_id, name, pool_id) VALUES (%s,%s,%s)",
                (artist_id, name, pid))
    return pid


def refresh_counts(cur, pool_ids):
    ids = [int(i) for i in set(pool_ids or []) if i]
    if not ids:
        return
    ph = ",".join(["%s"] * len(ids))
    cur.execute(f"""UPDATE furry_pools p
                    SET cnt = (SELECT COUNT(*) FROM furry_pool_items i WHERE i.pool_id = p.id)
                    WHERE p.id IN ({ph})""", ids)


def add_item(cur, pool_id, file_id, page=None, via_folder=False) -> bool:
    """Сторінка в пулі. Повертає True, якщо файлу в цьому пулі ще не було."""
    if not pool_id or not file_id:
        return False
    if via_folder:
        cur.execute("""INSERT INTO furry_pool_items (pool_id, file_id, page, via_folder) VALUES (%s,%s,%s,1)
                       ON DUPLICATE KEY UPDATE page=VALUES(page), via_folder=1""", (pool_id, file_id, page))
    else:
        cur.execute("""INSERT INTO furry_pool_items (pool_id, file_id, page, via_folder) VALUES (%s,%s,%s,0)
                       ON DUPLICATE KEY UPDATE page=COALESCE(page, VALUES(page))""", (pool_id, file_id, page))
    added = cur.rowcount == 1
    refresh_counts(cur, [pool_id])
    return added


def link(cur, file_id, artist_id, name, page=None, source=None) -> bool:
    """Додати роботу ще й у пул «name» (без копії файлу)."""
    pid = ensure_pool(cur, artist_id, name, source)
    return add_item(cur, pid, file_id, page) if pid else False


def unlink(cur, file_id, pool_id):
    cur.execute("DELETE FROM furry_pool_items WHERE pool_id=%s AND file_id=%s", (pool_id, file_id))
    refresh_counts(cur, [pool_id])


def file_pool_ids(cur, file_id) -> set:
    cur.execute("SELECT pool_id FROM furry_pool_items WHERE file_id=%s", (file_id,))
    return {r["pool_id"] for r in cur.fetchall()}


def file_pools(cur, file_id) -> list:
    """Усі пули файлу одним запитом: «рідний» першим."""
    cur.execute("""SELECT p.id, p.name, p.artist_id, p.cnt, i.page, i.via_folder
                   FROM furry_pool_items i JOIN furry_pools p ON p.id = i.pool_id
                   WHERE i.file_id = %s
                   ORDER BY i.via_folder DESC, p.name""", (file_id,))
    return [{"pool_id": r["id"], "name": r["name"], "artist_id": r["artist_id"], "order": r["page"],
             "total": int(r["cnt"] or 0), "linked": not r["via_folder"]} for r in cur.fetchall()]


def set_page(cur, file_id, pool_id, page):
    """Номер сторінки в конкретному пулі. Для «рідного» — і в furry_posts."""
    cur.execute("UPDATE furry_pool_items SET page=%s WHERE pool_id=%s AND file_id=%s", (page, pool_id, file_id))
    cur.execute("SELECT via_folder FROM furry_pool_items WHERE pool_id=%s AND file_id=%s", (pool_id, file_id))
    r = cur.fetchone()
    if r and r["via_folder"]:
        cur.execute("UPDATE furry_posts SET pool_order=%s WHERE file_id=%s", (page, file_id))


def sync_file(cur, file_id):
    """
    Привести «рідний» пул файлу у відповідність до furry_posts (тека й номер
    сторінки). Викликається після будь-якої зміни subfolder/pool_order.
    """
    cur.execute("SELECT artist_id, subfolder, pool_order, source FROM furry_posts WHERE file_id=%s", (file_id,))
    fp = cur.fetchone()
    pid = None
    if fp and fp["subfolder"]:
        pid = ensure_pool(cur, fp["artist_id"], fp["subfolder"], fp["source"])
    cur.execute("SELECT pool_id FROM furry_pool_items WHERE file_id=%s AND via_folder=1", (file_id,))
    stale = [r["pool_id"] for r in cur.fetchall() if r["pool_id"] != pid]
    for old in stale:
        cur.execute("DELETE FROM furry_pool_items WHERE pool_id=%s AND file_id=%s AND via_folder=1", (old, file_id))
    if pid:
        cur.execute("""INSERT INTO furry_pool_items (pool_id, file_id, page, via_folder) VALUES (%s,%s,%s,1)
                       ON DUPLICATE KEY UPDATE page=VALUES(page), via_folder=1""", (pid, file_id, fp["pool_order"]))
    refresh_counts(cur, stale + [pid])


def sync_files(cur, file_ids):
    for fid in file_ids or []:
        sync_file(cur, fid)


def drop_empty(cur, artist_id=None):
    if artist_id:
        cur.execute("DELETE FROM furry_pools WHERE artist_id=%s AND cnt=0", (artist_id,))
    else:
        cur.execute("DELETE FROM furry_pools WHERE cnt=0")


def reconcile_artist(cur, artist_id):
    """
    Звірка всіх «рідних» пулів автора з furry_posts (страховка на випадок,
    якщо якесь місце змінило теку файлу й не викликало sync_file).
    Іде лише по файлах цього автора — по індексу.
    """
    cur.execute("""SELECT DISTINCT subfolder, MIN(source) AS source FROM furry_posts
                   WHERE artist_id=%s AND subfolder IS NOT NULL AND subfolder <> ''
                   GROUP BY subfolder""", (artist_id,))
    for r in cur.fetchall():
        ensure_pool(cur, artist_id, r["subfolder"], r["source"])
    cur.execute("""INSERT INTO furry_pool_items (pool_id, file_id, page, via_folder)
                   SELECT n.pool_id, fp.file_id, fp.pool_order, 1
                   FROM furry_posts fp
                   JOIN furry_pool_names n ON n.artist_id = fp.artist_id AND n.name = fp.subfolder
                   WHERE fp.artist_id = %s AND fp.subfolder IS NOT NULL AND fp.subfolder <> ''
                   ON DUPLICATE KEY UPDATE page = VALUES(page), via_folder = 1""", (artist_id,))
    cur.execute("""DELETE i FROM furry_pool_items i
                   JOIN furry_pools p ON p.id = i.pool_id
                   WHERE p.artist_id = %s AND i.via_folder = 1
                     AND NOT EXISTS (SELECT 1 FROM furry_posts fp
                                     JOIN furry_pool_names n ON n.artist_id = fp.artist_id AND n.name = fp.subfolder
                                     WHERE fp.file_id = i.file_id AND n.pool_id = i.pool_id)""", (artist_id,))
    cur.execute("""UPDATE furry_pools p
                   SET cnt = (SELECT COUNT(*) FROM furry_pool_items i WHERE i.pool_id = p.id)
                   WHERE p.artist_id = %s""", (artist_id,))
    drop_empty(cur, artist_id)


def merge_duplicate_pools(cur, artist_id) -> int:
    """
    Склеїти пули автора, назви яких — та сама серія (різне написання на
    різних сайтах). Сторінки й назви переїжджають у пул з більшою кількістю
    сторінок. Повертає, скільки пулів склеєно.
    """
    artist = _artist_name(cur, artist_id)
    cur.execute("SELECT id, name, cnt FROM furry_pools WHERE artist_id=%s ORDER BY cnt DESC, id", (artist_id,))
    keep = {}
    merged = 0
    for p in cur.fetchall():
        key = norm_name(p["name"], artist)
        if key not in keep:
            keep[key] = p["id"]
            continue
        target = keep[key]
        cur.execute("""INSERT INTO furry_pool_items (pool_id, file_id, page, via_folder)
                       SELECT %s, x.file_id, x.page, x.via_folder FROM furry_pool_items x WHERE x.pool_id=%s
                       ON DUPLICATE KEY UPDATE page = COALESCE(furry_pool_items.page, VALUES(page)),
                                               via_folder = GREATEST(furry_pool_items.via_folder, VALUES(via_folder))""",
                    (target, p["id"]))
        cur.execute("UPDATE furry_pool_names SET pool_id=%s WHERE pool_id=%s", (target, p["id"]))
        cur.execute("DELETE FROM furry_pools WHERE id=%s", (p["id"],))
        refresh_counts(cur, [target])
        merged += 1
    return merged


def move_artist_pools(cur, source_id, target_id):
    """Злиття авторів: пули старого автора переходять до нового; та сама
    серія в обох — склеюється в один пул."""
    cur.execute("SELECT id, name FROM furry_pools WHERE artist_id=%s", (source_id,))
    for p in cur.fetchall():
        target = find_pool(cur, target_id, p["name"])
        cur.execute("SELECT name FROM furry_pool_names WHERE pool_id=%s", (p["id"],))
        names = [r["name"] for r in cur.fetchall()]
        if target:
            cur.execute("""INSERT INTO furry_pool_items (pool_id, file_id, page, via_folder)
                           SELECT %s, x.file_id, x.page, x.via_folder FROM furry_pool_items x WHERE x.pool_id=%s
                           ON DUPLICATE KEY UPDATE page = COALESCE(furry_pool_items.page, VALUES(page))""",
                        (target, p["id"]))
            cur.execute("DELETE FROM furry_pools WHERE id=%s", (p["id"],))
        else:
            cur.execute("UPDATE furry_pools SET artist_id=%s WHERE id=%s", (target_id, p["id"]))
            cur.execute("DELETE FROM furry_pool_names WHERE pool_id=%s", (p["id"],))
            target = p["id"]
        for n in names:
            cur.execute("INSERT IGNORE INTO furry_pool_names (artist_id, name, pool_id) VALUES (%s,%s,%s)",
                        (target_id, n, target))
        refresh_counts(cur, [target])


def import_legacy_links(cur):
    """Разова міграція: старі додаткові пули (furry_pool_links) → сторінки."""
    cur.execute("SHOW TABLES LIKE 'furry_pool_links'")
    if not cur.fetchone():
        return 0
    cur.execute("SELECT file_id, artist_id, pool_name, pool_order, source FROM furry_pool_links")
    rows = cur.fetchall()
    for r in rows:
        link(cur, r["file_id"], r["artist_id"], r["pool_name"], r["pool_order"], r["source"])
    cur.execute("DROP TABLE furry_pool_links")
    return len(rows)


def reconcile_all(db, log=print):
    """Звірка всіх авторів (старт сервера, кінець роботи архіватора)."""
    with db.cursor() as cur:
        imported = import_legacy_links(cur)
        cur.execute("""SELECT id FROM artists a WHERE EXISTS (SELECT 1 FROM furry_posts fp WHERE fp.artist_id = a.id)
                       OR EXISTS (SELECT 1 FROM furry_pools p WHERE p.artist_id = a.id)""")
        ids = [r["id"] for r in cur.fetchall()]
    db.commit()
    merged = 0
    for aid in ids:
        with db.cursor() as cur:
            reconcile_artist(cur, aid)
            merged += merge_duplicate_pools(cur, aid)
        db.commit()
    if imported or merged:
        log(f"  ✓ Пули: перенесено {imported} додаткових привʼязок, склеєно дублів {merged}")
    return merged
