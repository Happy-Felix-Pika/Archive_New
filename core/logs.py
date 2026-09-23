"""logs"""

import queue, threading



_log_queue:      queue.Queue           = queue.Queue(maxsize=2000)
_log_history:    list                  = []          # зберігаємо всі рядки сесії
_log_lock:       threading.Lock        = threading.Lock()


def _push_log(line: str):
    """Додає рядок в чергу і в історію."""
    with _log_lock:
        _log_history.append(line)
        if len(_log_history) > 5000:
            _log_history.pop(0)
    try:
        _log_queue.put_nowait(line)
    except Exception:
        pass
