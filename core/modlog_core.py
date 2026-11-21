"""
Відміна дій модератора, які робить саме ядро: файли, теки, кошик,
картка файлу, теги. Застосунки реєструють свої дії самі (furry — пули й
запуск архіватора, duplicates — черга «На перевірку»).

Кожна функція отримує payload, записаний у момент дії, і повертає
коротку примітку (або нічого). Якщо відмінити вже не можна — UndoError
з поясненням: наприклад, файл відтоді видалили.
"""

import posixpath

from core import events, modlog
from core.db import _connect
from core.modlog import UndoError


def _db():
    return _connect()


# ── Файли й теки ─────────────────────────────────────────────────────

def _undo_upload(p):
    from core.files_api import TrashError, trash_file
    try:
        trash_file(p["file_id"])
    except TrashError:
        raise UndoError("Цього файлу в архіві вже немає")
    return "файл переміщено в кошик"


def _undo_trash(p):
    """Видалення в кошик (файли або тека) → відновити з кошика."""
    from core.files_api import TrashError, restore_trash
    from core.storage_api import get_storage
    errors, done = [], 0
    for it in p.get("items") or []:
        try:
            restore_trash(it["trash_id"])
            done += 1
        except TrashError as e:
            errors.append(f"{posixpath.basename(it.get('path') or '')}: {e}")
    if p.get("folder") and not (p.get("items") or []):
        get_storage().mkdir(p["folder"])          # порожня тека — просто створити знову
    if errors and not done:
        raise UndoError("; ".join(errors[:5]))
    return f"відновлено {done}" + (f", не вдалося: {'; '.join(errors[:5])}" if errors else "")


def _undo_move(p):
    from core.files_api import MoveError, move_path
    errors, done = [], 0
    for it in reversed(p.get("items") or []):
        back = posixpath.dirname(it["from"])
        try:
            move_path(it["to"], back)
        except MoveError as e:
            errors.append(f"{posixpath.basename(it['to'])}: {e}")
            continue
        done += 1
        if it.get("file_id") and it.get("snapshot"):
            # Застосунки повертають своє (furry: автор, пул, номер сторінки) —
            # саме переміщення їх лише перерахувало б за текою.
            db = _db()
            try:
                events.emit("file.state_restore", {"file_id": it["file_id"],
                                                   "snapshot": it["snapshot"], "db": db})
                db.commit()
            finally:
                db.close()
    if errors and not done:
        raise UndoError("; ".join(errors[:5]))
    return f"повернуто {done}" + (f", не вдалося: {'; '.join(errors[:5])}" if errors else "")


def _undo_mkdir(p):
    from core.storage_api import get_storage
    st = get_storage()
    if not st.exists(p["path"]):
        return "теки вже немає"
    if not st.remove_empty_dir(p["path"]):
        raise UndoError("У теці вже є файли — прибери їх спершу (або відміни їхнє завантаження)")
    return "теку прибрано"


def _undo_trash_restore(p):
    from core.files_api import TrashError, trash_file, trash_path
    try:
        if p.get("file_id"):
            trash_file(p["file_id"])
        else:
            trash_path(p["rel_path"])
    except TrashError as e:
        raise UndoError(str(e))
    return "файл знову в кошику"


def _undo_trash_restore_folder(p):
    from core.files_api import TrashError, trash_folder
    try:
        res = trash_folder(p["folder"])
    except TrashError as e:
        raise UndoError(str(e))
    return f"тека знову в кошику ({res['trashed']} файл(ів))"


# ── Картка файлу ─────────────────────────────────────────────────────

def _undo_meta(p):
    db = _db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT 1 FROM furry_posts WHERE file_id=%s", (p["file_id"],))
            if not cur.fetchone():
                raise UndoError("Цього файлу в архіві вже немає")
            if "source" in p:
                cur.execute("UPDATE furry_posts SET source=%s WHERE file_id=%s", (p["source"], p["file_id"]))
            if "post_url" in p:
                cur.execute("UPDATE furry_posts SET post_url=%s WHERE file_id=%s", (p["post_url"], p["file_id"]))
            if p.get("artist_id") and p.get("artist_name") is not None:
                cur.execute("UPDATE artists SET artist_name=%s WHERE id=%s", (p["artist_name"], p["artist_id"]))
        db.commit()
    finally:
        db.close()


# ── Теги ─────────────────────────────────────────────────────────────

def _tag_unused(cur, tag_id):
    cur.execute("SELECT 1 FROM download_tags WHERE tag_id=%s LIMIT 1", (tag_id,))
    return cur.fetchone() is None


def _invalidate_tags():
    try:
        from blueprints.tags import _invalidate_tags_cache
        _invalidate_tags_cache()
    except Exception:
        pass


def _undo_tag_add(p):
    db = _db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM download_tags WHERE download_id=%s AND tag_id=%s", (p["file_id"], p["tag_id"]))
            if p.get("created_tag") and _tag_unused(cur, p["tag_id"]):
                cur.execute("DELETE FROM tags WHERE id=%s", (p["tag_id"],))
        db.commit()
    finally:
        db.close()
    _invalidate_tags()


def _undo_tag_remove(p):
    db = _db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT id FROM tags WHERE id=%s", (p["tag_id"],))
            if not cur.fetchone():
                raise UndoError(f"Тегу «{p.get('tag_name')}» уже немає в архіві")
            cur.execute("SELECT 1 FROM files WHERE id=%s", (p["file_id"],))
            if not cur.fetchone():
                raise UndoError("Цього файлу в архіві вже немає")
            cur.execute("INSERT IGNORE INTO download_tags (download_id, tag_id) VALUES (%s,%s)",
                        (p["file_id"], p["tag_id"]))
        db.commit()
    finally:
        db.close()
    _invalidate_tags()


def _undo_tag_set(p):
    db = _db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM download_tags WHERE download_id=%s", (p["file_id"],))
            for tid in p.get("before") or []:
                cur.execute("""INSERT IGNORE INTO download_tags (download_id, tag_id)
                               SELECT %s, id FROM tags WHERE id=%s""", (p["file_id"], tid))
        db.commit()
    finally:
        db.close()
    _invalidate_tags()


def _undo_tag_create(p):
    db = _db()
    try:
        with db.cursor() as cur:
            if not _tag_unused(cur, p["tag_id"]):
                raise UndoError(f"Тег «{p.get('tag_name')}» уже стоїть на файлах — "
                                f"спершу відміни додавання тегу до них")
            cur.execute("DELETE FROM tags WHERE id=%s", (p["tag_id"],))
        db.commit()
    finally:
        db.close()
    _invalidate_tags()


def register_all():
    modlog.register("file.upload",   _undo_upload,        "Завантажив файл")
    modlog.register("file.trash",    _undo_trash,         "Видалив у кошик")
    modlog.register("folder.trash",  _undo_trash,         "Видалив теку в кошик")
    modlog.register("file.move",     _undo_move,          "Перемістив")
    modlog.register("folder.create", _undo_mkdir,         "Створив теку")
    modlog.register("trash.restore", _undo_trash_restore, "Відновив з кошика")
    modlog.register("trash.restore_folder", _undo_trash_restore_folder, "Відновив теку з кошика")
    modlog.register("file.meta",     _undo_meta,          "Змінив картку файлу")
    modlog.register("tag.add",       _undo_tag_add,       "Додав тег")
    modlog.register("tag.remove",    _undo_tag_remove,    "Прибрав тег")
    modlog.register("tag.set",       _undo_tag_set,       "Замінив теги файлу")
    modlog.register("tag.create",    _undo_tag_create,    "Створив тег")
