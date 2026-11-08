"""
Ядро: теги файлів і пропозиції тегів.

  * Модератор і адмін додають/прибирають теги одразу (модератор — із
    записом у журнал, звідки адмін може відмінити).
  * Користувач теги лише ПРОПОНУЄ: «додати тег X до файлу», «прибрати тег X».
  * Видалити тег з архіву цілком (з усіх файлів одразу) може лише адмін;
    модератор це теж лише пропонує.

Пропозиції вирішують модератор або адмін (видалення тегу цілком — лише адмін)
на сторінці «На перевірку» → «Теги».
"""

from core import modlog
from core.db import get_db, q, qq
from core.modlog import UndoError

CATEGORIES = ("character", "species", "general", "other")


class TagError(Exception):
    """Зрозуміла причина відмови."""


def _norm(name):
    return (name or "").strip().lower()[:100]


def _invalidate():
    try:
        from blueprints.tags import _invalidate_tags_cache
        _invalidate_tags_cache()
    except Exception:
        pass


def _file_name(db, file_id):
    r = q(db, "SELECT name FROM files WHERE id=%s", (file_id,))
    return r["name"] if r else f"#{file_id}"


# ── Прямі дії (модератор / адмін) ────────────────────────────────────

def add_tag(file_id, name, category="other", log=True):
    """Додати тег файлу. Повертає (тег, чи створено новий тег)."""
    name = _norm(name)
    if not name:
        raise TagError("Порожня назва тегу")
    category = category if category in CATEGORIES else "other"
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT 1 FROM files WHERE id=%s", (file_id,))
        if not cur.fetchone():
            raise TagError("Файл не знайдено")
        cur.execute("SELECT id, name, category FROM tags WHERE name=%s", (name,))
        tag = cur.fetchone()
        created = False
        if not tag:
            cur.execute("INSERT INTO tags (name, category) VALUES (%s,%s)", (name, category))
            tag = {"id": cur.lastrowid, "name": name, "category": category}
            created = True
        cur.execute("INSERT IGNORE INTO download_tags (download_id, tag_id) VALUES (%s,%s)",
                    (file_id, tag["id"]))
        added = cur.rowcount > 0
    db.commit()
    _invalidate()
    if log and (added or created):
        modlog.record("tag.add", f"«{name}» → «{_file_name(db, file_id)}»",
                      {"file_id": file_id, "tag_id": tag["id"], "tag_name": name, "created_tag": created})
    return tag, created


def remove_tag(file_id, tag_id, log=True):
    db = get_db()
    tag = q(db, "SELECT id, name FROM tags WHERE id=%s", (tag_id,))
    with db.cursor() as cur:
        cur.execute("DELETE FROM download_tags WHERE download_id=%s AND tag_id=%s", (file_id, tag_id))
        removed = cur.rowcount > 0
    db.commit()
    _invalidate()
    if log and removed and tag:
        modlog.record("tag.remove", f"«{tag['name']}» з «{_file_name(db, file_id)}»",
                      {"file_id": file_id, "tag_id": tag_id, "tag_name": tag["name"]})
    return removed


def set_tags(file_id, tag_ids):
    db = get_db()
    before = [r["tag_id"] for r in qq(db, "SELECT tag_id FROM download_tags WHERE download_id=%s", (file_id,))]
    with db.cursor() as cur:
        cur.execute("DELETE FROM download_tags WHERE download_id=%s", (file_id,))
        for tid in tag_ids:
            cur.execute("INSERT IGNORE INTO download_tags (download_id, tag_id) VALUES (%s,%s)", (file_id, tid))
    db.commit()
    _invalidate()
    if sorted(before) != sorted(int(t) for t in tag_ids):
        modlog.record("tag.set", f"«{_file_name(db, file_id)}»: {len(before)} → {len(tag_ids)} тег(ів)",
                      {"file_id": file_id, "before": before})


def create_tag(name, category="other"):
    name = _norm(name)
    if not name:
        raise TagError("Порожня назва тегу")
    category = category if category in CATEGORIES else "other"
    db = get_db()
    with db.cursor() as cur:
        cur.execute("INSERT IGNORE INTO tags (name, category) VALUES (%s,%s)", (name, category))
        created = cur.rowcount > 0
        cur.execute("SELECT id, name, category FROM tags WHERE name=%s", (name,))
        tag = cur.fetchone()
    db.commit()
    _invalidate()
    if created:
        modlog.record("tag.create", f"«{name}»", {"tag_id": tag["id"], "tag_name": name})
    return tag


def delete_tag(tag_id):
    """Видалити тег з архіву цілком (лише адмін)."""
    db = get_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM tags WHERE id=%s", (tag_id,))
        cur.execute("""UPDATE tag_suggestions SET status='obsolete'
                       WHERE tag_id=%s AND status='pending'""", (tag_id,))
    db.commit()
    _invalidate()


# ── Пропозиції ───────────────────────────────────────────────────────

def suggest(action, user_id, *, file_id=None, tag_id=None, name=None, category="other"):
    """
    Нова пропозиція. Та сама (дія, файл, тег) від будь-кого ще не вирішена —
    повертаємо її, а не плодимо дублікати.
    """
    db = get_db()
    if action in ("remove", "delete_tag"):
        t = q(db, "SELECT id, name, category FROM tags WHERE id=%s", (tag_id,))
        if not t:
            raise TagError("Такого тегу немає")
        name, category = t["name"], t["category"]
    name = _norm(name)
    if not name:
        raise TagError("Порожня назва тегу")
    category = category if category in CATEGORIES else "other"
    if file_id is not None and not q(db, "SELECT 1 AS x FROM files WHERE id=%s", (file_id,)):
        raise TagError("Файл не знайдено")
    if action == "add" and file_id is not None:
        if q(db, """SELECT 1 AS x FROM download_tags dt JOIN tags t ON t.id = dt.tag_id
                    WHERE dt.download_id=%s AND t.name=%s""", (file_id, name)):
            raise TagError(f"Тег «{name}» у файлу вже є")
    existing = q(db, """SELECT * FROM tag_suggestions WHERE status='pending' AND action=%s
                        AND file_id <=> %s AND tag_name=%s""", (action, file_id, name))
    if existing:
        return _out(existing)
    with db.cursor() as cur:
        cur.execute("""INSERT INTO tag_suggestions (action, file_id, tag_id, tag_name, category, suggested_by)
                       VALUES (%s,%s,%s,%s,%s,%s)""", (action, file_id, tag_id, name, category, user_id))
        sid = cur.lastrowid
    db.commit()
    return _out(q(db, "SELECT * FROM tag_suggestions WHERE id=%s", (sid,)))


def _out(r):
    if not r:
        return None
    r = dict(r)
    for k in ("created_at", "decided_at"):
        if r.get(k):
            r[k] = r[k].isoformat()
    return r


def list_pending(include_delete_tag=True):
    db = get_db()
    rows = qq(db, f"""
        SELECT s.*, u.display_name AS by_name, u.email AS by_email, f.name AS file_name, f.ext AS file_ext,
               (SELECT COUNT(*) FROM download_tags dt WHERE dt.tag_id = s.tag_id) AS tag_usage
        FROM tag_suggestions s
        LEFT JOIN users u ON u.id = s.suggested_by
        LEFT JOIN files f ON f.id = s.file_id
        WHERE s.status='pending' {"" if include_delete_tag else "AND s.action <> 'delete_tag'"}
        ORDER BY s.id
    """)
    out = []
    for r in rows:
        r = _out(r)
        r["by"] = r.pop("by_name", None) or r.pop("by_email", None) or f"#{r['suggested_by']}"
        r.pop("by_email", None)
        out.append(r)
    return out


def pending_count(include_delete_tag=True):
    db = get_db()
    r = q(db, f"""SELECT COUNT(*) AS n FROM tag_suggestions WHERE status='pending'
                  {"" if include_delete_tag else "AND action <> 'delete_tag'"}""")
    return int(r["n"]) if r else 0


def for_file(file_id, user_id=None):
    """Невирішені пропозиції для файлу (user_id — лише свої)."""
    db = get_db()
    sql = "SELECT * FROM tag_suggestions WHERE status='pending' AND file_id=%s"
    args = [file_id]
    if user_id:
        sql += " AND suggested_by=%s"
        args.append(user_id)
    return [_out(r) for r in qq(db, sql + " ORDER BY id", args)]


def withdraw(sugg_id, user_id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("""DELETE FROM tag_suggestions WHERE id=%s AND suggested_by=%s AND status='pending'""",
                    (sugg_id, user_id))
        ok = cur.rowcount > 0
    db.commit()
    return ok


def decide(sugg_id, accept: bool, by_user: int, role: str):
    """Прийняти/відхилити. Видалення тегу цілком — лише адмін."""
    db = get_db()
    s = q(db, "SELECT * FROM tag_suggestions WHERE id=%s", (sugg_id,))
    if not s or s["status"] != "pending":
        raise TagError("Пропозицію вже вирішено або її немає")
    if s["action"] == "delete_tag" and role != "admin":
        raise TagError("Видалити тег з архіву цілком може лише адмін")
    effect = {}
    if accept:
        if s["action"] == "add":
            tag, created = add_tag(s["file_id"], s["tag_name"], s["category"], log=False)
            effect = {"tag_id": tag["id"], "created_tag": created}
        elif s["action"] == "remove":
            remove_tag(s["file_id"], s["tag_id"], log=False)
            effect = {"tag_id": s["tag_id"]}
        elif s["action"] == "delete_tag":
            delete_tag(s["tag_id"])
    with db.cursor() as cur:
        cur.execute("""UPDATE tag_suggestions SET status=%s, decided_by=%s, decided_at=NOW() WHERE id=%s""",
                    ("accepted" if accept else "rejected", by_user, sugg_id))
    db.commit()
    verb = {"add": "додати", "remove": "прибрати", "delete_tag": "видалити тег"}[s["action"]]
    target = f" у «{_file_name(db, s['file_id'])}»" if s["file_id"] else ""
    modlog.record("tagsugg.accept" if accept else "tagsugg.reject",
                  f"«{verb} {s['tag_name']}»{target}",
                  {"suggestion_id": sugg_id, "action": s["action"], "file_id": s["file_id"],
                   "tag_name": s["tag_name"], **effect})


# ── Відміна рішень модератора ────────────────────────────────────────

def _reopen(sugg_id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("""UPDATE tag_suggestions SET status='pending', decided_by=NULL, decided_at=NULL
                       WHERE id=%s""", (sugg_id,))
    db.commit()


def _undo_accept(p):
    db = get_db()
    with db.cursor() as cur:
        if p["action"] == "add":
            cur.execute("DELETE FROM download_tags WHERE download_id=%s AND tag_id=%s", (p["file_id"], p["tag_id"]))
            if p.get("created_tag"):
                cur.execute("SELECT 1 FROM download_tags WHERE tag_id=%s LIMIT 1", (p["tag_id"],))
                if not cur.fetchone():
                    cur.execute("DELETE FROM tags WHERE id=%s", (p["tag_id"],))
        elif p["action"] == "remove":
            cur.execute("SELECT id FROM tags WHERE id=%s", (p["tag_id"],))
            if not cur.fetchone():
                raise UndoError(f"Тегу «{p['tag_name']}» уже немає")
            cur.execute("INSERT IGNORE INTO download_tags (download_id, tag_id) VALUES (%s,%s)",
                        (p["file_id"], p["tag_id"]))
    db.commit()
    _invalidate()
    _reopen(p["suggestion_id"])
    return "пропозиція знову чекає рішення"


def _undo_reject(p):
    _reopen(p["suggestion_id"])
    return "пропозиція знову чекає рішення"


def register_undo():
    modlog.register("tagsugg.accept", _undo_accept, "Прийняв пропозицію тегу")
    modlog.register("tagsugg.reject", _undo_reject, "Відхилив пропозицію тегу")
