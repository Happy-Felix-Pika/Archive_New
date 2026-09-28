"""
Статистика Nextcloud: скільки тек авторів і файлів e621/Inkbunny.

Жила в ядрі, хоча рахує суто furry-речі — теки «@автор» і файли з іменами
e621_* та ib_*. Тепер тут; адреса лишилась тією ж.
"""

from flask import Blueprint, jsonify

from core.auth import get_user_nc_config
from core.db import q
from core.nextcloud import get_nc_db

bp = Blueprint("furry_ncstats", __name__)


@bp.route("/api/nc-stats")
def api_nc_stats():
    """Статистика Nextcloud через її власну БД (read-only)."""
    try:
        nc_conn = get_nc_db(1)
        nc_cfg  = get_user_nc_config()

        # Базовий шлях зі спільних налаштувань адміна
        base_folder = nc_cfg.get("base_folder", "").strip("/")
        artist_path_pattern = f"files/{base_folder}/@%"   # файли авторів
        files_path_pattern  = f"files/{base_folder}/@%/%"  # файли всередині папок авторів

        # Кількість авторів — папки @author всередині base_folder
        r_artists = q(nc_conn,
            "SELECT COUNT(*) AS c"
            " FROM oc_filecache"
            " WHERE path LIKE %s"
            "   AND path NOT LIKE %s"  # тільки сам рівень, не глибше
            "   AND mimetype = (SELECT id FROM oc_mimetypes WHERE mimetype='httpd/unix-directory')",
            (artist_path_pattern, files_path_pattern)
        )

        # Файли всередині папок авторів
        r_total = q(nc_conn,
            "SELECT"
            "  COUNT(*) AS total,"
            "  SUM(name LIKE %s) AS e621,"
            "  SUM(name LIKE %s) AS inkbunny"
            " FROM oc_filecache"
            " WHERE path LIKE %s"
            "   AND mimetype != (SELECT id FROM oc_mimetypes WHERE mimetype='httpd/unix-directory')"
            "   AND name != 'download.txt'",
            ('e621\\_%', 'ib\\_%', files_path_pattern)
        )

        nc_conn.close()

        return jsonify({
            "nc_status":   "online",
            "nc_artists":  int(r_artists["c"]    or 0),
            "nc_total":    int(r_total["total"]   or 0),
            "nc_e621":     int(r_total["e621"]    or 0),
            "nc_inkbunny": int(r_total["inkbunny"]or 0),
        })

    except ValueError as e:
        return jsonify({"nc_status": "not_configured", "error": str(e)}), 200
    except Exception as e:
        return jsonify({"nc_status": "offline", "error": str(e)}), 503
