"""
Furry Archive — застосунок.

Автори, серії, зовнішні джерела (e621, Inkbunny) і автоматична
архівація. Ядро без нього лишається звичайним файловим архівом:
каталог, файли, завантаження, користувачі, задачі.

Адреси лишаються тими ж (/api/artists, /api/live/... тощо), бо на
них уже розраховує фронтенд і зовнішні архіви.
"""

import threading

from . import artists, library, poolstore, pools, runner

MODULES = (artists, pools, runner)


def _file_meta(data, file_id, db, **_):
    """
    Дописує в картку файлу те, що знає тільки furry: автора, джерело,
    посилання на пост і серію.
    Ядро цих полів не знає — воно лише дає фільтру доповнити відповідь.
    """
    with db.cursor() as cur:
        cur.execute("""
            SELECT fp.source, fp.post_url, fp.subfolder, fp.pool_order,
                   a.folder_name, a.artist_name, a.id AS artist_id
            FROM furry_posts fp
            JOIN artists a ON a.id = fp.artist_id
            WHERE fp.file_id = %s
        """, (file_id,))
        row = cur.fetchone()

    if not row:
        # Файл без автора — нічого не додаємо, картка лишається як є
        return data

    # усі пули файлу — один запит по індексу таблиці сторінок
    with db.cursor() as cur:
        file_pools = poolstore.file_pools(cur, file_id)

    data.update({
        "artist":     row["artist_name"],
        "artist_id":  row["artist_id"],
        "source":     row["source"],
        "post_url":   row["post_url"],
        "subfolder":  row["subfolder"],
        "pool_order": row["pool_order"],
        "pool_total": next((p["total"] for p in file_pools if not p["linked"]), None) if row["subfolder"] else None,
        "pools":      file_pools,
        "artist_fav": row["artist_id"] in artists.favorite_artist_ids(_uid()),
    })
    return data


def _uid():
    try:
        from core.auth import current_user_id
        return current_user_id()
    except Exception:
        return None


def _snapshot_for_trash(snap, file_id, db, **_):
    """Перед відправкою в кошик — запам'ятати автора, пост і серію."""
    with db.cursor() as cur:
        cur.execute("SELECT * FROM furry_posts WHERE file_id=%s", (file_id,))
        row = cur.fetchone()
    if row:
        snap["furry"] = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}
        with db.cursor() as cur:
            extra = [{"artist_id": p["artist_id"], "pool_name": p["name"], "pool_order": p["order"]}
                     for p in poolstore.file_pools(cur, file_id) if p["linked"]]
        if extra:
            snap["furry"]["pool_links"] = extra
    return snap


def _restore_check(problems, snapshot, file_id, db, **_):
    """
    Поки файл лежав у кошику, архіватор не бачив посту в furry_posts і міг
    скачати його знову. Тоді відновлення дало б дубль, а прив'язка до
    автора мовчки загубилась би (унікальний ключ автор+пост+сторінка).
    """
    data = (snapshot or {}).get("apps", {}).get("furry")
    if not data:
        return problems
    with db.cursor() as cur:
        cur.execute("""SELECT file_id FROM furry_posts
                       WHERE artist_id=%s AND post_id=%s AND page_num=%s AND file_id<>%s""",
                    (data["artist_id"], data["post_id"], data.get("page_num") or 1, file_id))
        row = cur.fetchone()
    if row:
        problems.append(f"Цей пост уже є в архіві іншим файлом (id {row['file_id']}) — "
                        f"схоже, архіватор скачав його знову. Відновлення дасть дубль; "
                        f"якщо він не потрібен — «Видалити назавжди».")
    return problems


def _restore_from_trash(payload):
    """Файл повернули з кошика — повертаємо і прив'язку до автора."""
    data = (payload.get("snapshot") or {}).get("apps", {}).get("furry")
    if not data:
        return
    db = payload["db"]
    with db.cursor() as cur:
        # автора могли видалити, поки файл лежав у кошику — тоді файл
        # відновлюється без автора, а не падає
        cur.execute("SELECT id FROM artists WHERE id=%s", (data["artist_id"],))
        if not cur.fetchone():
            print(f"  ⚠ furry: автора {data['artist_id']} вже немає — файл {payload['file_id']} відновлено без нього")
            return
        cur.execute("""INSERT IGNORE INTO furry_posts
                       (file_id, artist_id, post_id, source, post_url, page_num, subfolder, pool_order, downloaded_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (payload["file_id"], data["artist_id"], data["post_id"], data.get("source"),
                     data.get("post_url"), data.get("page_num"), data.get("subfolder"),
                     data.get("pool_order"), data.get("downloaded_at")))
        poolstore.sync_file(cur, payload["file_id"])
        for l in data.get("pool_links") or []:
            cur.execute("SELECT 1 FROM artists WHERE id=%s", (l["artist_id"],))
            if cur.fetchone():
                poolstore.link(cur, payload["file_id"], l["artist_id"], l["pool_name"], l.get("pool_order"), l.get("source"))


def _pools_startup():
    from core.db import _connect
    try:
        db = _connect()
        try:
            with db.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM furry_pools")
                first = cur.fetchone()["n"] == 0
                cur.execute("SHOW TABLES LIKE 'furry_pool_links'")
                first = first or bool(cur.fetchone())
            if first:
                print("  … Пули: перший перенос у таблиці пулів (разово, може зайняти хвилину)")
                poolstore.reconcile_all(db)
        finally:
            db.close()
    except Exception as e:
        print(f"  ⚠ Пули: перенос не вдався: {e}")


def _overview_stats(data, db, **_):
    """Картки «Огляду», які мають сенс лише з цим застосунком."""
    def count(sql):
        with db.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
        return int(list(row.values())[0]) if row else 0

    data["cards"] += [
        {"id": "furry-artists", "label": {"uk": "Авторів", "ru": "Авторов", "en": "Artists"},
         "value": count("SELECT COUNT(*) FROM artists"), "color": "c-accent", "order": 10},
        {"id": "furry-e621", "label": {"uk": "e621", "ru": "e621", "en": "e621"},
         "value": count("SELECT COUNT(*) FROM furry_posts WHERE source='e621'"),
         "color": "c-blue", "order": 30},
        {"id": "furry-inkbunny", "label": {"uk": "Inkbunny", "ru": "Inkbunny", "en": "Inkbunny"},
         "value": count("SELECT COUNT(*) FROM furry_posts WHERE source='inkbunny'"),
         "color": "c-yellow", "order": 31},
    ]
    with db.cursor() as cur:
        cur.execute("""SELECT a.artist_name AS label, COUNT(fp.file_id) AS value
                       FROM artists a JOIN furry_posts fp ON fp.artist_id = a.id
                       GROUP BY a.id ORDER BY value DESC LIMIT 5""")
        rows = cur.fetchall()
    data["blocks"].append({
        "id": "furry-top-artists",
        "title": {"uk": "Топ авторів", "ru": "Топ авторов", "en": "Top artists"},
        "rows": [{"label": r["label"], "value": int(r["value"])} for r in rows],
    })
    return data


def _artist_for_path(db, rel_path):
    """
    Чия це тека: (artist_id, subfolder) за шляхом файлу, або None.

    Шлях furry-файлу — «тека автора / [підпапка] / ім'я» (або просто ім'я
    для автора '_root'). Саме так його складають архіватор і
    resync_rel_path, тому прив'язка мусить цьому відповідати.
    """
    parts = [p for p in rel_path.split("/") if p]
    folder = parts[0] if len(parts) > 1 else "_root"
    sub = "/".join(parts[1:-1]) or None
    with db.cursor() as cur:
        cur.execute("SELECT id, artist_name FROM artists WHERE folder_name=%s", (folder,))
        row = cur.fetchone()
    return (row["id"], sub) if row else None


def _move_check(problems, file_id, new_rel, db, **_):
    """
    Файл з автором може переїжджати лише в теку автора: інакше архіватор
    шукатиме його за старою схемою, вважатиме зниклим і прибере з бази.
    """
    with db.cursor() as cur:
        cur.execute("""SELECT fp.artist_id, fp.post_id, fp.page_num, a.artist_name
                       FROM furry_posts fp JOIN artists a ON a.id = fp.artist_id
                       WHERE fp.file_id=%s""", (file_id,))
        cur_row = cur.fetchone()
    if not cur_row:
        return problems
    target = _artist_for_path(db, new_rel)
    if not target:
        problems.append(f"Файл прив'язаний до автора «{cur_row['artist_name']}» — його можна "
                        f"переносити лише в теку автора (@…) або її підпапки.")
        return problems
    artist_id, _sub = target
    if artist_id != cur_row["artist_id"]:
        with db.cursor() as cur:
            cur.execute("""SELECT file_id FROM furry_posts
                           WHERE artist_id=%s AND post_id=%s AND page_num<=>%s AND file_id<>%s""",
                        (artist_id, cur_row["post_id"], cur_row["page_num"], file_id))
            if cur.fetchone():
                problems.append("У того автора вже є цей самий пост — перенос дав би дубль.")
    return problems


def _folder_delete_check(problems, rel_path, db, **_):
    """
    Теку автора цілком з каталогу не видаляємо: автор лишився б у базі з
    увімкненим автооновленням, і архіватор скачав би все знову. Підтеки
    (серії) — можна.
    """
    if "/" in rel_path.strip("/"):
        return problems
    with db.cursor() as cur:
        cur.execute("SELECT artist_name FROM artists WHERE folder_name=%s", (rel_path.strip("/"),))
        row = cur.fetchone()
    if row:
        problems.append(f"Це тека автора «{row['artist_name']}». Щоб прибрати автора, скористайся "
                        f"вкладкою «Автори»; окремі файли й підтеки звідси видаляти можна.")
    return problems


def _on_file_moved(payload):
    """Файл переїхав у каталозі — автор і підпапка тепер за новою текою."""
    db = payload["db"]
    target = _artist_for_path(db, payload["new_rel"])
    if not target:
        return
    artist_id, sub = target
    fid = payload["file_id"]
    with db.cursor() as cur:
        # у тій самій серії лишається її номер, у новій — ще не визначений
        cur.execute("""UPDATE furry_posts
                       SET pool_order = CASE WHEN artist_id=%s AND subfolder<=>%s THEN pool_order ELSE NULL END,
                           artist_id=%s, subfolder=%s
                       WHERE file_id=%s""",
                    (artist_id, sub, artist_id, sub, fid))
        if sub:
            # файл уже був у цьому пулі додатково — беремо звідти номер сторінки
            pid = poolstore.find_pool(cur, artist_id, sub)
            if pid:
                cur.execute("SELECT page FROM furry_pool_items WHERE pool_id=%s AND file_id=%s", (pid, fid))
                was = cur.fetchone()
                if was and was["page"] is not None:
                    cur.execute("UPDATE furry_posts SET pool_order=COALESCE(pool_order,%s) WHERE file_id=%s",
                                (was["page"], fid))
        poolstore.sync_file(cur, fid)


def _upload_plan(plan, form, md5, db, **_):
    """
    Завантаження з указаним автором: кладемо в теку автора (створюємо його,
    якщо ще немає) і запам'ятовуємо, до кого прив'язати файл.
    Без автора, але в теку автора (напр. перетягнули в каталозі в @вовк) —
    прив'язуємо до того автора. Інакше файл іде туди, куди просили, без автора.
    """
    name = (form.get("artist_name") or "").strip()
    if not name:
        folder = (plan.get("folder") or "").strip("/")
        if folder:
            target = _artist_for_path(db, f"{folder}/_")
            if target:
                plan["furry"] = {"artist_id": target[0], "post_id": f"manual_{md5[:16]}",
                                 "subfolder": target[1]}
        return plan
    folder = name if name.startswith("@") else f"@{name}"
    with db.cursor() as cur:
        cur.execute("SELECT id FROM artists WHERE folder_name=%s", (folder,))
        row = cur.fetchone()
        if row:
            artist_id = row["id"]
        else:
            cur.execute("INSERT INTO artists (folder_name, artist_name) VALUES (%s,%s)",
                        (folder, folder[1:]))
            artist_id = cur.lastrowid
    db.commit()
    plan["folder"] = folder
    plan["furry"] = {"artist_id": artist_id, "post_id": f"manual_{md5[:16]}"}
    return plan


def _on_file_created(payload):
    """Файл збережено — прив'язуємо до автора, якщо його вказували."""
    data = (payload.get("plan") or {}).get("furry")
    if data:
        with payload["db"].cursor() as cur:
            cur.execute("""INSERT IGNORE INTO furry_posts
                           (file_id, artist_id, post_id, source, post_url, page_num, subfolder, downloaded_at)
                           VALUES (%s,%s,%s,NULL,NULL,1,%s,NOW())""",
                        (payload["file_id"], data["artist_id"], data["post_id"], data.get("subfolder")))
            poolstore.sync_file(cur, payload["file_id"])


# ── Відміна дій модератора (журнал ядра) ─────────────────────────────

def _state_restore(payload):
    """Відмінили переміщення — повертаємо автора, пул і номер сторінки."""
    data = (payload.get("snapshot") or {}).get("furry")
    if not data:
        return
    with payload["db"].cursor() as cur:
        cur.execute("""UPDATE furry_posts SET artist_id=%s, subfolder=%s, pool_order=%s
                       WHERE file_id=%s""",
                    (data["artist_id"], data.get("subfolder"), data.get("pool_order"), payload["file_id"]))
        poolstore.sync_file(cur, payload["file_id"])


def _undo_auto_update(p):
    from core.db import get_db
    db = get_db()
    with db.cursor() as cur:
        cur.execute("UPDATE artists SET auto_update=%s WHERE id=%s", (p["before"], p["artist_id"]))
    db.commit()


def _undo_orders(p):
    from core.db import get_db
    db = get_db()
    with db.cursor() as cur:
        for o in p.get("orders") or []:
            _restore_order(cur, o)
    db.commit()


def _restore_order(cur, o):
    """Повернути номер сторінки з журналу: у конкретному пулі (pool_id),
    за назвою пулу (старі записи) або «рідний» у furry_posts."""
    fid = o["file_id"]
    pid = o.get("pool_id")
    if not pid and o.get("pool"):
        cur.execute("SELECT artist_id FROM furry_posts WHERE file_id=%s", (fid,))
        r = cur.fetchone()
        pid = poolstore.find_pool(cur, r["artist_id"], o["pool"]) if r else None
    if pid:
        poolstore.set_page(cur, fid, pid, o["pool_order"])
    else:
        cur.execute("UPDATE furry_posts SET pool_order=%s WHERE file_id=%s", (o["pool_order"], fid))
        poolstore.sync_file(cur, fid)


def _undo_pool_assign(p):
    """Повернути файли з пулу туди, де вони були (тека, пул, сторінка)."""
    from core.db import get_db, q
    from core.modlog import UndoError
    from core.storage_api import get_storage
    db, st = get_db(), get_storage()
    art = q(db, "SELECT folder_name FROM artists WHERE id=%s", (p["artist_id"],))
    if not art:
        raise UndoError("Автора вже немає")
    prefix = "" if art["folder_name"] == "_root" else f"{art['folder_name']}/"
    ids, errors = [], []
    for f in p.get("files") or []:
        row = q(db, "SELECT name, rel_path FROM files WHERE id=%s", (f["file_id"],))
        if not row:
            errors.append(f"#{f['file_id']}: файлу вже немає")
            continue
        dst = prefix + (f"{f['subfolder']}/" if f.get("subfolder") else "") + row["name"]
        if row["rel_path"] != dst:
            if st.exists(dst):
                errors.append(f"{row['name']}: на старому місці вже щось лежить")
                continue
            if not st.move(row["rel_path"], dst):
                errors.append(f"{row['name']}: не вдалося перемістити")
                continue
        with db.cursor() as cur:
            cur.execute("UPDATE furry_posts SET subfolder=%s, pool_order=%s WHERE file_id=%s",
                        (f.get("subfolder"), f.get("pool_order"), f["file_id"]))
            poolstore.sync_file(cur, f["file_id"])
        ids.append(f["file_id"])
    library.resync_rel_path(db, ids)
    with db.cursor() as cur:
        for o in p.get("prev_pool_orders") or []:
            _restore_order(cur, o)
        # додаткові пули, які перенос зробив «рідними» — знову додаткові
        for l in p.get("removed_links") or []:
            poolstore.link(cur, l["file_id"], l["artist_id"], l["pool_name"], l.get("pool_order"), l.get("source"))
    db.commit()
    if errors and not ids:
        raise UndoError("; ".join(errors[:5]))
    return f"повернуто {len(ids)}" + (f", не вдалося: {'; '.join(errors[:5])}" if errors else "")


def _undo_archiver_run(p):
    """Прибрати в кошик усе, що цей запуск скачав для автора."""
    from core.db import get_db, qq
    from core.files_api import TrashError, trash_file
    from core.modlog import UndoError
    if not p.get("finished_at"):
        if runner._active_process and runner._active_process.poll() is None:
            raise UndoError("Архіватор ще працює — зупини його або дочекайся кінця")
        raise UndoError("Невідомо, коли запуск закінчився (сервер перезапускали?) — прибери файли вручну")
    db = get_db()
    name = p["artist"]
    rows = qq(db, """SELECT f.id FROM files f
                     JOIN furry_posts fp ON fp.file_id = f.id
                     JOIN artists a ON a.id = fp.artist_id
                     WHERE (a.folder_name=%s OR a.artist_name=%s)
                       AND f.created_at >= %s AND f.created_at <= %s""",
              (f"@{name}", name, p["started_at"], p["finished_at"]))
    n = 0
    for r in rows:
        try:
            trash_file(r["id"])
            n += 1
        except TrashError:
            pass
    return f"у кошик: {n} файл(ів), скачаних цим запуском"


def register(core):
    for module in MODULES:
        core.api.add_routes(module.bp, at_root=True)

    # Свої поля в картці файлу — через фільтр, а не правкою ядра
    core.events.add_filter("file.meta", _file_meta)
    # створення файлу з прив'язкою до автора — для дублікатів
    core.services.provide("furry.library", library)
    # ручне завантаження: «вказано автора → тека автора + прив'язка»
    core.events.add_filter("file.upload", _upload_plan)
    core.events.add_filter("stats.overview", _overview_stats)
    core.events.on("file.created", _on_file_created)
    # кошик ядра: зберегти й повернути свої дані про файл
    core.events.add_filter("file.trash", _snapshot_for_trash)
    core.events.on("file.restored", _restore_from_trash)
    core.events.add_filter("file.restore_check", _restore_check)
    # переміщення в каталозі: лише в теки авторів, з оновленням прив'язки
    core.events.add_filter("file.move_check", _move_check)
    core.events.on("file.moved", _on_file_moved)
    core.events.add_filter("folder.delete_check", _folder_delete_check)
    # журнал модераторів: відміна дій furry і повернення стану при відміні переміщення
    core.events.on("file.state_restore", _state_restore)
    from core import modlog
    modlog.register("artist.auto_update", _undo_auto_update, "Автооновлення автора")
    modlog.register("pool.set_page",      _undo_orders,       "Змінив сторінку в пулі")
    modlog.register("pool.resort",        _undo_orders,       "Пересортував пул")
    modlog.register("pool.assign",        _undo_pool_assign,  "Переніс у пул")
    modlog.register("archiver.run",       _undo_archiver_run, "Запустив архіватор")

    # Пули: при першому запуску після оновлення — перенести наявні пули в
    # таблиці пулів і склеїти дублі (разом, до відкриття сервера); далі —
    # звірка у фоні щогодини.
    _pools_startup()
    threading.Thread(target=pools.reconcile_loop, daemon=True).start()
    threading.Thread(target=runner._auto_update_scheduler_loop, daemon=True).start()
