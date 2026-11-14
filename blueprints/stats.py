"""Статистика архіву"""


import time

from flask import Blueprint, jsonify


from core.auth import get_user_nc_config
from core import events
from core.db import get_db, q, qq
from core.nextcloud import get_nc_db

bp = Blueprint("stats", __name__)

# «Огляд» оновлюється кожні 20 с у кожного відкритого адміна, а числа там
# змінюються повільно — рахуємо не частіше, ніж раз на _CACHE_TTL секунд.
_CACHE_TTL = 15
_cache = {"at": 0.0, "data": None}


@bp.route("/api/stats")
def api_stats():
    """
    Статистика архіву.

    Ядро рахує тільки своє: скільки файлів, скільки за добу, які формати.
    Автори, e621, дублікати — це дані застосунків, і вони дописують свої
    картки через фільтр 'stats.overview'. Вимкнули застосунок — його
    чисел тут немає взагалі, а не «0».

    cards — список карток для «Огляду»:
        {"id": "...", "label": {"uk": ...}, "value": 12, "hint": "...", "color": "c-accent"}
    blocks — більші блоки (таблиці) того ж вигляду:
        {"id": "...", "title": {...}, "rows": [{"label":..,"value":..}, ...]}
    """
    now = time.monotonic()
    if _cache["data"] is not None and now - _cache["at"] < _CACHE_TTL:
        return jsonify(_cache["data"])
    db = None
    try:
        db = get_db()
        total   = q(db, "SELECT COUNT(*) AS c FROM files")["c"]
        last24h = q(db, "SELECT COUNT(*) AS c FROM files WHERE created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)")["c"]
        formats = qq(db, """SELECT f.ext AS file_ext, COUNT(*) AS cnt
                            FROM files f WHERE f.ext IS NOT NULL
                            GROUP BY f.ext ORDER BY cnt DESC LIMIT 10""")
        data = {
            "db_status":  "online",
            "db_total":   int(total),
            "db_last24h": int(last24h),
            "formats":    [{"ext": r["file_ext"], "cnt": int(r["cnt"])} for r in formats],
            "cards":      [],
            "blocks":     [],
        }
        data = events.apply_filters("stats.overview", data, db=db) or data
        _cache.update(at=time.monotonic(), data=data)
        return jsonify(data)
    except Exception as e:
        return jsonify({"db_status": "offline", "error": str(e),
                        "db_total": 0, "db_last24h": 0, "formats": [], "cards": [], "blocks": []})
    finally:
        if db:
            db.close()
