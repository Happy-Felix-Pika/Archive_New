#!/usr/bin/env python3
"""Точка входу. Раніше все це було всередині dashboard_server.py."""

import threading

from core.app import create_app
from core.db_schema import ensure_tables
from core.jobs import recover_stuck_jobs, start_worker
from core.imgcache import _cache_cleanup_loop, _cleanup_img_cache
from blueprints.tags import _rebuild_tags_cache, _tags_cache_background_loop

app = create_app()


def _drop_old_nextcloud_settings():
    """Nextcloud прибрано повністю — стираємо його старі налаштування (там був пароль)."""
    try:
        from core.config import load_config, save_config
        cfg = load_config()
        if any(k in cfg for k in ("nextcloud", "nextcloud_db")):
            for k in ("nextcloud", "nextcloud_db"):
                cfg.pop(k, None)
            save_config(cfg)
            print("  ✓ Старі налаштування Nextcloud прибрано з конфігу")
    except Exception as e:
        print(f"  ⚠ Не вдалося прибрати налаштування Nextcloud: {e}")


def _startup():
    _drop_old_nextcloud_settings()
    ensure_tables()
    try:
        recover_stuck_jobs()   # задачі, перервані попереднім запуском
    except Exception as e:
        # нова установка без бази — сервер усе одно стартує й показує
        # екран підключення до бази
        print(f"  ⚠ База недоступна: {e}")
    start_worker()         # черга фонових задач ядра
    _cleanup_img_cache()
    threading.Thread(target=_cache_cleanup_loop, daemon=True).start()
    print("Прогрів кешу тегів...")
    _rebuild_tags_cache()
    threading.Thread(target=_tags_cache_background_loop, daemon=True).start()


if __name__ == "__main__":
    print("╔══════════════════════════════════════╗")
    print("║   Archive Dashboard                  ║")
    print("║   http://localhost:5000              ║")
    print("╚══════════════════════════════════════╝")
    _startup()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
