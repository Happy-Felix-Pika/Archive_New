"""imgcache"""

import os, time



import tempfile as _tempfile

# Диск-кеш для зображень — в /data/cache якщо є (Docker), інакше /tmp
_DATA_CACHE = "/data/cache"
_IMG_CACHE_DIR = _DATA_CACHE if os.path.isdir(_DATA_CACHE) else \
                 os.path.join(_tempfile.gettempdir(), "furry_gal_cache")
_IMG_CACHE_TTL = 2 * 3600
os.makedirs(_IMG_CACHE_DIR, exist_ok=True)

def _cleanup_img_cache():
    """Видаляє файли кешу старше TTL."""
    now = time.time()
    removed = 0
    try:
        for fname in os.listdir(_IMG_CACHE_DIR):
            fpath = os.path.join(_IMG_CACHE_DIR, fname)
            try:
                if now - os.path.getatime(fpath) > _IMG_CACHE_TTL:
                    os.remove(fpath)
                    removed += 1
            except Exception:
                pass
    except Exception:
        pass
    if removed:
        print(f"  🗑 Кеш галереї: видалено {removed} файлів")

def _cache_cleanup_loop():
    """Фоновий потік — чистить кеш раз на годину."""
    while True:
        time.sleep(3600)
        _cleanup_img_cache()
