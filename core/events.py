"""
Ядро: події.

Тридцять рядків, які сильно зменшують зв'язність: застосунки
дізнаються про те, що сталось у ядрі, не змінюючи саме ядро.

    core.events.on("file.created", handler)
    core.events.emit("file.created", {"file_id": 42})

Виклики синхронні й по черзі. Ніякої черги й брокера — для архіву
на одному сервері це було б зайвим ускладненням.

Помилка в одному підписнику не ламає ні інших підписників, ні те
місце, що подію породило: інакше зламаний застосунок зупиняв би
завантаження файлів.

Імена подій ядра:
    file.created / file.updated / file.deleted
    user.created / user.deleted
    job.started  / job.completed / job.failed
"""

import threading
import traceback

_subscribers: dict[str, list] = {}
_lock = threading.Lock()


def on(event: str, fn):
    """Підписатись на подію."""
    with _lock:
        _subscribers.setdefault(event, []).append(fn)


def off(event: str, fn):
    with _lock:
        if event in _subscribers and fn in _subscribers[event]:
            _subscribers[event].remove(fn)


def emit(event: str, payload: dict | None = None):
    """Повідомити підписників. Ніколи не кидає виняток назовні."""
    with _lock:
        handlers = list(_subscribers.get(event, []))
    for fn in handlers:
        try:
            fn(payload or {})
        except Exception as e:
            print(f"  ⚠ Обробник події '{event}' впав: {e}")
            traceback.print_exc()


def subscribers(event: str | None = None):
    """Хто на що підписаний — для сторінки застосунків і для налагодження."""
    with _lock:
        if event:
            return list(_subscribers.get(event, []))
        return {k: len(v) for k, v in _subscribers.items() if v}


# ─────────────────────────────────────────────────────────────────────
#  Фільтри: як події, але з результатом.
#
#  Подія каже "щось сталося". Фільтр каже "ось дані — доповни їх".
#  Так застосунок додає свої поля у відповідь ядра, а ядро про ці поля
#  нічого не знає:
#
#      def add_artist(data, file_id, db):
#          data["artist"] = ...
#          return data
#
#      core.events.add_filter("file.meta", add_artist)
#
#  Ядро викликає:
#
#      data = events.apply_filters("file.meta", data, file_id=42, db=db)
#
#  Якщо фільтр упав, дані йдуть далі НЕЗМІНЕНИМИ: зламаний застосунок
#  не повинен ламати відповідь ядра.
#
#  Фільтри ядра:
#      file.meta   — картка файлу. Контекст: file_id, db
# ─────────────────────────────────────────────────────────────────────

_filters: dict[str, list] = {}


def add_filter(name: str, fn):
    with _lock:
        _filters.setdefault(name, []).append(fn)


def apply_filters(name: str, value, **ctx):
    with _lock:
        fns = list(_filters.get(name, []))
    for fn in fns:
        try:
            result = fn(value, **ctx)
            if result is not None:
                value = result
        except Exception as e:
            print(f"  ⚠ Фільтр '{name}' впав: {e}")
            traceback.print_exc()
    return value
