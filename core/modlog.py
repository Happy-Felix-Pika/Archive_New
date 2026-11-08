"""
Ядро: журнал дій модераторів з відміною.

Кожна дія модератора (завантаження, переміщення, видалення в кошик, теги,
черга «На перевірку», пули, запуск архіватора…) записується сюди разом із
тим, що потрібно, щоб її відмінити (payload). Адмін бачить журнал і
відміняє будь-який запис однією кнопкою.

Хто що вміє відміняти — реєструє той, хто цю дію виконує:

    modlog.register("file.upload", undo_fn, "Завантажив файл")

а в місці самої дії:

    modlog.record("file.upload", f"«{name}» → {folder}", {"file_id": fid})

record() пише лише дії МОДЕРАТОРА: адмін — власник архіву, його дії не
журналюються; звичайний користувач нічого, що треба відміняти, не робить
(теги він лише пропонує).
"""

import json
from datetime import date, datetime

from core.db import _connect


class UndoError(Exception):
    """Чому відміна не вийшла (показується адміну)."""


_handlers = {}      # action -> {"undo": fn | None, "label": str}


def register(action: str, undo=None, label: str | None = None):
    _handlers[action] = {"undo": undo, "label": label or action}


def label_of(action: str) -> str:
    return (_handlers.get(action) or {}).get("label") or action


def _json(v):
    def conv(o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if isinstance(o, (bytes, bytearray)):
            return None
        raise TypeError(type(o).__name__)
    return json.dumps(v or {}, ensure_ascii=False, default=conv)


def record(action: str, summary: str, payload: dict | None = None, *,
           undoable: bool | None = None, user_id: int | None = None, role: str | None = None):
    """
    Записати дію модератора. Повертає id запису або None (дію робив не
    модератор). Окреме зʼєднання з базою: запис не має залежати від того,
    що відбувається з транзакцією самої дії.
    """
    try:
        from core.auth import current_role, current_user_id
        role = role or current_role()
        if role != "moderator":
            return None
        user_id = user_id or current_user_id()
        if undoable is None:
            undoable = bool((_handlers.get(action) or {}).get("undo"))
        db = _connect()
        try:
            with db.cursor() as cur:
                cur.execute("""INSERT INTO mod_actions (user_id, action, summary, payload, undoable)
                               VALUES (%s,%s,%s,%s,%s)""",
                            (user_id, action, (summary or "")[:500], _json(payload), 1 if undoable else 0))
                new_id = cur.lastrowid
            db.commit()
            return new_id
        finally:
            db.close()
    except Exception as e:
        print(f"  ⚠ Журнал модераторів: не вдалося записати «{action}»: {e}")
        return None


def patch_payload(entry_id: int, **values):
    """Дописати в payload (напр. коли архіватор закінчив — час завершення)."""
    if not entry_id:
        return
    db = _connect()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT payload FROM mod_actions WHERE id=%s", (entry_id,))
            row = cur.fetchone()
            if not row:
                return
            data = json.loads(row["payload"] or "{}")
            data.update(values)
            cur.execute("UPDATE mod_actions SET payload=%s WHERE id=%s", (_json(data), entry_id))
        db.commit()
    finally:
        db.close()


def list_entries(limit=100, offset=0, user_id=None, only_active=False):
    db = _connect()
    try:
        where, args = [], []
        if user_id:
            where.append("m.user_id=%s"); args.append(int(user_id))
        if only_active:
            where.append("m.undone_at IS NULL")
        sql = """SELECT m.*, u.display_name, u.email, ub.display_name AS undone_by_name
                 FROM mod_actions m
                 LEFT JOIN users u  ON u.id = m.user_id
                 LEFT JOIN users ub ON ub.id = m.undone_by"""
        if where:
            sql += " WHERE " + " AND ".join(where)
        with db.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM mod_actions m" + (" WHERE " + " AND ".join(where) if where else ""), args)
            total = int(cur.fetchone()["n"])
            cur.execute(sql + " ORDER BY m.id DESC LIMIT %s OFFSET %s", args + [int(limit), int(offset)])
            rows = cur.fetchall()
    finally:
        db.close()
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "user_id": r["user_id"],
            "user": r["display_name"] or r["email"] or f"#{r['user_id']}",
            "action": r["action"], "label": label_of(r["action"]), "summary": r["summary"],
            "undoable": bool(r["undoable"]) and bool((_handlers.get(r["action"]) or {}).get("undo")),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "undone_at": r["undone_at"].isoformat() if r["undone_at"] else None,
            "undone_by": r["undone_by_name"],
            "undo_error": r["undo_error"],
        })
    return out, total


def undo(entry_id: int, by_user: int) -> dict:
    """Відмінити запис. UndoError — якщо не можна (з поясненням)."""
    db = _connect()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM mod_actions WHERE id=%s", (entry_id,))
            row = cur.fetchone()
    finally:
        db.close()
    if not row:
        raise UndoError("Запису немає")
    if row["undone_at"]:
        raise UndoError("Уже відмінено")
    h = _handlers.get(row["action"]) or {}
    if not row["undoable"] or not h.get("undo"):
        raise UndoError("Цю дію відмінити не можна — вона лише записана в журнал")
    payload = json.loads(row["payload"] or "{}")
    try:
        note = h["undo"](payload)
    except UndoError as e:
        _mark(entry_id, error=str(e))
        raise
    except Exception as e:
        _mark(entry_id, error=f"{type(e).__name__}: {e}")
        raise UndoError(f"Не вдалося відмінити: {e}")
    _mark(entry_id, by_user=by_user)
    return {"ok": True, "note": note}


def _mark(entry_id, by_user=None, error=None):
    db = _connect()
    try:
        with db.cursor() as cur:
            if error is not None:
                cur.execute("UPDATE mod_actions SET undo_error=%s WHERE id=%s", (error[:500], entry_id))
            else:
                cur.execute("""UPDATE mod_actions SET undone_at=NOW(), undone_by=%s, undo_error=NULL
                               WHERE id=%s""", (by_user, entry_id))
        db.commit()
    finally:
        db.close()
