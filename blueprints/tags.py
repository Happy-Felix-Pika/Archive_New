"""Теги"""

import time

from flask import Blueprint, jsonify, request as flask_request


from core.auth import moderator_required
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
