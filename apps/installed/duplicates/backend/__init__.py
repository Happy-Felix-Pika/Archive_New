"""Пошук дублікатів. Залежить від furry: перевірка перед збереженням
йде в межах автора, а пошук по всьому архіву — застосунку не важливо."""

from . import duplicates
from .hashing import compute_phash


def _overview_stats(data, db, **_):
    """Свої картки «Огляду»: файлів з хешем і скільки чекає перевірки."""
    def count(sql):
        with db.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
        return int(list(row.values())[0]) if row else 0

    data["cards"] += [
        {"id": "dup-phash", "label": {"uk": "pHash індекс", "ru": "pHash индекс", "en": "pHash index"},
         "value": count("SELECT COUNT(*) FROM file_phash WHERE phash IS NOT NULL"),
         "hint": {"uk": "файлів з хешем", "ru": "файлов с хешем", "en": "files hashed"},
         "color": "c-green", "order": 50},
        {"id": "dup-pending", "label": {"uk": "На перевірку", "ru": "На проверку", "en": "Review"},
         "value": count("SELECT COUNT(*) FROM pending_duplicates"),
         "hint": {"uk": "можливих дублікатів", "ru": "возможных дубликатов", "en": "possible duplicates"},
         "color": "c-red", "order": 51},
    ]
    return data


def _snapshot_for_trash(snap, file_id, db, **_):
    with db.cursor() as cur:
        cur.execute("SELECT phash FROM file_phash WHERE file_id=%s", (file_id,))
        row = cur.fetchone()
    if row and row["phash"] is not None:
        snap["duplicates"] = {"phash": int(row["phash"])}
    return snap


def _restore_from_trash(payload):
    data = (payload.get("snapshot") or {}).get("apps", {}).get("duplicates")
    if not data:
        return
    with payload["db"].cursor() as cur:
        cur.execute("INSERT IGNORE INTO file_phash (file_id, phash) VALUES (%s,%s)",
                    (payload["file_id"], data["phash"]))


def _on_file_created(payload):
    """Новий файл — рахуємо pHash, щоб його можна було порівнювати."""
    content, ext = payload.get("content"), payload.get("ext")
    if not content:
        return
    h = compute_phash(content, ext)
    if h is None:
        return          # не картинка й не відео — порівнювати нічого
    with payload["db"].cursor() as cur:
        cur.execute("""INSERT INTO file_phash (file_id, phash) VALUES (%s,%s)
                       ON DUPLICATE KEY UPDATE phash=VALUES(phash)""", (payload["file_id"], h))


def _after_archiver(payload=None):
    """
    Архіватор закінчив — замінюємо файли, які поступились сторінці pool.
    Робиться тут, у веб-процесі: лише тут зареєстровані всі застосунки,
    тож старий файл іде в кошик з повним знімком (автор, теги, pHash).
    """
    from datetime import datetime
    try:
        from core.logs import _push_log
        log = lambda m: _push_log(f"[{datetime.now().strftime('%H:%M:%S')}] {m}")
    except Exception:
        log = print
    done, failed = duplicates.apply_pool_priority(log)
    if done or failed:
        log(f"⇄ Pool має перевагу: замінено {done}" + (f", не вдалось {failed} (лишились «На перевірку»)" if failed else ""))


def register(core):
    duplicates._core = core
    duplicates.register_undo()       # журнал модераторів: як відміняти рішення в черзі
    core.api.add_routes(duplicates.bp, at_root=True)
    # Довгий пошук по всьому архіву — фонова задача ядра
    core.jobs.register("scan-all", duplicates._scan_whole_archive)
    core.jobs.register("dup-auto-resolve", duplicates._auto_resolve)
    core.events.add_filter("file.trash", _snapshot_for_trash)
    core.events.add_filter("stats.overview", _overview_stats)
    core.events.on("file.created", _on_file_created)
    core.events.on("file.restored", _restore_from_trash)
    # після архіватора — автозаміна на користь сторінок pool; і раз при
    # старті, якщо сервер перезапустився, поки щось лишалось у черзі
    core.events.on("furry.archiver.finished", _after_archiver)
    import threading
    threading.Thread(target=_after_archiver, daemon=True).start()
