"""Художники, pools, злиття"""

import os, re, shutil

from flask import Blueprint, jsonify, request as flask_request


from .pools import _invalidate_pools_cache, pool_members, set_order
from blueprints.tags import _invalidate_tags_cache
from core.auth import admin_required, moderator_required, get_user_nc_config
from core import modlog
from core.db import get_db, q, qq
from core.warmcache import WarmCache
from .library import resync_rel_path
from core.storage_api import get_storage
from core.storage import get_storage_mode, local_file_path

bp = Blueprint("artists", __name__)


# ── Artists ───────────────────────────────────────────────────────────────

def _artist_rows(db, f="all", tag_ids=(), sort="files"):
    IMAGE_EXTS = ('png','jpg','jpeg','gif','webp')
    VIDEO_EXTS = ('webm','mp4')
    # Умова «файл підходить під фільтр» — рахуємо нею, а не відсіюємо рядки:
    # «останнє скачування» показується по всіх файлах автора, як і раніше.
    if f == "image":   match = f"f2.ext IN {IMAGE_EXTS}"
    elif f == "video": match = f"f2.ext IN {VIDEO_EXTS}"
    else:              match = "1"

    tag_where = ""
    tag_args  = []
    if tag_ids:
        placeholders = ",".join(["%s"] * len(tag_ids))
        tag_where = f"""
            AND EXISTS (
                SELECT dt.download_id FROM download_tags dt
                JOIN furry_posts dd ON dd.file_id = dt.download_id AND dd.artist_id = a.id
                WHERE dt.tag_id IN ({placeholders})
                GROUP BY dt.download_id
                HAVING COUNT(DISTINCT dt.tag_id) = %s
            )
        """
        tag_args = [int(t) for t in tag_ids] + [len(tag_ids)]

    order_col = "total_size" if sort == "size" else "total_files"

    # Один прохід по всіх постах з групуванням за автором. Раніше на
    # КОЖНОГО автора було шість окремих підзапитів — на великій
    # бібліотеці це секунди (вкладка «Автори» й пошук по авторах
    # чекали саме на це).
    rows = qq(db, f"""
        SELECT a.id, a.folder_name, a.artist_name, a.created_at, a.auto_update,
               s.total_files, s.e621_files, s.ib_files, s.last_download, s.total_size,
               (SELECT COUNT(*) FROM pending_duplicates p WHERE p.artist_id=a.id) AS pending_count
        FROM artists a
        JOIN (SELECT fp.artist_id,
                     SUM({match}) AS total_files,
                     SUM({match} AND fp.source='e621') AS e621_files,
                     SUM({match} AND fp.source='inkbunny') AS ib_files,
                     MAX(fp.downloaded_at) AS last_download,
                     COALESCE(SUM(IF({match}, f2.size_bytes, 0)),0) AS total_size
              FROM furry_posts fp JOIN files f2 ON f2.id = fp.file_id
              GROUP BY fp.artist_id) s ON s.artist_id = a.id
        WHERE s.total_files > 0 {tag_where}
        ORDER BY {order_col} DESC, a.id
    """, tag_args)
    for r in rows:
        if r["created_at"]:    r["created_at"]    = r["created_at"].isoformat()
        if r["last_download"]: r["last_download"]  = r["last_download"].isoformat()
        r["total_files"]   = int(r["total_files"]   or 0)
        r["e621_files"]    = int(r["e621_files"]    or 0)
        r["ib_files"]      = int(r["ib_files"]      or 0)
        r["pending_count"] = int(r["pending_count"] or 0)
        r["total_size"]    = int(r["total_size"]    or 0)
    return rows


# Основний вигляд (усі файли, без тегів) — найчастіший: вкладка «Автори» і
# пошук по авторах. Рахується на старті й оновлюється у фоні після змін
# (core/warmcache.py), тож відкривається миттєво.
artists_cache = WarmCache("artists", _artist_rows)


@bp.route("/api/artists")
@moderator_required
def api_artists():
    f = flask_request.args.get("filter", "all")
    tag_ids = flask_request.args.getlist("tag_id")
    sort = flask_request.args.get("sort", "files")  # files | size
    try:
        if f == "all" and not tag_ids:
            rows = artists_cache.get()
            if sort == "size":
                rows = sorted(rows, key=lambda r: (-r["total_size"], r["id"]))
            return jsonify(rows)
        return jsonify(_artist_rows(get_db(), f, tag_ids, sort))
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@bp.route("/api/artist/<int:artist_id>/downloads")
@moderator_required
def api_artist_downloads(artist_id):
    page   = int(flask_request.args.get("page", 1))
    limit  = int(flask_request.args.get("limit", 100))
    offset = (page - 1) * limit
    try:
        db = get_db()
        owner = q(db, "SELECT id FROM artists WHERE id=%s", (artist_id,))
        if not owner:
            db.close()
            return jsonify({"error": "not found"}), 404
        rows  = qq(db, """
            SELECT fp.post_id, fp.source, fp.post_url, f.name AS filename, f.ext AS file_ext,
                   f.width, f.height, fp.page_num, fp.downloaded_at
            FROM furry_posts fp JOIN files f ON f.id = fp.file_id WHERE fp.artist_id=%s
            ORDER BY fp.downloaded_at DESC LIMIT %s OFFSET %s
        """, (artist_id, limit, offset))
        total = q(db, "SELECT COUNT(*) as c FROM furry_posts WHERE artist_id=%s", (artist_id,))["c"]
        db.close()
        for r in rows:
            if r["downloaded_at"]: r["downloaded_at"] = r["downloaded_at"].isoformat()
        return jsonify({"items": rows, "total": int(total), "page": page})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


# ── Recent ────────────────────────────────────────────────────────────────

@bp.route("/api/artists/<int:artist_id>/forget", methods=["DELETE"])
@admin_required
def api_artist_forget(artist_id):
    """
    Прибирає автора і всі його записи з БД — фізичні файли НЕ чіпає
    взагалі. Для випадків типу помилково проіндексованої особистої папки:
    файли лишаються на диску (і виключені зі сканування назавжди), просто
    записи про них зникають з "Автори"/"Арти".
    """
    try:
        db  = get_db()
        row = q(db, "SELECT artist_name, folder_name FROM artists WHERE id=%s", (artist_id,))
        if not row:
            db.close()
            return jsonify({"error": "Автора не знайдено"}), 404
        with db.cursor() as cur:
            cur.execute("DELETE FROM artists WHERE id=%s", (artist_id,))  # downloads підуть каскадом
        db.commit(); db.close()
        return jsonify({"ok": True, "removed": row["artist_name"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/artists/<int:artist_id>/merge-log")
def api_artists_merge_log(artist_id):
    """Журнал того, які файли виявились візуальними дублікатами під час
    об'єднання авторів у цього автора — 'e621_123 і e621_595 це один і той
    самий арт, лишили тільки один'."""
    try:
        db   = get_db()
        rows = qq(db, """
            SELECT kept_post_id, kept_filename, removed_post_id, removed_filename, hamming_distance, created_at
            FROM merge_duplicate_log WHERE artist_id=%s ORDER BY created_at DESC
        """, (artist_id,))
        db.close()
        for r in rows:
            if r["created_at"]: r["created_at"] = r["created_at"].isoformat()
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/artists/auto-update-bulk", methods=["POST"])
@admin_required
def api_artists_toggle_auto_update_bulk():
    """Вмикає/вимикає авто-оновлення одразу для ВСІХ авторів — кнопки
    'Обрати всіх'/'Зняти всіх' у списку авторів."""
    body = flask_request.get_json(silent=True) or {}
    enabled = 1 if body.get("enabled") else 0
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("UPDATE artists SET auto_update=%s", (enabled,))
            count = cur.rowcount
        db.commit(); db.close()
        return jsonify({"ok": True, "count": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/artists/<int:artist_id>/auto-update", methods=["POST"])
@moderator_required
def api_artists_toggle_auto_update(artist_id):
    """Перемикає, чи перевіряти цього автора на нове при щоденному
    авто-оновленні."""
    body = flask_request.get_json(silent=True) or {}
    enabled = 1 if body.get("enabled") else 0
    try:
        db = get_db()
        art = q(db, "SELECT artist_name, auto_update FROM artists WHERE id=%s", (artist_id,))
        with db.cursor() as cur:
            cur.execute("UPDATE artists SET auto_update=%s WHERE id=%s", (enabled, artist_id))
        db.commit(); db.close()
        if art and bool(art["auto_update"]) != bool(enabled):
            modlog.record("artist.auto_update", f"«{art['artist_name']}»: {'увімкнено' if enabled else 'вимкнено'}",
                          {"artist_id": artist_id, "before": int(bool(art["auto_update"]))})
        return jsonify({"ok": True, "auto_update": bool(enabled)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/artists/check-name")
def api_artists_check_name():
    """
    Перевіряє, чи цей нік уже відомий у бібліотеці — або як точна папка
    автора, або як заявлений раніше псевдонім (через об'єднання). Викликається
    ПЕРЕД завантаженням автора, щоб попередити про можливий дубль-акаунт.
    """
    name = (flask_request.args.get("name") or "").strip().lstrip("@")
    if not name:
        return jsonify({"exists": False})
    folder_name = f"@{name}"
    try:
        db = get_db()
        row = q(db, "SELECT id, artist_name, folder_name FROM artists WHERE folder_name=%s OR artist_name=%s",
                (folder_name, name))
        if not row:
            arow = q(db, """
                SELECT a.id, a.artist_name, a.folder_name FROM artist_aliases al
                JOIN artists a ON a.id = al.artist_id
                WHERE al.alias_name = %s
            """, (name,))
            row = arow
        db.close()
        if row:
            return jsonify({"exists": True, "artist_id": row["id"], "artist_name": row["artist_name"], "folder_name": row["folder_name"]})
        return jsonify({"exists": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


import re as _re_natsort

def _natural_sort_key(s: str):
    """Природне сортування — '2.jpg' йде перед '10.jpg', а не як звичайний
    текстовий порядок (де '10' випадково опинився б перед '2')."""
    return [int(t) if t.isdigit() else t.lower() for t in _re_natsort.split(r'(\d+)', s)]


@bp.route("/api/artists/<int:artist_id>/pool-editor")
def api_artist_pool_editor(artist_id):
    """
    Список файлів для редактора pool — або конкретного pool (subfolder=X),
    або файлів автора без жодного pool (subfolder порожній/не задано).
    """
    subfolder = flask_request.args.get("subfolder", "")
    try:
        db = get_db()
        if subfolder:
            # і файли з теки пулу, і привʼязані до нього з інших пулів
            members, margs = pool_members("{artist_id}=%s AND {pool_name}=%s", (artist_id, subfolder))
            rows = qq(db, f"""
                SELECT f.id, f.name AS filename, m.pool_order, f.ext AS file_ext, m.linked
                FROM {members} m JOIN files f ON f.id = m.file_id
                ORDER BY m.pool_order ASC, f.id ASC
            """, margs)
        else:
            rows = qq(db, """
                SELECT f.id, f.name AS filename, fp.pool_order, f.ext AS file_ext
                FROM furry_posts fp JOIN files f ON f.id = fp.file_id
                WHERE fp.artist_id=%s AND (fp.subfolder IS NULL OR fp.subfolder='') ORDER BY f.id ASC
            """, (artist_id,))
        db.close()
        for r in rows:
            r["stream_url"] = f"/api/gallery/stream/{r['id']}"
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/artists/<int:artist_id>/pool-names")
def api_artist_pool_names(artist_id):
    """Список назв усіх pool цього автора — для вибору в редакторі."""
    try:
        db   = get_db()
        members, margs = pool_members("{artist_id}=%s", (artist_id,))
        rows = qq(db, f"""
            SELECT DISTINCT m.pool_name FROM {members} m ORDER BY m.pool_name
        """, margs)
        db.close()
        return jsonify([r["pool_name"] for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/downloads/<int:download_id>/set-page", methods=["POST"])
@moderator_required
def api_download_set_page(download_id):
    """Проставляє номер сторінки (pool_order) для ОДНОГО конкретного файлу."""
    body  = flask_request.get_json(silent=True) or {}
    order = body.get("pool_order")
    pool  = (body.get("pool") or "").strip() or None   # у якому пулі (файл може бути в кількох)
    try:
        db = get_db()
        old = q(db, """SELECT fp.pool_order, fp.subfolder, f.name FROM furry_posts fp
                       JOIN files f ON f.id = fp.file_id WHERE fp.file_id=%s""", (download_id,))
        if old and pool and pool != (old["subfolder"] or ""):
            link = q(db, "SELECT pool_order FROM furry_pool_links WHERE file_id=%s AND pool_name=%s",
                     (download_id, pool))
            if link:
                old = {**old, "pool_order": link["pool_order"], "subfolder": pool}
            else:
                pool = None
        with db.cursor() as cur:
            set_order(cur, download_id, order, pool)
        db.commit(); db.close()
        if old and old["pool_order"] != order:
            modlog.record("pool.set_page", f"«{old['name']}» у «{old['subfolder']}»: стор. {old['pool_order']} → {order}",
                          {"orders": [{"file_id": download_id, "pool_order": old["pool_order"], "pool": pool}]})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/artists/<int:artist_id>/pool-assign", methods=["POST"])
@moderator_required
def api_artist_pool_assign(artist_id):
    """
    Переносить обрані роботи (уже наявні в архіві, у цього ж автора) у
    pool з вказаною назвою — створює його, якщо такого ще нема. Реально
    переміщує файл на диску/Nextcloud, щоб subfolder у БД відповідав
    справжньому місцю файлу — так само, як і решта переносів у дашборді.
    """
    body          = flask_request.get_json(silent=True) or {}
    download_ids  = body.get("download_ids") or []
    pool_name     = (body.get("pool_name") or "").strip()
    if not download_ids or not pool_name:
        return jsonify({"error": "Вкажи роботи і назву pool"}), 400
    try:
        db  = get_db()
        art = q(db, "SELECT folder_name FROM artists WHERE id=%s", (artist_id,))
        if not art:
            db.close()
            return jsonify({"error": "Автора не знайдено"}), 404
        placeholders = ",".join(["%s"] * len(download_ids))
        rows = qq(db, f"""SELECT f.id, f.name AS filename, fp.subfolder, fp.pool_order
                          FROM furry_posts fp JOIN files f ON f.id = fp.file_id
                          WHERE f.id IN ({placeholders}) AND fp.artist_id=%s""",
                  download_ids + [artist_id])
        # як було до переносу — для відміни з журналу модераторів
        before_rows = [{"file_id": r["id"], "subfolder": r["subfolder"], "pool_order": r["pool_order"]}
                       for r in rows]

        storage = get_storage()

        moved, failed = 0, 0
        for row in rows:
            try:
                # Шляхи всередині сховища: тека автора + (стара) підпапка + ім'я.
                # Теку призначення створює саме сховище.
                folder  = art["folder_name"]
                prefix  = "" if folder == "_root" else f"{folder}/"
                old_sub = f"{row['subfolder']}/" if row.get("subfolder") else ""
                src_rel = f"{prefix}{old_sub}{row['filename']}"
                dst_rel = f"{prefix}{pool_name}/{row['filename']}"
                if src_rel != dst_rel and not storage.exists(dst_rel):
                    storage.move(src_rel, dst_rel)
                moved += 1
            except Exception as e:
                failed += 1
                print(f"  ⚠ Не вдалося перемістити {row['filename']}: {e}")

        with db.cursor() as cur:
            # файли тепер «рідні» в цьому пулі — додаткові привʼязки до нього прибираємо
            cur.execute(f"""SELECT file_id, artist_id, pool_name, pool_order, source FROM furry_pool_links
                            WHERE pool_name=%s AND file_id IN ({placeholders})""", [pool_name] + download_ids)
            removed_links = cur.fetchall()
            cur.execute(f"DELETE FROM furry_pool_links WHERE pool_name=%s AND file_id IN ({placeholders})",
                        [pool_name] + download_ids)
            cur.execute(f"UPDATE furry_posts SET subfolder=%s WHERE file_id IN ({placeholders})",
                        [pool_name] + download_ids)
            # Файли переїхали у сховищі — оновлюємо їхній шлях у ядрі,
            # інакше вони перестануть відкриватись.
            resync_rel_path(db, download_ids)
            # Одразу проставляємо очерговість — за природнім порядком назви,
            # щоб не лишати porожнім/незрозумілим
            members, margs = pool_members("{artist_id}=%s AND {pool_name}=%s", (artist_id, pool_name))
            cur.execute(f"""SELECT f.id, f.name AS filename, m.pool_order, m.linked
                            FROM {members} m JOIN files f ON f.id = m.file_id""", margs)
            pool_files = cur.fetchall()
            # сторінки, що вже були в пулі, теж перенумеруються — запамʼятовуємо
            moved_ids = {int(i) for i in download_ids}
            prev_orders = [{"file_id": r["id"], "pool_order": r["pool_order"],
                            "pool": pool_name if r["linked"] else None}
                           for r in pool_files if r["id"] not in moved_ids]
            pool_files.sort(key=lambda r: _natural_sort_key(r["filename"]))
            for order, r in enumerate(pool_files, start=1):
                set_order(cur, r["id"], order, pool_name)
        db.commit(); db.close()
        modlog.record("pool.assign", f"{len(before_rows)} файл(ів) → пул «{pool_name}»",
                      {"artist_id": artist_id, "pool_name": pool_name, "files": before_rows,
                       "prev_pool_orders": prev_orders, "removed_links": removed_links})
        return jsonify({"ok": True, "moved": moved, "failed": failed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/artists/<int:artist_id>/resort-pool", methods=["POST"])
@moderator_required
def api_artist_resort_pool(artist_id):
    """
    Пересортовує вже наявний pool за природнім порядком назви файлу —
    для випадків, коли очерговість загубилась (напр. після перенесення
    автора в pool до цього фіксу).
    """
    body      = flask_request.get_json(silent=True) or {}
    subfolder = (body.get("subfolder") or "").strip()
    if not subfolder:
        return jsonify({"error": "Вкажи назву pool"}), 400
    try:
        db = get_db()
        with db.cursor() as cur:
            members, margs = pool_members("{artist_id}=%s AND {pool_name}=%s", (artist_id, subfolder))
            cur.execute(f"""SELECT f.id, f.name AS filename, m.pool_order, m.linked
                            FROM {members} m JOIN files f ON f.id = m.file_id""", margs)
            files = cur.fetchall()
            before = [{"file_id": r["id"], "pool_order": r["pool_order"],
                       "pool": subfolder if r["linked"] else None} for r in files]
            files.sort(key=lambda r: _natural_sort_key(r["filename"]))
            for order, r in enumerate(files, start=1):
                set_order(cur, r["id"], order, subfolder)
        db.commit(); db.close()
        modlog.record("pool.resort", f"пул «{subfolder}» ({len(files)} стор.)", {"orders": before})
        return jsonify({"ok": True, "count": len(files)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/artists/<int:source_id>/merge", methods=["POST"])
@admin_required
def api_artists_merge(source_id):
    """
    Об'єднує "source" автора в "target" — реально переносить файли зі
    сховища, переприв'язує всі записи БД, і запам'ятовує стару назву як
    псевдонім (щоб наступного разу при спробі завантажити цей самий нік
    система одразу впізнала, що це вже відомий автор).
    """
    body = flask_request.get_json(silent=True) or {}
    target_id = body.get("target_artist_id")
    if not target_id or int(target_id) == source_id:
        return jsonify({"error": "Вкажи іншого автора для об'єднання"}), 400
    target_id = int(target_id)
    # Опційно — перенести не в корінь автора, а як pool (підпапку) з цією
    # назвою. Для випадку "цей 'автор' насправді просто pool/серія іншого".
    target_subfolder = (body.get("target_subfolder") or "").strip() or None

    try:
        db = get_db()
        src = q(db, "SELECT id, artist_name, folder_name FROM artists WHERE id=%s", (source_id,))
        tgt = q(db, "SELECT id, artist_name, folder_name FROM artists WHERE id=%s", (target_id,))
        if not src or not tgt:
            db.close()
            return jsonify({"error": "Автора не знайдено"}), 404

        rows = qq(db, """SELECT f.id, fp.post_id, f.name AS filename, fp.subfolder, ph.phash
                         FROM furry_posts fp JOIN files f ON f.id = fp.file_id
                         LEFT JOIN file_phash ph ON ph.file_id = f.id
                         WHERE fp.artist_id=%s""", (source_id,))
        tgt_rows = qq(db, """SELECT f.id, fp.post_id, f.name AS filename, ph.phash
                             FROM furry_posts fp JOIN files f ON f.id = fp.file_id
                             JOIN file_phash ph ON ph.file_id = f.id
                             WHERE fp.artist_id=%s""", (target_id,))

        # pHash-перевірка ПЕРЕД переносом — це той самий сценарій, про який
        # питали: "e621_123 у одного, e621_595 у іншого — це один і той самий
        # арт, просто різні ID". Точний post_id тут не збігається, тому раніше
        # такі випадки взагалі не ловились — обидва файли просто дублювались
        # би в об'єднаній бібліотеці. Тепер порівнюємо pHash кожного файлу
        # source з усіма файлами target (той самий XOR+popcount підхід, що
        # й у звичайній перевірці дублікатів при скачуванні).
        PHASH_THRESHOLD = 8
        skip_ids = set()      # download.id з source, які виявились дублікатами — не переносимо
        dup_log_rows = []     # для запису в merge_duplicate_log
        for row in rows:
            if row["phash"] is None:
                continue
            best_dist, best_match = PHASH_THRESHOLD + 1, None
            for trow in tgt_rows:
                dist = bin(row["phash"] ^ trow["phash"]).count("1")
                if dist <= PHASH_THRESHOLD and dist < best_dist:
                    best_dist, best_match = dist, trow
            if best_match:
                skip_ids.add(row["id"])
                dup_log_rows.append((target_id, best_match["post_id"], best_match["filename"],
                                      row["post_id"], row["filename"], best_dist))

        storage_mode = get_storage_mode()
        moved, skipped_dup, failed = 0, 0, 0
        client = None
        base = ""
        if storage_mode != "local":
            from webdav3.client import Client
            nc = get_user_nc_config()
            base = nc.get("base_folder", "").strip("/")
            client = Client({
                "webdav_hostname": nc.get("webdav_url"),
                "webdav_login":    nc.get("username"),
                "webdav_password": nc.get("password"),
                "disable_check":   True,
            })

        for row in rows:
            sub = f"{row['subfolder']}/" if row.get("subfolder") else ""
            # Якщо перетворюємо на pool — усі файли йдуть в ОДНУ підпапку з
            # цією назвою, незалежно від того, яка підпапка (чи її
            # відсутність) була в них раніше
            dst_sub = f"{target_subfolder}/" if target_subfolder else sub
            if row["id"] in skip_ids:
                # Візуальний дублікат уже наявного файлу в target — прибираємо
                # СТАРУ копію фізично (не переносимо, не лишаємо запис у БД),
                # щоб не зберігати той самий арт двічі
                skipped_dup += 1
                try:
                    if storage_mode == "local":
                        src_path = local_file_path(src["folder_name"], row["filename"], row.get("subfolder"))
                        if os.path.isfile(src_path):
                            os.remove(src_path)
                    else:
                        src_nc = f"{base}/{src['folder_name']}/{sub}{row['filename']}"
                        client.clean(src_nc)
                except Exception as e:
                    print(f"  ⚠ Не вдалося прибрати дублікат {row['filename']}: {e}")
                continue
            try:
                if storage_mode == "local":
                    src_path = local_file_path(src["folder_name"], row["filename"], row.get("subfolder"))
                    dst_path = local_file_path(tgt["folder_name"], row["filename"], target_subfolder or row.get("subfolder"))
                    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                    if os.path.isfile(src_path) and not os.path.isfile(dst_path):
                        # shutil.move витримує перенесення між різними
                        # файловими системами, os.rename — ні
                        shutil.move(src_path, dst_path)
                    moved += 1
                else:
                    src_nc = f"{base}/{src['folder_name']}/{sub}{row['filename']}"
                    dst_nc = f"{base}/{tgt['folder_name']}/{dst_sub}{row['filename']}"
                    dst_dir = dst_nc.rsplit("/", 1)[0]
                    try:
                        client.mkdir(dst_dir)
                    except Exception:
                        pass
                    client.move(remote_path_from=src_nc, remote_path_to=dst_nc)
                    moved += 1
            except Exception as e:
                failed += 1
                print(f"  ⚠ Не вдалося перенести {row['filename']}: {e}")

        with db.cursor() as cur:
            # Записуємо в журнал, що з чим збіглося — щоб ця інформація не
            # губилась (саме про це й було питання)
            for dl in dup_log_rows:
                cur.execute("""
                    INSERT INTO merge_duplicate_log
                        (artist_id, kept_post_id, kept_filename, removed_post_id, removed_filename, hamming_distance)
                    VALUES (%s,%s,%s,%s,%s,%s)
                """, dl)

            # Прибираємо з БД ті записи source, що виявились pHash-дублікатами
            if skip_ids:
                placeholders = ",".join(["%s"] * len(skip_ids))
                cur.execute(f"DELETE FROM files WHERE id IN ({placeholders})", list(skip_ids))

            # Перепривʼязуємо решту на нового автора; якщо той самий пост уже
            # є в цільового автора (точний збіг post_id) — UPDATE IGNORE просто
            # пропустить конфліктний рядок (через UNIQUE artist_id+post_id+page_num)
            if target_subfolder:
                cur.execute("UPDATE IGNORE downloads SET artist_id=%s, subfolder=%s WHERE artist_id=%s",
                            (target_id, target_subfolder, source_id))
            else:
                cur.execute("UPDATE IGNORE downloads SET artist_id=%s WHERE artist_id=%s", (target_id, source_id))
            # Додаткові пули (одна робота в кількох пулах) — теж до нового автора,
            # інакше вони зникли б разом зі старим записом автора
            cur.execute("UPDATE furry_pool_links SET artist_id=%s WHERE artist_id=%s", (target_id, source_id))
            # Ще й точні збіги post_id, які не змогли перепривʼязатись — прибираємо
            cur.execute("DELETE FROM files WHERE id IN (SELECT file_id FROM furry_posts WHERE artist_id=%s)", (source_id,))

            if target_subfolder:
                # Проставляємо очерговість "по-новому" — за природнім
                # сортуванням назви файлу (щоб "2.jpg" йшло перед "10.jpg",
                # а не як звичайний текстовий порядок), а не лишаємо порожнім
                cur.execute("""SELECT f.id, f.name AS filename
                               FROM furry_posts fp JOIN files f ON f.id = fp.file_id
                               WHERE fp.artist_id=%s AND fp.subfolder=%s""",
                            (target_id, target_subfolder))
                pool_files = cur.fetchall()
                pool_files.sort(key=lambda r: _natural_sort_key(r["filename"]))
                for order, r in enumerate(pool_files, start=1):
                    cur.execute("UPDATE furry_posts SET pool_order=%s WHERE file_id=%s", (order, r["id"]))

            # Запам'ятовуємо стару назву як псевдонім — щоб наступного разу
            # при спробі завантажити цей самий нік система одразу впізнала
            for alias in {src["folder_name"].lstrip("@"), src["artist_name"]}:
                if alias:
                    cur.execute("INSERT IGNORE INTO artist_aliases (artist_id, alias_name) VALUES (%s,%s)",
                                (target_id, alias))
            cur.execute("DELETE FROM artists WHERE id=%s", (source_id,))
        db.commit(); db.close()
        _invalidate_tags_cache(); _invalidate_pools_cache()
        return jsonify({"ok": True, "moved": moved, "failed": failed, "skipped_dup": skipped_dup})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
