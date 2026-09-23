"""Теги"""

import time

from flask import Blueprint, jsonify, request as flask_request


from core.db import get_db, qq

bp = Blueprint("tags", __name__)


# ── Tags API ──────────────────────────────────────────────────────────────

_tags_cache = {"data": None, "ts": 0}
_TAGS_CACHE_TTL = 3600  # 1 год — фонове оновлення перебудовує кеш раніше, ніж він встигне протухнути

def _invalidate_tags_cache():
    _tags_cache["data"] = None

def _rebuild_tags_cache():
    """Рахує список тегів наживо і кладе в кеш. Викликається і при старті
    (щоб кеш був теплий одразу), і у фоновому потоці регулярно — тому
    жоден користувач ніколи не ловить важкий запит наживо."""
    try:
        db   = get_db()
        rows = qq(db, """
            SELECT t.id, t.name, t.category, COUNT(dt.download_id) AS usage_count
            FROM tags t
            LEFT JOIN download_tags dt ON dt.tag_id = t.id
            GROUP BY t.id
            ORDER BY t.category, usage_count DESC, t.name ASC
        """)
        db.close()
        for r in rows:
            r["usage_count"] = int(r["usage_count"] or 0)
        _tags_cache["data"] = rows
        _tags_cache["ts"]   = time.time()
        return True
    except Exception as e:
        print(f"  ⚠ Не вдалося оновити кеш тегів: {e}")
        return False

def _tags_cache_background_loop():
    """Фоновий потік — оновлює кеш тегів заздалегідь, ще до того як він
    протухне, щоб ЖОДЕН запит користувача не потрапив на живий підрахунок."""
    while True:
        time.sleep(_TAGS_CACHE_TTL - 30)  # трохи раніше за TTL, з запасом
        _rebuild_tags_cache()

@bp.route("/api/tags")
def api_tags_list():
    # Кеш прогрітий при старті й оновлюється фоново — тут просто віддаємо
    # готове. Якщо з якоїсь причини кеш ще порожній (перший запит одразу
    # після старту, фонове оновлення ще не встигло) — рахуємо наживо один раз.
    if _tags_cache["data"] is None:
        _rebuild_tags_cache()
    return jsonify(_tags_cache["data"] or [])


@bp.route("/api/tags", methods=["POST"])
def api_tags_create():
    body     = flask_request.get_json(silent=True) or {}
    name     = (body.get("name") or "").strip().lower()[:100]
    category = body.get("category") or "other"
    if name not in ("character","species","general","other"):
        category = "other"
    if not name:
        return jsonify({"error": "name required"}), 400
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("INSERT IGNORE INTO tags (name, category) VALUES (%s,%s)", (name, category))
            cur.execute("SELECT id, name, category FROM tags WHERE name=%s", (name,))
            tag = cur.fetchone()
        db.commit(); db.close()
        _invalidate_tags_cache()
        return jsonify(tag)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/tags/<int:tag_id>", methods=["DELETE"])
def api_tags_delete(tag_id):
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute("DELETE FROM tags WHERE id=%s", (tag_id,))
        db.commit(); db.close()
        _invalidate_tags_cache()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
