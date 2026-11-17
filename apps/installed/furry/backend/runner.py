"""Запуск архіватора та логи"""

import json, os, queue, subprocess, sys, threading, time
from datetime import datetime, timedelta

from flask import Blueprint, Response, jsonify, request as flask_request


from core.auth import admin_required, current_role, get_user_settings, moderator_required
from core.auth import current_user_id, login_required
from core.jobs import (cancel_job, create_job, fail_job, finish_job,
                       list_jobs, update_progress)
from core.logs import _log_history, _log_lock, _log_queue, _push_log

from core.storage import get_storage_mode


# Скрипт архіватора — частина furry, не ядра
ARCHIVER_SCRIPT = "furry_archiver.py"

bp = Blueprint("runner", __name__)


_active_process: subprocess.Popen | None = None
_modlog_entry = {}          # job_id -> запис у журналі модераторів (щоб дописати кінець)
_process_lock:   threading.Lock        = threading.Lock()


def get_auto_update_hour() -> int:
    """Година доби (0-23), о якій запускається щоденне авто-оновлення.
    Зберігається в тому самому спільному конфізі, що й решта налаштувань."""
    try:
        h = int(get_user_settings().get("scheduler", {}).get("auto_update_hour", 4))
        return h if 0 <= h <= 23 else 4
    except Exception:
        return 4


def _auto_update_scheduler_loop():
    """Раз на добу, о обраній годині, запускає перевірку на нове для
    авторів з увімкненим авто-оновленням (галочка в 'Автори') — той самий
    підпроцес і той самий лог, що й для ручного запуску, просто по розкладу.
    Годину перечитує щоразу заново — щоб зміна в налаштуваннях підхопилась
    з наступного циклу, без перезапуску сервера."""
    global _active_process
    while True:
        target_hour = get_auto_update_hour()
        now = datetime.now()
        next_run = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        time.sleep((next_run - now).total_seconds())
        with _process_lock:
            if _active_process and _active_process.poll() is None:
                _push_log(f"[{datetime.now().strftime('%H:%M:%S')}] ⏭ Щоденне авто-оновлення пропущено — вже щось запущено")
                continue
            try:
                env = os.environ.copy()
                env["PYTHONUNBUFFERED"] = "1"
                env["PYTHONIOENCODING"] = "utf-8"
                proc = subprocess.Popen(
                    [sys.executable, "-u", ARCHIVER_SCRIPT, "--user-id", "1", "--auto-update"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    bufsize=1, encoding="utf-8", errors="replace", env=env,
                )
                _active_process = proc
                _push_log(f"[{datetime.now().strftime('%H:%M:%S')}] ⏰ Щоденне авто-оновлення почалось")
                # created_by=None означає "за розкладом, не людиною"
                job_id = create_job("furry.auto-update", {"pid": proc.pid})
                threading.Thread(target=_stream_process, args=(proc, job_id), daemon=True).start()
            except Exception as e:
                _push_log(f"[{datetime.now().strftime('%H:%M:%S')}] ✗ Не вдалося запустити авто-оновлення: {e}")
                # Невдалий запуск теж має лишити слід, інакше знову
                # вийде "нічого не сталось і невідомо чому"
                fail_job(create_job("furry.auto-update"), f"Не вдалося запустити: {e}")


def _stream_process(proc, job_id=None):
    """
    Читає вивід архіватора і пише його в лог.

    job_id — запис у таблиці jobs, якщо запуск зареєстровано. Лог живе
    в пам'яті й зникає при перезапуску, а запис у jobs лишається: саме
    по ньому потім видно, чи спрацювало нічне авто-оновлення і чим
    воно скінчилось.
    """
    global _active_process
    last_line = ""
    for line in iter(proc.stdout.readline, ""):
        stripped = line.rstrip()
        if stripped:
            last_line = stripped
            _push_log(f"[{datetime.now().strftime('%H:%M:%S')}] {stripped}")
            if job_id:
                update_progress(job_id, message=stripped)
    proc.wait()
    _push_log(f"[{datetime.now().strftime('%H:%M:%S')}] ── Завершено (код {proc.returncode}) ──")
    # запуск модератора: час кінця — щоб відміна знала, що саме він скачав
    entry = _modlog_entry.pop(job_id, None) if job_id else None
    if entry:
        try:
            from core import modlog
            from core.db import _connect
            c = _connect()
            with c.cursor() as cur:
                cur.execute("SELECT NOW() AS t")
                t = cur.fetchone()["t"]
            c.close()
            modlog.patch_payload(entry, finished_at=t.isoformat(sep=" "))
        except Exception as e:
            _push_log(f"  ⚠ журнал модераторів: {e}")
    if job_id:
        if proc.returncode == 0:
            finish_job(job_id, {"код": proc.returncode, "останній рядок": last_line[:300]})
        else:
            fail_job(job_id, f"Архіватор завершився з кодом {proc.returncode}. {last_line[:300]}")
    with _process_lock:
        _active_process = None
    # Інші застосунки можуть доробити своє по завершенні (дублікати:
    # автоматична заміна файлів, які поступились сторінці pool).
    try:
        from core import events
        events.emit("furry.archiver.finished", {"returncode": proc.returncode, "job_id": job_id})
    except Exception as e:
        _push_log(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠ Обробка після архіватора: {e}")

# Що модератор може запускати: лише одного автора — скачати, знайти його
# дублікати, перевірити пули. Повні проходи, синхронізація, очищення,
# міграції — лише адмін.
_MOD_FLAGS = {"artist", "find_duplicates", "recheck_pools", "subfolder", "dry_run"}
_ADMIN_FLAGS = {"migrate_tags", "sync_all", "cleanup", "cleanup_dry", "backfill_sizes"}


@bp.route("/api/run", methods=["POST"])
@moderator_required
def api_run():
    global _active_process
    with _process_lock:
        if _active_process and _active_process.poll() is None:
            return jsonify({"error": "Архиватор вже запущено"}), 409
        body  = flask_request.get_json(silent=True) or {}
        role  = current_role()
        if role != "admin":
            if not (body.get("artist") or "").strip():
                return jsonify({"error": "Модератор запускає архіватор лише для одного автора"}), 403
            if any(body.get(k) for k in _ADMIN_FLAGS):
                return jsonify({"error": "Синхронізація, очищення й міграції — лише для адміна"}), 403
        flags = ["--user-id", "1"]
        if body.get("dry_run"):      flags.append("--dry-run")
        if body.get("migrate_tags"): flags.append("--migrate-tags")
        if body.get("sync_all"):     flags.append("--sync-all")
        if body.get("artist"):       flags.extend(["--artist", str(body.get("artist"))])
        if body.get("cleanup_dry"):  flags.append("--cleanup-dry")
        elif body.get("cleanup"):    flags.append("--cleanup")
        if body.get("find_duplicates"): flags.append("--find-duplicates")
        if body.get("recheck_pools"):   flags.append("--recheck-pools")
        if body.get("backfill_sizes"):  flags.append("--backfill-sizes")
        if body.get("subfolder"):    flags.extend(["--subfolder", str(body.get("subfolder"))])
        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"   # форсируем UTF-8 на Windows
            proc = subprocess.Popen(
                [sys.executable, "-u", ARCHIVER_SCRIPT] + flags,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",               # читаем stdout как UTF-8
                errors="replace",               # заменяем нераспознанные байты на ?
                env=env,
            )
            _active_process = proc
            job_id = create_job("furry.run", {"flags": flags}, user_id=current_user_id())
            if role == "moderator":
                from core import modlog
                from core.db import get_db, q
                started = q(get_db(), "SELECT NOW() AS t")["t"]
                what = ("перевірка пулів" if body.get("recheck_pools") else
                        "пошук дублікатів" if body.get("find_duplicates") else "скачування")
                entry = modlog.record("archiver.run", f"{what}: «{body['artist']}»",
                                      {"artist": str(body["artist"]).strip().lstrip("@"),
                                       "started_at": started.isoformat(sep=" "), "job_id": job_id,
                                       "kind": what},
                                      undoable=not body.get("dry_run") and not body.get("find_duplicates"))
                if entry:
                    _modlog_entry[job_id] = entry
            threading.Thread(target=_stream_process, args=(proc, job_id), daemon=True).start()
            return jsonify({"ok": True, "pid": proc.pid, "job_id": job_id})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

@bp.route("/api/stop", methods=["POST"])
@admin_required
def api_stop():
    global _active_process
    with _process_lock:
        if not _active_process or _active_process.poll() is not None:
            return jsonify({"error": "Процес не запущено"}), 409
        _active_process.terminate()
        return jsonify({"ok": True})

@bp.route("/api/status")
@moderator_required
def api_status():
    with _process_lock:
        running = bool(_active_process and _active_process.poll() is None)
        pid     = _active_process.pid if running else None
    return jsonify({"running": running, "pid": pid, "storage_mode": get_storage_mode()})

@bp.route("/api/logs/clear", methods=["POST"])
@admin_required
def api_logs_clear():
    with _log_lock:
        _log_history.clear()
    # очищаємо чергу
    while not _log_queue.empty():
        try: _log_queue.get_nowait()
        except: break
    return jsonify({"ok": True})

@bp.route("/api/logs/stream")
@moderator_required
def api_logs_stream():
    def generate():
        try:
            yield "retry: 1000\n\n"
            # Відправляємо збережену історію при підключенні
            with _log_lock:
                history_snapshot = list(_log_history)
            for line in history_snapshot:
                yield f"data: {json.dumps(line)}\n\n"
            # Слухаємо нові повідомлення
            while True:
                try:
                    msg = _log_queue.get(timeout=20)
                    yield f"data: {json.dumps(msg)}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
        except GeneratorExit:
            pass  # клієнт відключився — виходимо чисто
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})
