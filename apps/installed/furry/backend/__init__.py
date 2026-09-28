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


def _upload_plan(plan, form, md5, db, **_):
    """
    Завантаження з указаним автором: кладемо в теку автора (створюємо його,
    якщо ще немає) і запам'ятовуємо, до кого прив'язати файл.
    Без автора — нічого не робимо, файл іде туди, куди просили.
    """
    name = (form.get("artist_name") or "").strip()
    if not name:
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
                           (file_id, artist_id, post_id, source, post_url, page_num, downloaded_at)
                           VALUES (%s,%s,%s,NULL,NULL,1,NOW())""",
                        (payload["file_id"], data["artist_id"], data["post_id"]))
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

    # Кеш серій і нічний планувальник — турбота застосунку, не ядра.
    try:
        pools._rebuild_pools_cache()
    except Exception as e:
        print(f"  ⚠ furry: кеш серій не прогрівся: {e}")
    threading.Thread(target=pools._pools_cache_background_loop, daemon=True).start()
    threading.Thread(target=runner._auto_update_scheduler_loop, daemon=True).start()
