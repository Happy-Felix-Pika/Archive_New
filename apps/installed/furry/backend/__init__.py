"""
Furry Archive — застосунок.

Автори, серії, зовнішні джерела (e621, Inkbunny) і автоматична
архівація. Ядро без нього лишається звичайним файловим архівом:
каталог, файли, завантаження, користувачі, задачі.

Адреси лишаються тими ж (/api/artists, /api/live/... тощо), бо на
них уже розраховує фронтенд і зовнішні архіви.
"""

import threading

from . import artists, library, ncstats, pools, runner

MODULES = (artists, ncstats, pools, runner)


def _file_meta(data, file_id, db, **_):
    """
    Дописує в картку файлу те, що знає тільки furry: автора, джерело,
    посилання на пост, серію й публічне посилання Nextcloud.
    Ядро цих полів не знає — воно лише дає фільтру доповнити відповідь.
    """
    with db.cursor() as cur:
        cur.execute("""
            SELECT fp.source, fp.post_url, fp.subfolder, fp.pool_order,
                   a.folder_name, a.artist_name, a.id AS artist_id,
                   (SELECT COUNT(*) FROM furry_posts fp2
                     WHERE fp2.artist_id = fp.artist_id
                       AND fp2.subfolder <=> fp.subfolder
                       AND fp2.source    <=> fp.source) AS pool_total
            FROM furry_posts fp
            JOIN artists a ON a.id = fp.artist_id
            WHERE fp.file_id = %s
        """, (file_id,))
        row = cur.fetchone()

    if not row:
        # Файл без автора — нічого не додаємо, картка лишається як є
        return data

    share_url = None
    if row["folder_name"]:
        from core.auth import get_user_nc_config
        from core.nextcloud import nc_share_url
        base = get_user_nc_config().get("base_folder", "").strip("/")
        share_url = nc_share_url(row["folder_name"], data["filename"], base)

    data.update({
        "artist":     row["artist_name"],
        "artist_id":  row["artist_id"],
        "source":     row["source"],
        "post_url":   row["post_url"],
        "subfolder":  row["subfolder"],
        "pool_order": row["pool_order"],
        "pool_total": row["pool_total"] if row["subfolder"] else None,
        "share_url":  share_url,
    })
    return data


def _snapshot_for_trash(snap, file_id, db, **_):
    """Перед відправкою в кошик — запам'ятати автора, пост і серію."""
    with db.cursor() as cur:
        cur.execute("SELECT * FROM furry_posts WHERE file_id=%s", (file_id,))
        row = cur.fetchone()
    if row:
        snap["furry"] = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}
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
    with db.cursor() as cur:
        # у тій самій серії лишається її номер, у новій — ще не визначений
        cur.execute("""UPDATE furry_posts
                       SET pool_order = CASE WHEN artist_id=%s AND subfolder<=>%s THEN pool_order ELSE NULL END,
                           artist_id=%s, subfolder=%s
                       WHERE file_id=%s""",
                    (artist_id, sub, artist_id, sub, payload["file_id"]))
    try:
        pools._invalidate_pools_cache()
    except Exception:
        pass


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
    try:
        pools._invalidate_pools_cache()
    except Exception:
        pass


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

    # Кеш серій і нічний планувальник — турбота застосунку, не ядра.
    try:
        pools._rebuild_pools_cache()
    except Exception as e:
        print(f"  ⚠ furry: кеш серій не прогрівся: {e}")
    threading.Thread(target=pools._pools_cache_background_loop, daemon=True).start()
    threading.Thread(target=runner._auto_update_scheduler_loop, daemon=True).start()
