"""Теги"""

import time

from flask import Blueprint, jsonify, request as flask_request


from core.auth import moderator_required
from core.db import get_db, qq
from core.warmcache import WarmCache

bp = Blueprint("tags", __name__)


# ── Tags API ──────────────────────────────────────────────────────────────

_CAT_ORDER = {"character": 0, "species": 1, "general": 2, "other": 3}


def _compute_tags(db):
    """Два прості запити замість JOIN + GROUP BY + сортування в базі: лічильники
    — прохід лише по індексу tag_id, назви — маленька таблиця tags; зводимо
    й сортуємо в пам'яті. На великому архіві в рази швидше."""
    import pymysql
    with db.cursor(pymysql.cursors.Cursor) as cur:
        cur.execute("SELECT tag_id, COUNT(*) FROM download_tags GROUP BY tag_id")
        counts = dict(cur.fetchall())
        cur.execute("SELECT id, name, category FROM tags")
        rows = [{"id": i, "name": n, "category": c, "usage_count": int(counts.get(i, 0))}
                for i, n, c in cur.fetchall()]
    rows.sort(key=lambda r: (_CAT_ORDER.get(r["category"], 9), -r["usage_count"], r["name"]))
    return rows


# Рахується у фоні на старті й після змін; запит отримує готовий список.
# persist — після перезапуску (оновлення контейнера) теги є одразу, а не
# через хвилину-дві, поки на великому архіві рахується перший список
tags_cache = WarmCache("tags", _compute_tags, interval=600, persist=True)


def _invalidate_tags_cache():
    tags_cache.invalidate()


def _rebuild_tags_cache():
    """Сумісність зі старим запуском (main.py): кеш і так гріється сам."""
    return True


def _tags_cache_background_loop():
    """Сумісність: фонове оновлення тепер робить WarmCache."""
    return


@bp.route("/api/tags")
def api_tags_list():
    # Найперший підрахунок на великому архіві може йти хвилини — запит його
    # не чекає (інакше проксі обриває з’єднання, а сторінка лишалась без тегів
    # до перезавантаження): «ще рахую», браузер спитає знову за кілька секунд.
    data = tags_cache.get(wait=False)
    if data is None:
        return jsonify({"loading": True}), 202
    return jsonify(data)


@bp.route("/api/tags", methods=["POST"])
def api_tags_create():
    """Новий тег (без файлу) — модератор або адмін. Користувач пропонує теги
    до конкретних файлів."""
    from core.auth import current_role
    from core.tagops import TagError, create_tag
    if current_role() == "user":
        return jsonify({"error": "Тег можна лише запропонувати — до конкретного файлу"}), 403
    body = flask_request.get_json(silent=True) or {}
    try:
        return jsonify(create_tag(body.get("name"), body.get("category") or "other"))
    except TagError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/tags/<int:tag_id>", methods=["DELETE"])
def api_tags_delete(tag_id):
    """
    Видалити тег з архіву цілком (з усіх файлів одразу) — лише адмін.
    Модератор це пропонує: пропозиція чекає на адміна.
    """
    from core.auth import current_role, current_user_id
    from core.tagops import TagError, delete_tag, suggest
    role = current_role()
    try:
        if role == "admin":
            delete_tag(tag_id)
            return jsonify({"ok": True})
        if role == "moderator":
            s = suggest("delete_tag", current_user_id(), tag_id=tag_id)
            return jsonify({"ok": True, "suggested": True, "suggestion": s})
        return jsonify({"error": "Видаляти теги може лише адмін"}), 403
    except TagError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Пропозиції тегів ─────────────────────────────────────────────────

@bp.route("/api/tag-suggestions")
@moderator_required
def api_tag_suggestions_list():
    from core.auth import current_role
    from core.tagops import list_pending
    is_admin = current_role() == "admin"
    items = list_pending()
    for s in items:
        s["can_decide"] = is_admin or s["action"] != "delete_tag"
    return jsonify({"items": items})


@bp.route("/api/tag-suggestions/count")
@moderator_required
def api_tag_suggestions_count():
    from core.auth import current_role
    from core.tagops import pending_count
    return jsonify({"count": pending_count(include_delete_tag=current_role() == "admin")})


@bp.route("/api/tag-suggestions/<int:sid>/<verdict>", methods=["POST"])
@moderator_required
def api_tag_suggestions_decide(sid, verdict):
    from core.auth import current_role, current_user_id
    from core.tagops import TagError, decide
    if verdict not in ("accept", "reject"):
        return jsonify({"error": "accept або reject"}), 400
    try:
        decide(sid, verdict == "accept", current_user_id(), current_role())
        return jsonify({"ok": True})
    except TagError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/tag-suggestions/<int:sid>", methods=["DELETE"])
def api_tag_suggestions_withdraw(sid):
    """Відкликати свою пропозицію, поки її не вирішили."""
    from core.auth import current_user_id
    from core.tagops import withdraw
    if not withdraw(sid, current_user_id()):
        return jsonify({"error": "Пропозицію не знайдено або її вже вирішено"}), 404
    return jsonify({"ok": True})
