"""
Ядро: «теплі» дані — пораховані заздалегідь, віддаються миттєво.

Для важких зведень, які показуються часто, а змінюються повільно (список
авторів, статистика «Огляду»):

  * рахуються одразу на старті сервера, у фоні — сервер не чекає;
  * запит сторінки отримує готове з пам'яті;
  * після будь-якої зміни в архіві (успішний POST/PUT/PATCH/DELETE або
    подія file.*) позначаються застарілими й за мить перераховуються у
    фоні. Запит НІКОЛИ не чекає на важкий підрахунок: поки фон рахує,
    віддається попередній результат (наживо — лише найперший раз, коли
    взагалі нічого ще немає, або коли підрахунок дешевий — до 0.3 с);
  * поки йде низка змін (розбір дублікатів — десятки кліків за хвилину),
    фон рахує не частіше ніж раз на 10 с (важкий — ще рідше, пропорційно
    тому, скільки він рахується) і ніколи не два підрахунки разом;
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
_MIN_GAP = 10               # фоновий перерахунок після змін — не частіше
_CHEAP = 0.3                # підрахунок, дешевший за це, можна зробити прямо в запиті


def _persist_path(name: str) -> str:
    import os
    from core.paths import CONFIG_FILE
    d = os.path.join(os.path.dirname(os.path.abspath(CONFIG_FILE)), "cache", "warm")   # підтека: чистка кешу картинок її не чіпає
    return os.path.join(d, f"{name}.json")


class WarmCache:
    """persist=True — останній результат ще й пишеться на диск (JSON), і після
    перезапуску сервера віддається одразу, поки фон рахує свіжий. Лише для
    даних, що без втрат переживають JSON (списки/словники рядків і чисел)."""

    def __init__(self, name: str, compute, interval: int = 60, persist: bool = False):
        self.name = name
        self.persist = persist
        self._compute = compute
        self.interval = interval
        self._data = None
        self._dirty = True
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._last_get = 0.0
        self._last_done = 0.0
        self.computes = 0            # скільки разів рахувалось (для перевірки)
        self.cost = 0.0              # скільки тривав останній підрахунок, с
        self._thread = None
        _ALL.append(self)
        if persist:
            self._load_saved()

    def _load_saved(self):
        import json
        try:
            with open(_persist_path(self.name), encoding="utf-8") as f:
                self._data = json.load(f)
            self._dirty = True          # збережене — лише до першого свіжого підрахунку
        except (OSError, ValueError):
            pass

    def _save(self, data):
        import json, os
        try:
            path = _persist_path(self.name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception as e:
            print(f"  ⚠ Кеш «{self.name}» не збережено на диск: {e}")

    # ── читання ──────────────────────────────────────────────────────
    def get(self):
        self._last_get = time.monotonic()
        if self._data is None:
            self.refresh()
        elif self._dirty:
            if self.cost <= _CHEAP and not self._lock.locked():
                self.refresh()           # дешево — одразу свіже
            else:
                self._wake.set()         # важко — віддаємо попереднє, фон перерахує
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
            t0 = time.monotonic()
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
            if self.persist:
                self._save(data)
            self.cost = time.monotonic() - t0
            self.computes += 1
            self._last_done = time.monotonic()

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
                # Швидке клацання (напр. розбір дублікатів — десятки дій за
                # хвилину): рахуємо не частіше ніж раз на _MIN_GAP секунд.
                # Зміни за цей час лише ставлять позначку, а останній
                # перерахунок після паузи все одно відбудеться.
                wait = max(_MIN_GAP, self.cost * 5) - (time.monotonic() - self._last_done)
                if wait > 0:
                    time.sleep(wait)
                    self._wake.clear()
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
