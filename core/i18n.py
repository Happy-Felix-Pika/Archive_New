"""
Ядро: переклади інтерфейсу.

Де лежать:
    locales/<мова>.json                         — ядро
    apps/installed/<id>/locales/<мова>.json     — кожен застосунок окремо

Українська — базова мова: текст інтерфейсу пишеться в коді українською,
тож uk.json потрібен лише для ключів (nav.overview тощо). Файл іншої мови —
плоский словник двох видів записів:

    "nav.overview": "Overview"                     ключ  → текст
    "Видалити теку «{0}»": "Delete folder «{0}»"   українська фраза → переклад

{0}, {1}… — змінні частини (імена, числа); у перекладі їх можна
переставляти. Нова мова = новий файл (напр. pl.json) у тих самих теках.

Сервер склеює ядро + всі застосунки в один пакет /i18n.js, а сторінка
сама підставляє переклади (див. static/i18n.js).
"""

import hashlib
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOCALES_DIR = os.path.join(BASE_DIR, "locales")

_cache = {"sig": None, "js": None, "etag": None}


def _read_dir(path: str) -> dict:
    out = {}
    if not os.path.isdir(path):
        return out
    for fn in sorted(os.listdir(path)):
        if not fn.endswith(".json"):
            continue
        lang = fn[:-5]
        try:
            with open(os.path.join(path, fn), encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"  ⚠ Переклад {path}/{fn} не прочитано: {e}")
            continue
        if isinstance(data, dict):
            out[lang] = {str(k): str(v) for k, v in data.items()
                         if not str(k).startswith("_") and isinstance(v, (str, int, float))}
    return out


def _sources() -> list:
    """Теки з перекладами: ядро першим (його записи мають перевагу)."""
    dirs = [LOCALES_DIR]
    try:
        from core.app_manager import APPS_DIR, manager
        for aid in sorted(manager.apps):
            dirs.append(os.path.join(APPS_DIR, aid, "locales"))
    except Exception:
        pass
    return dirs


def _signature(dirs) -> tuple:
    sig = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            p = os.path.join(d, fn)
            try:
                sig.append((p, os.path.getmtime(p)))
            except OSError:
                pass
    return tuple(sig)


def bundle() -> dict:
    """{мова: {ключ/фраза: текст}} — ядро + застосунки."""
    out = {}
    for d in _sources():
        for lang, data in _read_dir(d).items():
            target = out.setdefault(lang, {})
            for k, v in data.items():
                target.setdefault(k, v)
    out.setdefault("uk", {})
    return out


def bundle_js() -> tuple[str, str]:
    """(текст скрипта, etag); перебудовується лише коли змінився якийсь файл."""
    dirs = _sources()
    sig = _signature(dirs)
    if _cache["sig"] != sig:
        data = json.dumps(bundle(), ensure_ascii=False, separators=(",", ":"))
        js = f"window.I18N_BUNDLE={data};\n"
        _cache.update(sig=sig, js=js, etag=hashlib.md5(js.encode()).hexdigest())
    return _cache["js"], _cache["etag"]
