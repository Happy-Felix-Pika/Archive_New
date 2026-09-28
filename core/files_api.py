"""
Ядро: доступ до файлу як до самостійної сутності.

Єдине місце, яке знає, як з id файлу дістатись до самого файлу.
Нічого не знає ні про авторів, ні про пости, ні про e621 — тільки
files.rel_path і активне сховище.

Далі саме цим API користуватимуться застосунки замість того, щоб
збирати шлях з folder_name + subfolder + filename самостійно.
"""

import os

from core.db import get_db, q, qq
from core.storage import get_local_root


def get_file(file_id, db=None) -> dict | None:
    """Рядок з files, або None. db можна передати, щоб не відкривати нове з'єднання."""
    own = db is None
    if own:
        db = get_db()
    try:
        return q(db, """SELECT id, storage_id, rel_path, name, ext, size_bytes,
                               width, height, md5_hash, created_at
                        FROM files WHERE id = %s""", (file_id,))
    finally:
        if own:
            db.close()


def local_path(rel_path: str) -> str:
    """
    Абсолютний шлях у локальному сховищі для rel_path з БД.

    rel_path завжди зберігається з прямими слешами (це шлях у сховищі,
    а не в ОС), тому розбираємо його вручну — інакше на Windows
    вийде мішанина роздільників.
    """
    return os.path.join(get_local_root(), *[p for p in rel_path.split("/") if p])


def file_local_path(file_id, db=None) -> str | None:
    """Абсолютний локальний шлях до файлу за його id, або None якщо файлу немає в БД."""
    row = get_file(file_id, db)
    return local_path(row["rel_path"]) if row else None


# ═════════════════════════════════════════════════════════════════════
#  КОШИК
#
#  Видалений файл не зникає, а переїжджає в .trash усередині сховища.
#  Звідти його можна відновити або прибрати назавжди.
#
#  Чому .trash УСЕРЕДИНІ сховища, а не поруч: якщо архів у Docker, тека
#  з даними — змонтований том, а все поруч із нею живе в самому
#  контейнері й зникає при його перестворенні. Усередині — лежить на тому
#  ж диску, що й файли, і працює однаково для локального й Nextcloud.
#
#  Видалення рядка files по ланцюжку прибирає теги, улюблене й дані
#  застосунків (автор, хеш...). Щоб відновлення повертало все, перед
#  видаленням збираємо знімок:
#    * своє (рядок files, теги, улюблене, винятки ключів) — ядро саме;
#    * дані застосунків — через фільтр 'file.trash': кожен застосунок
#      дописує у знімок своє під своїм id.
#  При відновленні ядро повертає своє, а застосункам шле подію
#  'file.restored' зі знімком — і вони повертають своє.
# ═════════════════════════════════════════════════════════════════════

import json as _json

TRASH_DIR = ".trash"


class TrashError(Exception):
    """Зрозуміла причина, чому не вийшло (показується користувачу)."""


def _dt(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def trash_file(file_id, user_id=None):
    """Перемістити файл у кошик. Повертає id запису в кошику."""
    from core import events
    from core.storage_api import get_storage

    db = get_db()
    try:
        f = q(db, "SELECT * FROM files WHERE id=%s", (file_id,))
        if not f:
            raise TrashError("Файл не знайдено")

        snap = {
            "file":      {k: _dt(v) for k, v in f.items() if k != "path_hash"},
            "tags":      [r["tag_id"] for r in qq(db, "SELECT tag_id FROM download_tags WHERE download_id=%s", (file_id,))],
            "favorites": [r["user_id"] for r in qq(db, "SELECT user_id FROM user_favorites WHERE download_id=%s", (file_id,))],
            "key_exceptions": [r["key_id"] for r in qq(db, "SELECT key_id FROM key_file_exceptions WHERE file_id=%s", (file_id,))],
            "apps":      {},
        }
        # застосунки дописують своє: {"furry": {...}, "duplicates": {...}}
        snap["apps"] = events.apply_filters("file.trash", {}, file_id=file_id, db=db) or {}

        with db.cursor() as cur:
            cur.execute("""INSERT INTO trash_items
                           (file_id, name, ext, size_bytes, storage_id, original_rel_path,
                            trash_rel_path, snapshot, deleted_by)
                           VALUES (%s,%s,%s,%s,%s,%s,'',%s,%s)""",
                        (file_id, f["name"], f["ext"], f["size_bytes"], f["storage_id"],
                         f["rel_path"], _json.dumps(snap, ensure_ascii=False), user_id))
            trash_id = cur.lastrowid
        # id кошика в імені — два файли з однаковою назвою не зіткнуться
        trash_rel = f"{TRASH_DIR}/{trash_id}__{f['name']}"

        # Спершу файл, потім база: якщо перенести не вдалось — нічого не
        # видаляємо, файл лишається на місці.
        storage = get_storage()
        if storage.exists(f["rel_path"]):
            if not storage.move(f["rel_path"], trash_rel):
                db.rollback()
                raise TrashError("Не вдалося перемістити файл у кошик")
        # файлу на диску могло вже не бути — тоді кошик тримає лише запис

        with db.cursor() as cur:
            cur.execute("UPDATE trash_items SET trash_rel_path=%s WHERE id=%s", (trash_rel, trash_id))
            cur.execute("DELETE FROM files WHERE id=%s", (file_id,))
        db.commit()
    finally:
        db.close()

    events.emit("file.deleted", {"file_id": file_id, "trash_id": trash_id})
    return trash_id


def list_trash():
    db = get_db()
    try:
        rows = qq(db, """SELECT id, file_id, name, ext, size_bytes, original_rel_path,
                                deleted_by, deleted_at
                         FROM trash_items ORDER BY deleted_at DESC, id DESC""")
        for r in rows:
            r["deleted_at"] = _dt(r["deleted_at"])
        return rows
    finally:
        db.close()


def restore_trash(trash_id):
    """Повернути файл на старе місце з усім, що в нього було."""
    from core import events
    from core.storage_api import get_storage

    db = get_db()
    try:
        item = q(db, "SELECT * FROM trash_items WHERE id=%s", (trash_id,))
        if not item:
            raise TrashError("У кошику такого немає")
        snap = _json.loads(item["snapshot"] or "{}")
        fr   = snap.get("file") or {}
        orig = item["original_rel_path"]

        # на старому місці вже щось є — не затираємо
        if q(db, "SELECT id FROM files WHERE storage_id=%s AND rel_path=%s", (item["storage_id"], orig)):
            raise TrashError(f"На місці «{orig}» уже є інший файл. Видали або перейменуй його і спробуй знову.")
        if q(db, "SELECT id FROM files WHERE id=%s", (item["file_id"],)):
            raise TrashError("Цей файл уже відновлено")

        storage = get_storage()
        if storage.exists(orig):
            raise TrashError(f"У сховищі на місці «{orig}» уже лежить файл. Прибери його і спробуй знову.")
        if item["trash_rel_path"] and storage.exists(item["trash_rel_path"]):
            if not storage.move(item["trash_rel_path"], orig):
                raise TrashError("Не вдалося повернути файл з кошика")

        with db.cursor() as cur:
            cur.execute("""INSERT INTO files (id, storage_id, rel_path, name, ext, size_bytes,
                                              width, height, md5_hash, owner_user_id, created_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (item["file_id"], item["storage_id"], orig, fr.get("name") or item["name"],
                         fr.get("ext") or item["ext"], fr.get("size_bytes"), fr.get("width"),
                         fr.get("height"), fr.get("md5_hash"), fr.get("owner_user_id"),
                         fr.get("created_at")))
            # своє повертаємо лише там, де воно ще має сенс: тег могли
            # видалити, користувача — теж
            for tag_id in snap.get("tags") or []:
                cur.execute("""INSERT IGNORE INTO download_tags (download_id, tag_id)
                               SELECT %s, id FROM tags WHERE id=%s""", (item["file_id"], tag_id))
            for uid in snap.get("favorites") or []:
                cur.execute("""INSERT IGNORE INTO user_favorites (user_id, download_id)
                               SELECT id, %s FROM users WHERE id=%s""", (item["file_id"], uid))
            for kid in snap.get("key_exceptions") or []:
                cur.execute("""INSERT IGNORE INTO key_file_exceptions (key_id, file_id)
                               SELECT id, %s FROM external_access_keys WHERE id=%s""", (item["file_id"], kid))
        db.commit()

        # застосунки повертають своє. Файл уже в базі — навіть якщо якийсь
        # застосунок упаде, сам файл відновлено.
        events.emit("file.restored", {"file_id": item["file_id"], "snapshot": snap, "db": db})
        db.commit()

        with db.cursor() as cur:
            cur.execute("DELETE FROM trash_items WHERE id=%s", (trash_id,))
        db.commit()
        return item["file_id"]
    finally:
        db.close()


def purge_trash(trash_id):
    """Видалити назавжди: і файл із .trash, і запис."""
    from core.storage_api import get_storage
    db = get_db()
    try:
        item = q(db, "SELECT trash_rel_path FROM trash_items WHERE id=%s", (trash_id,))
        if not item:
            raise TrashError("У кошику такого немає")
        if item["trash_rel_path"]:
            try:
                get_storage().delete(item["trash_rel_path"])
            except Exception as e:
                print(f"  ⚠ Кошик: не вдалося стерти {item['trash_rel_path']}: {e}")
        with db.cursor() as cur:
            cur.execute("DELETE FROM trash_items WHERE id=%s", (trash_id,))
        db.commit()
    finally:
        db.close()


def empty_trash():
    ids = [r["id"] for r in list_trash()]
    for tid in ids:
        purge_trash(tid)
    return len(ids)
