"""
Ядро: фонові задачі.

Ядро дає механізм, застосунки дають самі задачі. Ядро не знає, що
таке "скачати автора з e621" — воно вміє лише завести задачу, дати
їй виконавця, показати хід і записати підсумок.

Застосунок реєструє виконавця один раз при завантаженні:

    from core.jobs import register_handler, create_job

    def sync(job, params):
        job.progress(10, "качаю список")
        ...
        return {"files": 42}          # піде в result

    register_handler("furry.sync", sync)

і запускає задачу звідки завгодно:

    job_id = create_job("furry.sync", {"artist": "kenket"}, user_id=1)

Виконавець отримує об'єкт job з двома методами: progress(відсоток,
повідомлення) і cancelled() — довгі задачі мають самі час від часу
питати, чи їх не скасували.
"""

import json
import threading
import time
import traceback

from core.db import get_db, q, qq

# type -> функція-виконавець
_HANDLERS: dict[str, callable] = {}

_worker_started = False
_worker_lock = threading.Lock()

POLL_SECONDS = 2


def register_handler(job_type: str, fn):
    """Прив'язати виконавця до типу задачі. Викликається при завантаженні застосунку."""
    _HANDLERS[job_type] = fn


def registered_types() -> list:
    return sorted(_HANDLERS)


# ── створення і життєвий цикл ────────────────────────────────

def create_job(job_type: str, params: dict | None = None, user_id: int | None = None) -> int:
    """Поставити задачу в чергу. Повертає її id. Виконання почнеться саме."""
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (type, status, params, created_by) VALUES (%s,'pending',%s,%s)",
                (job_type, json.dumps(params or {}, ensure_ascii=False), user_id))
            job_id = cur.lastrowid
        db.commit()
        return job_id
    finally:
        db.close()


def update_progress(job_id: int, percent: int | None = None, message: str | None = None):
    sets, args = [], []
    if percent is not None:
        sets.append("progress=%s"); args.append(max(0, min(100, int(percent))))
    if message is not None:
        sets.append("message=%s");  args.append(str(message)[:512])
    if not sets:
        return
    args.append(job_id)
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=%s", args)
        db.commit()
    finally:
        db.close()


def finish_job(job_id: int, result: dict | None = None):
    _close(job_id, "done", result=result)


def fail_job(job_id: int, error: str):
    _close(job_id, "failed", error=error)


def cancel_job(job_id: int) -> bool:
    """
    Попросити задачу зупинитись.

    Якщо вона ще не почалась — знімаємо одразу. Якщо вже виконується,
    лише ставимо позначку: реально зупинитись має сам виконавець,
    перевіряючи job.cancelled(). Перервати чужий код посеред роботи
    ми не можемо й не намагаємось — так і файл недописаний лишиться.
    """
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""UPDATE jobs SET status='cancelled',
                                  finished_at=CASE WHEN status='pending' THEN NOW() ELSE finished_at END
                           WHERE id=%s AND status IN ('pending','running')""", (job_id,))
            changed = cur.rowcount > 0
        db.commit()
        return changed
    finally:
        db.close()


def _close(job_id: int, status: str, result=None, error=None):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""UPDATE jobs SET status=%s, finished_at=NOW(),
                                  progress=CASE WHEN %s='done' THEN 100 ELSE progress END,
                                  result=%s, error=%s
                           WHERE id=%s""",
                        (status, status,
                         json.dumps(result, ensure_ascii=False) if result is not None else None,
                         error, job_id))
        db.commit()
    finally:
        db.close()


# ── читання ──────────────────────────────────────────────────

def get_job(job_id: int) -> dict | None:
    db = get_db()
    try:
        return q(db, "SELECT * FROM jobs WHERE id=%s", (job_id,))
    finally:
        db.close()


def list_jobs(job_type: str | None = None, limit: int = 50) -> list:
    db = get_db()
    try:
        if job_type:
            return qq(db, "SELECT * FROM jobs WHERE type=%s ORDER BY id DESC LIMIT %s",
                      (job_type, int(limit)))
        return qq(db, "SELECT * FROM jobs ORDER BY id DESC LIMIT %s", (int(limit),))
    finally:
        db.close()


# ── те, що бачить виконавець ─────────────────────────────────

class JobContext:
    """Передається у виконавця. Усе, що йому треба знати про свою задачу."""

    def __init__(self, job_id: int, job_type: str):
        self.id = job_id
        self.type = job_type
        self._last_check = 0.0
        self._cancelled = False

    def progress(self, percent=None, message=None):
        update_progress(self.id, percent, message)

    def cancelled(self) -> bool:
        """Чи попросили зупинитись. Питати не частіше ніж раз на секунду — щоб
        довгий цикл не бомбардував базу запитами."""
        now = time.time()
        if self._cancelled or now - self._last_check < 1.0:
            return self._cancelled
        self._last_check = now
        row = get_job(self.id)
        self._cancelled = bool(row and row["status"] == "cancelled")
        return self._cancelled


# ── виконання ────────────────────────────────────────────────

def _claim_next() -> dict | None:
    """
    Взяти наступну задачу з черги.

    Позначаємо 'running' тим самим запитом, яким і вибираємо, щоб дві
    копії воркера (напр. після перезапуску) не схопили одну задачу.
    """
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""SELECT id, type FROM jobs
                           WHERE status='pending' ORDER BY id LIMIT 1""")
            row = cur.fetchone()
            if not row:
                return None
            cur.execute("""UPDATE jobs SET status='running', started_at=NOW()
                           WHERE id=%s AND status='pending'""", (row["id"],))
            if cur.rowcount == 0:
                return None          # хтось випередив
        db.commit()
        return row
    finally:
        db.close()


def _run_one(row: dict):
    job_id, job_type = row["id"], row["type"]
    handler = _HANDLERS.get(job_type)
    if not handler:
        fail_job(job_id, f"Немає виконавця для задачі '{job_type}'")
        return

    job = JobContext(job_id, job_type)
    full = get_job(job_id) or {}
    try:
        params = json.loads(full.get("params") or "{}")
    except Exception:
        params = {}

    try:
        result = handler(job, params)
        if job.cancelled():
            return                    # статус уже 'cancelled', не перетираємо
        finish_job(job_id, result if isinstance(result, dict) else None)
    except Exception as e:
        print(f"  ⚠ Задача {job_id} ({job_type}) впала: {e}")
        traceback.print_exc()
        fail_job(job_id, f"{type(e).__name__}: {e}")


def _worker_loop():
    while True:
        try:
            row = _claim_next()
            if row:
                _run_one(row)
                continue              # одразу дивимось, чи є наступна
        except Exception as e:
            print(f"  ⚠ Черга задач: {e}")
        time.sleep(POLL_SECONDS)


def start_worker():
    """Запустити обробку черги. Безпечно викликати кілька разів."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_worker_loop, daemon=True, name="jobs-worker").start()


def recover_stuck_jobs():
    """
    При старті: задачі в статусі 'running' лишились від попереднього
    запуску — процес перервали, і продовжити їх нікому. Позначаємо
    як невдалі, щоб вони не висіли 'виконуються' вічно.
    """
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("""UPDATE jobs SET status='failed', finished_at=NOW(),
                                  error='Перервано перезапуском сервера'
                           WHERE status='running'""")
            n = cur.rowcount
        db.commit()
        if n:
            print(f"  ℹ Незавершених задач після перезапуску: {n}")
    finally:
        db.close()
