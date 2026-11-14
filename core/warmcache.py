"""
Ядро: «теплі» дані — пораховані заздалегідь, віддаються миттєво.

Для важких зведень, які показуються часто, а змінюються повільно (список
авторів, статистика «Огляду»):

  * рахуються одразу на старті сервера, у фоні — сервер не чекає;
  * запит сторінки отримує готове з пам'яті;
  * після будь-якої зміни в архіві (успішний POST/PUT/PATCH/DELETE або
    подія file.*) позначаються застарілими й за мить перераховуються у
    фоні; якщо сторінку відкрили раніше, ніж фон устиг — рахується одразу,
    тож після власної дії людина завжди бачить свіжі числа;
  * раз на хвилину перераховуються самі — так підхоплюється те, що зробив
    архіватор (він окремий процес і подій сюди не шле). Коли архівом
    ніхто не користується, — рідше, щоб не навантажувати базу дарма.

Числа не рахуються «по одному файлу на кожну дію»: так кожне нове місце,
що змінює файли, мусило б не забути оновити лічильники, а пропущене —
тихо псувало б їх до перезапуску. Тут же гірше, ніж «відстало на хвилину»,
бути не може.

    artists_cache = WarmCache("artists", compute_fn)   # compute_fn(db) -> дані
    data = artists_cache.get()
"""

import threading
import time

from core.db import _connect

_ALL = []
_ACTIVE_FOR = 15 * 60       # стільки після останнього перегляду оновлюємо щохвилини
_IDLE_INTERVAL = 10 * 60    # коли ніхто не дивиться — раз на 10 хв


class WarmCache:
    def __init__(self, name: str, compute, interval: int = 60):
        self.name = name
        self._compute = compute
        self.interval = interval
        self._data = None
        self._dirty = True
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._last_get = 0.0
        self._thread = None
        _ALL.append(self)

    # ── читання ──────────────────────────────────────────────────────
    def get(self):
        self._last_get = time.monotonic()
        if self._data is None or self._dirty:
            self.refresh()
        return self._data

    def peek(self):
        """Що є зараз, без перерахунку (може бути None)."""
        return self._data

    # ── перерахунок ──────────────────────────────────────────────────
    def refresh(self, force: bool = False):
        with self._lock:
            # поки чекали на замок, хтось інший уже порахував свіже
            if not force and self._data is not None and not self._dirty:
                return
            self._dirty = False          # зміна під час підрахунку знову поставить True
            db = _connect()
            try:
                data = self._compute(db)
            except Exception as e:
                self._dirty = True
                print(f"  ⚠ Кеш «{self.name}» не порахувався: {e}")
                if self._data is None:
                    raise
                return
            finally:
                try:
                    db.close()
                except Exception:
                    pass
            self._data = data

    def invalidate(self):
        self._dirty = True
        self._wake.set()

    # ── фон ──────────────────────────────────────────────────────────
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name=f"warmcache-{self.name}", daemon=True)
        self._thread.start()

    def _loop(self):
        try:
            self.refresh(force=True)     # прогрів на старті
        except Exception:
            pass
        while True:
            active = time.monotonic() - self._last_get < _ACTIVE_FOR
            woke = self._wake.wait(self.interval if active else _IDLE_INTERVAL)
            if woke:
                time.sleep(0.5)          # пачка змін підряд — один перерахунок
                self._wake.clear()
                if not self._dirty:
                    continue
            try:
                self.refresh(force=True)
            except Exception:
                pass


def invalidate_all(*_args, **_kwargs):
    for c in list(_ALL):
        c.invalidate()


def start_all():
    for c in list(_ALL):
        c.start()


def install(flask_app):
    """Підписка на зміни: успішні запити, що щось міняють, і події файлів."""
    from flask import request as flask_request
    from core import events

    @flask_app.after_request
    def _warmcache_after(resp):
        if flask_request.method in ("POST", "PUT", "PATCH", "DELETE") and resp.status_code < 400:
            invalidate_all()
        return resp

    for ev in ("file.created", "file.deleted", "file.moved", "file.restored",
               "file.state_restore", "furry.archiver.finished"):
        events.on(ev, invalidate_all)
