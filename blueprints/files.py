"""Операції над окремим файлом (видалення, мета, теги, улюблене)"""

import os

from flask import Blueprint, jsonify, request as flask_request


from core.auth import admin_required, current_user_id, get_user_nc_config, login_required
from core.db import get_db, q, qq, table_exists
from core.storage_api import StorageError, get_storage
from core.files_api import TrashError, trash_file, get_file, local_path
from core.storage import get_storage_mode, local_file_path

bp = Blueprint("files", __name__)


@bp.route("/api/download/<int:download_id>/favorite", methods=["POST"])
def api_download_toggle_favorite(download_id):
    """Перемикає позначку '★ улюблене' для файлу — персонально для кожного
    юзера, а не спільно на всіх (раніше було одне поле на всю бібліотеку)."""
    user_id = current_user_id()
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT id FROM user_favorites WHERE user_id=%s AND download_id=%s", (user_id, download_id))
            existing = cur.fetchone()
            if existing:
                cur.execute("DELETE FROM user_favorites WHERE id=%s", (existing["id"],))
                new_val = False
            else:
                cur.execute("INSERT INTO user_favorites (user_id, download_id) VALUES (%s,%s)", (user_id, download_id))
                new_val = True
        db.commit(); db.close()
        return jsonify({"is_favorite": new_val})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/download/<int:download_id>", methods=["DELETE"])
@admin_required
def api_download_delete(download_id):
    """
    Видалення файлу. Файл НЕ зникає одразу — він іде в кошик, звідки його
    можна відновити з тегами, автором і всім іншим або прибрати назавжди.
    """
    try:
        trash_id = trash_file(download_id, current_user_id())
        return jsonify({"ok": True, "trash_id": trash_id})
    except TrashError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/download/<int:download_id>/meta", methods=["POST"])
@admin_required
def api_download_meta_update(download_id):
    """
    Ручне редагування файлу адміном: джерело (e621/inkbunny/невідоме),
    посилання на оригінал, і автор (перейменовує автора-папку цього файлу —
    вплине на всі файли цієї ж папки, бо автор у нас прив'язаний до папки).
    Порожнє значення source/post_url — означає "невідомо", зберігається як NULL.
    """
    body = flask_request.get_json(silent=True) or {}
    try:
        db  = get_db()
        row = q(db, "SELECT artist_id FROM furry_posts WHERE file_id=%s", (download_id,))
        if not row:
            db.close()
            return jsonify({"error": "not found"}), 404

        with db.cursor() as cur:
            if "source" in body:
                src = (body.get("source") or "").strip() or None
                if src not in (None, "e621", "inkbunny"):
                    db.close()
                    return jsonify({"error": "Джерело може бути тільки e621, inkbunny або порожнім"}), 400
                cur.execute("UPDATE furry_posts SET source=%s WHERE file_id=%s", (src, download_id))

            if "post_url" in body:
                url = (body.get("post_url") or "").strip() or None
                cur.execute("UPDATE furry_posts SET post_url=%s WHERE file_id=%s", (url, download_id))

            if "artist_name" in body:
                name = (body.get("artist_name") or "").strip()
                if name:
                    cur.execute("UPDATE artists SET artist_name=%s WHERE id=%s",
                                (name, row["artist_id"]))
        db.commit()
        db.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/download/<int:download_id>/tags")
def api_download_tags_get(download_id):
    """Теги конкретного файлу."""
    try:
        db   = get_db()
        rows = qq(db, """
            SELECT t.id, t.name, t.category
            FROM tags t
            JOIN download_tags dt ON dt.tag_id = t.id
            WHERE dt.download_id = %s
            ORDER BY t.name
        """, (download_id,))
        db.close()
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/download/<int:download_id>/tags", methods=["POST"])
def api_download_tags_set(download_id):
    """Замінити теги файлу (приймає список tag_id)."""
    body    = flask_request.get_json(silent=True) or {}
    tag_ids = body.get("tag_ids", [])
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("DELETE FROM download_tags WHERE download_id=%s", (download_id,))
            for tid in tag_ids:
                cur.execute("INSERT IGNORE INTO download_tags (download_id, tag_id) VALUES (%s,%s)",
                            (download_id, tid))
        db.commit(); db.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/download/<int:download_id>/tags/add", methods=["POST"])
def api_download_tag_add(download_id):
    body     = flask_request.get_json(silent=True) or {}
    name     = (body.get("name") or "").strip().lower()[:100]
    category = body.get("category") or "other"
    if category not in ("character","species","general","other"):
        category = "other"
    if not name:
        return jsonify({"error": "name required"}), 400
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("INSERT IGNORE INTO tags (name, category) VALUES (%s,%s)", (name, category))
            cur.execute("SELECT id, name, category FROM tags WHERE name=%s", (name,))
            tag = cur.fetchone()
            cur.execute("INSERT IGNORE INTO download_tags (download_id, tag_id) VALUES (%s,%s)",
                        (download_id, tag["id"]))
        db.commit(); db.close()
        return jsonify(tag)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/download/<int:download_id>/tags/<int:tag_id>", methods=["DELETE"])
def api_download_tag_remove(download_id, tag_id):
    """Видалити тег з файлу."""
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("DELETE FROM download_tags WHERE download_id=%s AND tag_id=%s",
                        (download_id, tag_id))
        db.commit(); db.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/recent")
@login_required
def api_recent():
    """
    Останні файли архіву.

    Це універсальна річ — просто нещодавно додані файли. Якщо встановлено
    застосунок furry, додаємо до кожного рядка автора й посилання на пост;
    якщо ні — таблиці немає, і в полях буде None. Саме тому приєднання
    умовне: ядро мусить працювати без застосунку.
    """
    limit = min(int(flask_request.args.get("limit", 50)), 500)
    db = get_db()
    try:
        if table_exists(db, "furry_posts"):
            rows = qq(db, """
                SELECT f.id, f.name AS filename, f.ext AS file_ext, f.size_bytes,
                       f.created_at AS downloaded_at,
                       fp.post_id, fp.source, fp.post_url, fp.page_num,
                       a.artist_name
                FROM files f
                LEFT JOIN furry_posts fp ON fp.file_id = f.id
                LEFT JOIN artists a      ON a.id       = fp.artist_id
                ORDER BY f.created_at DESC, f.id DESC
                LIMIT %s
            """, (limit,))
        else:
            rows = qq(db, """
                SELECT f.id, f.name AS filename, f.ext AS file_ext, f.size_bytes,
                       f.created_at AS downloaded_at,
                       NULL AS post_id, NULL AS source, NULL AS post_url,
                       NULL AS page_num, NULL AS artist_name
                FROM files f
                ORDER BY f.created_at DESC, f.id DESC
                LIMIT %s
            """, (limit,))
        for r in rows:
            if r.get("downloaded_at"):
                r["downloaded_at"] = r["downloaded_at"].isoformat()
        return jsonify(rows)
    finally:
        db.close()
