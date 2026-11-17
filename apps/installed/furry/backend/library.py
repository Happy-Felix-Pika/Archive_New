"""
Бібліотека furry: файл + прив'язка до автора й поста.

Ці функції раніше жили в ядрі (core/files_api.py), хоча працюють із
таблицями furry — авторами й постами. Тепер вони тут. Іншим застосункам
(дублікатам) вони доступні як сервіс 'furry.library'.
"""


def create_file_with_post(cur, *, artist_id, post_id, source, post_url, md5_hash,
                          phash, filename, file_ext, page_num=1, subfolder=None,
                          pool_order=None, width=None, height=None, size_bytes=None,
                          folder_name=None):
    """
    Створює файл у ядрі і furry-прив'язку до нього. Повертає id файлу,
    або 0 якщо такий пост у цього автора вже є (стара поведінка
    INSERT IGNORE по ключу artist_id + post_id + page_num).

    folder_name — тека автора; якщо не передали, беремо з artists.
    """
    cur.execute("""SELECT file_id FROM furry_posts
                   WHERE artist_id=%s AND post_id=%s AND page_num=%s""",
                (artist_id, post_id, page_num))
    if cur.fetchone():
        return 0

    if folder_name is None:
        cur.execute("SELECT folder_name FROM artists WHERE id=%s", (artist_id,))
        r = cur.fetchone()
        folder_name = r["folder_name"] if r else None

    parts = [] if folder_name == "_root" else [folder_name]
    if subfolder:
        parts.append(subfolder)
    parts.append(filename)
    rel_path = "/".join(p for p in parts if p)

    cur.execute("""INSERT INTO files (storage_id, rel_path, name, ext, size_bytes,
                                      width, height, md5_hash, created_at)
                   VALUES ('default',%s,%s,%s,%s,%s,%s,%s,NOW())""",
                (rel_path, filename, file_ext, size_bytes, width, height, md5_hash))
    file_id = cur.lastrowid
    cur.execute("""INSERT INTO furry_posts (file_id, artist_id, post_id, source, post_url,
                                            page_num, subfolder, pool_order, downloaded_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
                (file_id, artist_id, post_id, source, post_url, page_num, subfolder, pool_order))
    if phash is not None:
        cur.execute("""INSERT INTO file_phash (file_id, phash) VALUES (%s,%s)
                       ON DUPLICATE KEY UPDATE phash=VALUES(phash)""", (file_id, phash))
    if subfolder:
        from . import poolstore
        poolstore.sync_file(cur, file_id)
    return file_id


def resync_rel_path(db, file_ids):
    """
    Перерахувати files.rel_path після того, як файл переїхав у сховищі.

    Фізичне переміщення робить Storage API, запис про підпапку — furry-частина,
    але ЯДРО мусить знати НОВЕ місце файлу: саме з rel_path він потім
    віддається. Без цього виклику файл після переїзду в серію відкриватись
    перестає.

    Живе в ядрі, бо шлях — властивість файлу; те, ЯК він складається для
    furry-архіву (тека автора + назва серії), знає запит нижче і піде
    у furry-застосунок разом із furry_posts.
    """
    ids = [int(i) for i in file_ids if i]
    if not ids:
        return
    placeholders = ",".join(["%s"] * len(ids))
    with db.cursor() as cur:
        cur.execute(f"""
            UPDATE files f
            JOIN furry_posts fp ON fp.file_id = f.id
            JOIN artists a      ON a.id       = fp.artist_id
            SET f.rel_path = CONCAT(
                CASE WHEN a.folder_name = '_root' THEN '' ELSE CONCAT(a.folder_name, '/') END,
                CASE WHEN fp.subfolder IS NULL OR fp.subfolder = '' THEN '' ELSE CONCAT(fp.subfolder, '/') END,
                f.name)
            WHERE f.id IN ({placeholders})
        """, ids)
