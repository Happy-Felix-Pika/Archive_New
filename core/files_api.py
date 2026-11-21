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


def find_file_by_path(rel_path: str, db=None, storage_id: str = "default") -> dict | None:
    """Рядок files за шляхом у сховищі (через path_hash — він під індексом)."""
    own = db is None
    if own:
        db = get_db()
    try:
        return q(db, """SELECT id, storage_id, rel_path, name, ext, size_bytes, md5_hash
                        FROM files WHERE storage_id=%s AND path_hash=MD5(%s) AND rel_path=%s""",
                 (storage_id, rel_path, rel_path))
    finally:
        if own:
            db.close()


def file_ids_by_paths(rel_paths, db=None, storage_id: str = "default") -> dict:
    """{rel_path: id} для списку шляхів — одним-двома запитами, а не по
    запиту на файл (каталог показує теки з тисячами файлів)."""
    import hashlib
    paths = [p for p in rel_paths if p]
    if not paths:
        return {}
    own = db is None
    if own:
        db = get_db()
    out = {}
    try:
        for i in range(0, len(paths), 500):
            chunk = paths[i:i + 500]
            hashes = [hashlib.md5(p.encode("utf-8")).hexdigest() for p in chunk]
            ph = ",".join(["%s"] * len(hashes))
            for r in qq(db, f"""SELECT id, rel_path FROM files
                                WHERE storage_id=%s AND path_hash IN ({ph})""",
                        [storage_id] + hashes):
                out[r["rel_path"]] = r["id"]
    finally:
        if own:
            db.close()
    return out


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
        moved = False
        if storage.exists(f["rel_path"]):
            if not storage.move(f["rel_path"], trash_rel):
                db.rollback()
                raise TrashError("Не вдалося перемістити файл у кошик")
            moved = True
        # файлу на диску могло вже не бути — тоді кошик тримає лише запис

        try:
            with db.cursor() as cur:
                cur.execute("UPDATE trash_items SET trash_rel_path=%s WHERE id=%s", (trash_rel, trash_id))
                cur.execute("DELETE FROM files WHERE id=%s", (file_id,))
            db.commit()
        except Exception as e:
            # База впала ПІСЛЯ того, як файл уже переїхав (наприклад, скінчилось
            # місце на диску — 1114). Без цього файл лишався в .trash без
            # жодного запису, а в базі вказував на порожнє місце: зникав звідусіль.
            db.rollback()
            if moved:
                try:
                    storage.move(trash_rel, f["rel_path"])
                except Exception as e2:
                    print(f"  ⚠ Кошик: не вдалося повернути {trash_rel} → {f['rel_path']}: {e2}")
            raise TrashError(f"Не вдалося перемістити у кошик: помилка бази ({e}). Файл лишився на місці.")
    finally:
        db.close()

    events.emit("file.deleted", {"file_id": file_id, "trash_id": trash_id})
    return trash_id


def trash_path(rel_path, user_id=None):
    """
    Видалення з каталогу — за шляхом, а не за id.

    Файл з бази йде звичайним trash_file (зі знімком тегів, автора тощо).
    Файл, якого в базі немає (особисті файли у виключених теках, щось
    скопійоване руками), теж іде в кошик, а не стирається: запис кошика
    з file_id = 0 — «лише файл, без запису в архіві».
    """
    from core.storage_api import get_storage

    row = find_file_by_path(rel_path)
    if row:
        return trash_file(row["id"], user_id)

    storage = get_storage()
    if not storage.exists(rel_path):
        raise TrashError("Файл не знайдено")
    name = rel_path.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower()[:10] if "." in name else ""
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""INSERT INTO trash_items
                           (file_id, name, ext, size_bytes, storage_id, original_rel_path,
                            trash_rel_path, snapshot, deleted_by)
                           VALUES (0,%s,%s,%s,'default',%s,'',%s,%s)""",
                        (name, ext, storage.size(rel_path), rel_path,
                         _json.dumps({"untracked": True}), user_id))
            trash_id = cur.lastrowid
        trash_rel = f"{TRASH_DIR}/{trash_id}__{name}"
        if not storage.move(rel_path, trash_rel):
            db.rollback()
            raise TrashError("Не вдалося перемістити файл у кошик")
        with db.cursor() as cur:
            cur.execute("UPDATE trash_items SET trash_rel_path=%s WHERE id=%s", (trash_rel, trash_id))
        db.commit()
        return trash_id
    finally:
        db.close()


def folder_delete_check(rel_path) -> list:
    """Чому теку не можна видалити (список причин від застосунків), або []."""
    from core import events
    db = get_db()
    try:
        return events.apply_filters("folder.delete_check", [], rel_path=rel_path, db=db) or []
    finally:
        db.close()


def trash_folder(rel_path, user_id=None) -> dict:
    """
    Видалити теку: кожен файл усередині (з підтеками) — у кошик тим самим
    шляхом, що й поодинокий файл, тобто з базою і знімком тегів/автора.
    Саму теку прибираємо лише коли вона стала порожньою: якщо хоч один файл
    не пішов у кошик, тека лишається разом із ним.
    """
    from core.storage_api import get_storage

    problems = folder_delete_check(rel_path)
    if problems:
        raise TrashError(" ".join(problems))
    storage = get_storage()
    if not storage.exists(rel_path):
        raise TrashError("Теку не знайдено")

    import secrets
    batch = secrets.token_hex(8)
    done, errors, ids = 0, [], []
    for f in storage.walk_files(rel_path):
        try:
            ids.append((f, trash_path(f, user_id)))
            done += 1
        except Exception as e:
            errors.append({"path": f, "error": str(e)})
    if ids:
        # у кошику ці файли — одна тека
        db = get_db()
        try:
            with db.cursor() as cur:
                ph = ",".join(["%s"] * len(ids))
                cur.execute(f"UPDATE trash_items SET folder_rel=%s, folder_batch=%s WHERE id IN ({ph})",
                            [rel_path, batch] + [tid for _, tid in ids])
            db.commit()
        except Exception as e:
            print(f"  ⚠ Кошик: не вдалося позначити теку {rel_path}: {e}")
        finally:
            db.close()
    removed = False
    if not errors:
        removed = storage.remove_empty_dir(rel_path)
        if not removed:
            errors.append({"path": rel_path, "error": "Файли в кошику, але саму теку прибрати не вдалося"})
    return {"trashed": done, "folder_removed": removed, "errors": errors, "trash_ids": ids, "batch": batch}


# ═════════════════════════════════════════════════════════════════════
#  ПЕРЕМІЩЕННЯ
#
#  Файл переїжджає у сховищі, і в тій самій транзакції оновлюється
#  files.rel_path — інакше він перестав би відкриватись, а перевірка
#  «відсутніх файлів» прибрала б його з бази.
#
#  Застосунки можуть:
#    * фільтр 'file.move_check' — заборонити переїзд (список причин);
#    * подія  'file.moved'      — оновити своє в тій самій транзакції
#                                 (furry: автор і підпапка за новою текою).
# ═════════════════════════════════════════════════════════════════════

class MoveError(Exception):
    """Зрозуміла причина, чому файл не переїхав (показується користувачу)."""


def move_path(src_rel: str, dst_folder: str) -> str:
    """Перемістити файл src_rel у теку dst_folder ('' — корінь). Повертає новий шлях."""
    from core import events
    from core.storage_api import get_storage

    name = src_rel.rsplit("/", 1)[-1]
    dst_rel = f"{dst_folder}/{name}" if dst_folder else name
    if dst_rel == src_rel:
        return src_rel

    storage = get_storage()
    if not storage.exists(src_rel):
        raise MoveError("Файл не знайдено")
    db = get_db()
    try:
        if storage.exists(dst_rel) or find_file_by_path(dst_rel, db):
            raise MoveError(f"У «{dst_folder or 'корені'}» уже є файл «{name}»")
        row = find_file_by_path(src_rel, db)
        if row:
            problems = events.apply_filters("file.move_check", [], file_id=row["id"],
                                            old_rel=src_rel, new_rel=dst_rel, db=db) or []
            if problems:
                raise MoveError(" ".join(problems))

        if not storage.move(src_rel, dst_rel):
            raise MoveError("Не вдалося перемістити файл у сховищі")
        if not row:
            return dst_rel          # файл поза архівом — у базі міняти нічого

        try:
            with db.cursor() as cur:
                cur.execute("UPDATE files SET rel_path=%s WHERE id=%s", (dst_rel, row["id"]))
            events.emit("file.moved", {"file_id": row["id"], "old_rel": src_rel,
                                       "new_rel": dst_rel, "db": db})
            db.commit()
        except Exception as e:
            db.rollback()
            try:
                storage.move(dst_rel, src_rel)
            except Exception as e2:
                print(f"  ⚠ Переміщення: не вдалося повернути {dst_rel} → {src_rel}: {e2}")
            raise MoveError(f"Помилка бази ({e}) — файл повернуто на місце.")
        return dst_rel
    finally:
        db.close()


def _storage_is_local():
    from core.storage import get_storage_mode
    return get_storage_mode() == "local"


def list_trash():
    """
    Записи кошика. missing=True — файлу в .trash на диску вже немає
    (перевіряється лише для локального сховища: для Nextcloud це був би
    окремий мережевий запит на кожен рядок).
    """
    db = get_db()
    try:
        try:
            rows = qq(db, """SELECT id, file_id, name, ext, size_bytes, original_rel_path,
                                    trash_rel_path, deleted_by, deleted_at, folder_rel, folder_batch
                             FROM trash_items ORDER BY deleted_at DESC, id DESC""")
        except Exception:
            # колонок теки ще немає (сервер не оновив таблиці)
            rows = qq(db, """SELECT id, file_id, name, ext, size_bytes, original_rel_path,
                                    trash_rel_path, deleted_by, deleted_at
                             FROM trash_items ORDER BY deleted_at DESC, id DESC""")
    finally:
        db.close()
    check = _storage_is_local()
    for r in rows:
        r["deleted_at"] = _dt(r["deleted_at"])
        if check:
            r["missing"] = not (r["trash_rel_path"] and os.path.isfile(local_path(r["trash_rel_path"])))
    return rows


def trash_item(trash_id) -> dict | None:
    db = get_db()
    try:
        return q(db, "SELECT * FROM trash_items WHERE id=%s", (trash_id,))
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

        if not item["file_id"]:
            # файл поза архівом (видалений з каталогу): повертаємо лише сам файл
            storage = get_storage()
            trash_rel = item["trash_rel_path"]
            if not trash_rel or not storage.exists(trash_rel):
                raise TrashError("Самого файлу в кошику вже немає на диску — відновити нічого.")
            if storage.exists(orig):
                raise TrashError(f"У сховищі на місці «{orig}» уже лежить файл. Прибери його і спробуй знову.")
            if not storage.move(trash_rel, orig):
                raise TrashError("Не вдалося повернути файл з кошика")
            with db.cursor() as cur:
                cur.execute("DELETE FROM trash_items WHERE id=%s", (trash_id,))
            db.commit()
            return None

        # на старому місці вже щось є — не затираємо
        if q(db, "SELECT id FROM files WHERE storage_id=%s AND rel_path=%s", (item["storage_id"], orig)):
            raise TrashError(f"На місці «{orig}» уже є інший файл. Видали або перейменуй його і спробуй знову.")
        if q(db, "SELECT id FROM files WHERE id=%s", (item["file_id"],)):
            raise TrashError("Цей файл уже відновлено")

        storage = get_storage()
        trash_rel = item["trash_rel_path"]
        # Без самого файлу відновлювати нічого: інакше в архіві з'являвся
        # «файл-привид» — рядок у базі є, а відкрити його не можна.
        if not trash_rel or not storage.exists(trash_rel):
            raise TrashError("Самого файлу в кошику вже немає на диску — відновити нічого. "
                             "Запис можна прибрати кнопкою «Видалити назавжди».")
        if storage.exists(orig):
            raise TrashError(f"У сховищі на місці «{orig}» уже лежить файл. Прибери його і спробуй знову.")

        # Застосунки можуть заздалегідь сказати, чому відновлення зламає їхні
        # дані (furry: цей пост уже скачано знову іншим файлом).
        problems = events.apply_filters("file.restore_check", [], snapshot=snap,
                                        file_id=item["file_id"], db=db) or []
        if problems:
            raise TrashError(" ".join(problems))

        if not storage.move(trash_rel, orig):
            raise TrashError("Не вдалося повернути файл з кошика")

        try:
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
        except Exception as e:
            # база не прийняла — файл назад у кошик, інакше він застрягав на
            # старому місці без запису, а кошик більше не міг його відновити
            db.rollback()
            try:
                storage.move(orig, trash_rel)
            except Exception as e2:
                print(f"  ⚠ Кошик: не вдалося повернути {orig} → {trash_rel}: {e2}")
            raise TrashError(f"Не вдалося відновити: помилка бази ({e}). Файл лишився в кошику.")

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


def _folder_ids(batch) -> list:
    db = get_db()
    try:
        return [r["id"] for r in qq(db, "SELECT id FROM trash_items WHERE folder_batch=%s ORDER BY id", (batch,))]
    finally:
        db.close()


def restore_trash_folder(batch) -> dict:
    """Відновити видалену теку: усі її файли — на старі місця."""
    from core.storage_api import get_storage
    ids = _folder_ids(batch)
    if not ids:
        raise TrashError("У кошику такої теки немає")
    item = trash_item(ids[0])
    done, errors = 0, []
    for tid in ids:
        try:
            restore_trash(tid)
            done += 1
        except TrashError as e:
            errors.append(str(e))
    folder = item.get("folder_rel") if item else None
    if folder:
        try:
            get_storage().mkdir(folder)          # тека повертається, навіть якщо файли не всі
        except Exception:
            pass
    return {"folder": folder, "restored": done, "errors": errors}


def purge_trash_folder(batch) -> dict:
    ids = _folder_ids(batch)
    if not ids:
        raise TrashError("У кошику такої теки немає")
    done, errors = 0, []
    for tid in ids:
        try:
            purge_trash(tid)
            done += 1
        except TrashError as e:
            errors.append(str(e))
    return {"removed": done, "errors": errors}


def purge_trash(trash_id):
    """
    Видалити назавжди: і файл із .trash, і запис.

    Запис прибирається лише тоді, коли файлу на диску справді більше немає.
    Раніше помилка видалення ковталась, а запис усе одно стирався — файл
    лишався в .trash невидимим для кошика й тихо займав місце.
    """
    from core.storage_api import get_storage
    db = get_db()
    try:
        item = q(db, "SELECT trash_rel_path FROM trash_items WHERE id=%s", (trash_id,))
        if not item:
            raise TrashError("У кошику такого немає")
        rel = item["trash_rel_path"]
        if rel:
            storage = get_storage()
            try:
                storage.delete(rel)
            except Exception as e:
                print(f"  ⚠ Кошик: не вдалося стерти {rel}: {e}")
            if storage.exists(rel):
                raise TrashError(f"Не вдалося стерти файл «{rel}» з диска — запис лишено в кошику.")
        with db.cursor() as cur:
            cur.execute("DELETE FROM trash_items WHERE id=%s", (trash_id,))
        db.commit()
    finally:
        db.close()


def empty_trash():
    """Повертає (скільки стерто, скільки не вдалося)."""
    removed, failed = 0, 0
    for r in list_trash():
        try:
            purge_trash(r["id"])
            removed += 1
        except TrashError as e:
            print(f"  ⚠ Кошик: {e}")
            failed += 1
    return removed, failed


# ── «Сироти»: файли в .trash без запису в кошику ─────────────────────
#
#  Звідки беруться: старий баг (база падала ПІСЛЯ переносу файлу — так
#  було, коли скінчилось місце на диску) і старе «Видалити назавжди», яке
#  стирало запис навіть тоді, коли файл стерти не вдалося. Їх не видно в
#  кошику, але вони займають місце.
#
#  Ім'я в .trash — «<id кошика>__<назва>». Якщо в files є рядок з такою
#  назвою, а на його місці файлу немає — це той самий файл, що «зник»:
#  його можна повернути на місце. Інакше — лише стерти.
# ─────────────────────────────────────────────────────────────────────

def _orphan_rel(name: str) -> str | None:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    return f"{TRASH_DIR}/{name}"


def list_trash_orphans():
    if not _storage_is_local():
        return []
    trash_abs = local_path(TRASH_DIR)
    if not os.path.isdir(trash_abs):
        return []
    db = get_db()
    try:
        known = {r["trash_rel_path"] for r in qq(db, "SELECT trash_rel_path FROM trash_items")}
        out = []
        for name in sorted(os.listdir(trash_abs)):
            full = os.path.join(trash_abs, name)
            rel = f"{TRASH_DIR}/{name}"
            if not os.path.isfile(full) or rel in known:
                continue
            real_name = name.split("__", 1)[1] if "__" in name else name
            # кандидат на повернення: рядок files з тією ж назвою й тим самим
            # розміром, файлу якого на місці немає. Розмір — щоб однакові
            # назви на кшталт «1.png» не сплутались.
            size = os.path.getsize(full)
            back_to = None
            for row in qq(db, """SELECT id, rel_path FROM files
                                 WHERE name=%s AND (size_bytes=%s OR size_bytes IS NULL) LIMIT 20""",
                          (real_name, size)):
                if not os.path.exists(local_path(row["rel_path"])):
                    back_to = row
                    break
            out.append({
                "name": name, "real_name": real_name,
                "ext": real_name.rsplit(".", 1)[-1].lower() if "." in real_name else "",
                "size_bytes": size,
                "mtime": int(os.path.getmtime(full)),
                "restore_to": back_to["rel_path"] if back_to else None,
                "restore_file_id": back_to["id"] if back_to else None,
            })
        return out
    finally:
        db.close()


def restore_trash_orphan(name):
    rel = _orphan_rel(name)
    if not rel:
        raise TrashError("Погане ім'я файлу")
    match = next((o for o in list_trash_orphans() if o["name"] == name), None)
    if not match:
        raise TrashError("Такого файлу серед загублених немає")
    if not match["restore_to"]:
        raise TrashError("Невідомо, куди його повертати: у базі немає «порожнього» місця під цей файл.")
    from core.storage_api import get_storage
    if not get_storage().move(rel, match["restore_to"]):
        raise TrashError("Не вдалося перемістити файл")
    return match["restore_file_id"]


def purge_trash_orphan(name):
    rel = _orphan_rel(name)
    if not rel:
        raise TrashError("Погане ім'я файлу")
    if not any(o["name"] == name for o in list_trash_orphans()):
        raise TrashError("Такого файлу серед загублених немає")
    from core.storage_api import get_storage
    st = get_storage()
    st.delete(rel)
    if st.exists(rel):
        raise TrashError("Не вдалося стерти файл з диска")
