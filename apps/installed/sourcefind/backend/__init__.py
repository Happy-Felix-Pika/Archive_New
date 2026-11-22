"""Пошук джерел: для файлів без посилання на оригінал знаходить його за
картинкою, ставить посилання й теги. Файли не переносяться.

Адреси: /api/apps/sourcefind/...
"""

import json

from flask import Blueprint, jsonify, request

from . import finder as F

bp = Blueprint("sourcefind", __name__)
core = None

JOB = "scan"


# ── налаштування ─────────────────────────────────────────────────────

def _settings():
    s = core.settings.all() if core else {}
    return {"fuzzy_key": s.get("fuzzy_key") or "", "use_iqdb": s.get("use_iqdb", True),
            "use_fluffle": s.get("use_fluffle", True)}


def _make_finder(log=print):
    s = _settings()
    return F.Finder(fuzzy_key=s["fuzzy_key"], use_iqdb=s["use_iqdb"], use_fluffle=s["use_fluffle"], log=log)


def _require_mod():
    from core.auth import is_current_user_moderator
    if not is_current_user_moderator():
        return jsonify({"error": "Лише для модераторів"}), 403
    return None


# ── застосування знайденого ──────────────────────────────────────────

def _save_tags(db, file_id, tags) -> int:
    if not tags:
        return 0
    tag_map = {"other": tags} if isinstance(tags, list) else tags
    n = 0
    with db.cursor() as cur:
        for cat, names in tag_map.items():
            if cat not in ("character", "species", "general", "other"):
                cat = "other"
            for name in names or []:
                name = str(name).strip().lower()[:100]
                if not name:
                    continue
                cur.execute("INSERT IGNORE INTO tags (name, category) VALUES (%s,%s)", (name, cat))
                cur.execute("UPDATE tags SET category=%s WHERE name=%s AND category='other'", (cat, name))
                cur.execute("SELECT id FROM tags WHERE name=%s", (name,))
                row = cur.fetchone()
                if row:
                    cur.execute("INSERT IGNORE INTO download_tags (download_id, tag_id) VALUES (%s,%s)",
                                (file_id, row["id"]))
                    n += cur.rowcount
    return n


def apply_candidate(db, fnd, file_id, cand, cands, method_status="applied") -> int:
    """Посилання на оригінал + теги. Повертає кількість доданих тегів."""
    tags = None
    try:
        tags = fnd.tags_for(cand)
    except Exception as e:
        print(f"  ⚠ Теги для {cand['site']} {cand.get('site_id')}: {e}")
    added = _save_tags(db, file_id, tags)
    with db.cursor() as cur:
        # Файл автора (furry) без посилання — дописуємо прямо туди
        try:
            if cand["site"] in ("e621", "inkbunny"):
                cur.execute("""UPDATE furry_posts SET post_url=%s, source=%s
                               WHERE file_id=%s AND (post_url IS NULL OR post_url='')""",
                            (cand["url"], cand["site"], file_id))
            else:
                cur.execute("""UPDATE furry_posts SET post_url=%s
                               WHERE file_id=%s AND (post_url IS NULL OR post_url='')""",
                            (cand["url"], file_id))
        except Exception:
            pass                       # застосунку furry може не бути — посилання й так у нас
        cur.execute("""REPLACE INTO srcfind_results
                         (file_id, status, site, site_id, url, artists, method, score, candidates, tags_added, checked_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
                    (file_id, method_status, cand["site"], cand.get("site_id"), cand.get("url"),
                     ", ".join(cand.get("artists") or [])[:500], cand.get("method"), cand.get("score"),
                     json.dumps({"all": json.loads(F.clean_cands(cands)),
                                 "also": F.extra_links(cand, cands)}, ensure_ascii=False),
                     added))
    db.commit()
    return added


def process_file(db, fnd, row) -> str:
    """Один файл: шукаємо, застосовуємо або відкладаємо. → статус."""
    from core.files_api import local_path
    path = local_path(row["rel_path"])
    try:
        with open(path, "rb") as fh:
            content = fh.read()
    except OSError as e:
        _mark(db, row["id"], "error", error=f"не прочитано: {e}")
        return "error"
    try:
        cands = fnd.find(content, row["ext"], row.get("md5_hash"))
    except Exception as e:
        _mark(db, row["id"], "error", error=str(e)[:500])
        return "error"
    best = F.pick(cands)
    if best:
        apply_candidate(db, fnd, row["id"], best, cands)
        return "applied"
    if cands:
        _mark(db, row["id"], "review", candidates=F.clean_cands(cands))
        return "review"
    if getattr(fnd, "errors", None):
        # сайт не відповів — це ще не «не знайдено», наступного разу спробуємо знову
        _mark(db, row["id"], "error", error="; ".join(fnd.errors)[:500])
        return "error"
    _mark(db, row["id"], "none")
    return "none"


def _mark(db, file_id, status, candidates=None, error=None):
    with db.cursor() as cur:
        cur.execute("""REPLACE INTO srcfind_results (file_id, status, candidates, error, checked_at)
                       VALUES (%s,%s,%s,%s,NOW())""", (file_id, status, candidates, error))
    db.commit()


def _todo(db, folder: str, recheck: bool, limit=None):
    from core.db import qq
    from core.storage_api import is_service_path
    ph = ",".join(["%s"] * len(F.IMAGE_EXT))
    where = [f"LOWER(f.ext) IN ({ph})", "(fp.post_url IS NULL OR fp.post_url='')"]
    args = list(F.IMAGE_EXT)
    if not recheck:
        where.append("(r.file_id IS NULL OR r.status='error')")
    else:
        where.append("(r.status IS NULL OR r.status <> 'applied')")
    if folder:
        where.append("f.rel_path LIKE %s")
        args.append(folder.strip("/").replace("%", r"\%").replace("_", r"\_") + "/%")
    try:
        rows = qq(db, f"""SELECT f.id, f.rel_path, f.ext, f.md5_hash FROM files f
                          LEFT JOIN furry_posts fp ON fp.file_id = f.id
                          LEFT JOIN srcfind_results r ON r.file_id = f.id
                          WHERE {' AND '.join(where)} ORDER BY f.id
                          {f'LIMIT {int(limit)}' if limit else ''}""", args)
    except Exception:
        # без застосунку furry немає furry_posts — беремо всі файли
        where = [w for w in where if "fp." not in w]
        rows = qq(db, f"""SELECT f.id, f.rel_path, f.ext, f.md5_hash FROM files f
                          LEFT JOIN srcfind_results r ON r.file_id = f.id
                          WHERE {' AND '.join(where)} ORDER BY f.id""", args)
    return [r for r in rows if not is_service_path(r["rel_path"])]


# ── фонова задача ────────────────────────────────────────────────────

def run_scan(job, params):
    from core.db import get_db
    db = get_db()
    try:
        rows = _todo(db, params.get("folder") or "", bool(params.get("recheck")))
        total = len(rows)
        job.progress(0, f"Файлів без джерела: {total}")
        print(f"🔎 Пошук джерел: {total} файл(ів)")
        fnd = _make_finder()
        stats = {"applied": 0, "review": 0, "none": 0, "error": 0}
        for i, row in enumerate(rows, 1):
            if job.cancelled():
                break
            st = process_file(db, fnd, row)
            stats[st] = stats.get(st, 0) + 1
            if i % 5 == 0 or i == total:
                job.progress(int(i * 100 / max(total, 1)),
                             f"{i}/{total} · знайдено {stats['applied']} · на перевірку {stats['review']}"
                             f" · не знайдено {stats['none']}")
        print(f"🔎 Пошук джерел завершено: {stats}")
        return {"total": total, **stats}
    finally:
        db.close()


# ── адреси ───────────────────────────────────────────────────────────

@bp.route("/status")
def api_status():
    deny = _require_mod()
    if deny:
        return deny
    from core.db import get_db, qq
    db = get_db()
    try:
        counts = {r["status"]: r["n"] for r in qq(db, "SELECT status, COUNT(*) AS n FROM srcfind_results GROUP BY status")}
        todo = len(_todo(db, "", False))
    finally:
        db.close()
    s = _settings()
    jobs = core.jobs.list(JOB, limit=1)
    job = None
    if jobs:
        j = jobs[0]
        job = {k: (j[k].isoformat() if hasattr(j.get(k), "isoformat") else j.get(k))
               for k in ("id", "status", "progress", "message", "result", "error", "created_at", "finished_at") if k in j}
    return jsonify({"counts": counts, "todo": todo, "has_fuzzy_key": bool(s["fuzzy_key"]),
                    "use_iqdb": bool(s["use_iqdb"]), "use_fluffle": bool(s["use_fluffle"]), "job": job})


@bp.route("/settings", methods=["POST"])
def api_settings():
    deny = _require_mod()
    if deny:
        return deny
    d = request.get_json(silent=True) or {}
    if "fuzzy_key" in d:
        core.settings.set("fuzzy_key", (d.get("fuzzy_key") or "").strip())
    if "use_iqdb" in d:
        core.settings.set("use_iqdb", bool(d.get("use_iqdb")))
    if "use_fluffle" in d:
        core.settings.set("use_fluffle", bool(d.get("use_fluffle")))
    return jsonify({"ok": True})


@bp.route("/run", methods=["POST"])
def api_run():
    deny = _require_mod()
    if deny:
        return deny
    from core.auth import current_user_id
    d = request.get_json(silent=True) or {}
    for j in core.jobs.list(JOB, limit=5):
        if j.get("status") in ("pending", "running"):
            return jsonify({"error": "Пошук уже йде"}), 409
    job_id = core.jobs.create(JOB, {"folder": (d.get("folder") or "").strip(), "recheck": bool(d.get("recheck"))},
                              user_id=current_user_id())
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/review")
def api_review():
    deny = _require_mod()
    if deny:
        return deny
    from core.db import get_db, qq
    db = get_db()
    try:
        rows = qq(db, """SELECT r.file_id, r.candidates, f.name, f.rel_path FROM srcfind_results r
                         JOIN files f ON f.id = r.file_id
                         WHERE r.status='review' ORDER BY r.checked_at DESC LIMIT 200""")
    finally:
        db.close()
    for r in rows:
        try:
            r["candidates"] = json.loads(r["candidates"] or "[]")
        except ValueError:
            r["candidates"] = []
    return jsonify(rows)


@bp.route("/review/<int:file_id>/apply", methods=["POST"])
def api_review_apply(file_id):
    deny = _require_mod()
    if deny:
        return deny
    from core.db import get_db, q
    idx = int((request.get_json(silent=True) or {}).get("idx") or 0)
    db = get_db()
    try:
        r = q(db, "SELECT candidates FROM srcfind_results WHERE file_id=%s", (file_id,))
        cands = json.loads((r or {}).get("candidates") or "[]")
        if not (0 <= idx < len(cands)):
            return jsonify({"error": "Варіант не знайдено"}), 404
        added = apply_candidate(db, _make_finder(), file_id, cands[idx], cands)
    finally:
        db.close()
    return jsonify({"ok": True, "tags_added": added})


@bp.route("/review/<int:file_id>/reject", methods=["POST"])
def api_review_reject(file_id):
    deny = _require_mod()
    if deny:
        return deny
    from core.db import get_db
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("UPDATE srcfind_results SET status='rejected' WHERE file_id=%s", (file_id,))
        db.commit()
    finally:
        db.close()
    return jsonify({"ok": True})


@bp.route("/file/<int:file_id>/find", methods=["POST"])
def api_find_one(file_id):
    """Кнопка в картці файлу: шукаємо одразу, без черги."""
    deny = _require_mod()
    if deny:
        return deny
    from core.db import get_db, q
    db = get_db()
    try:
        row = q(db, "SELECT id, rel_path, ext, md5_hash FROM files WHERE id=%s", (file_id,))
        if not row:
            return jsonify({"error": "Файл не знайдено"}), 404
        if (row["ext"] or "").lower() not in F.IMAGE_EXT:
            return jsonify({"error": "Шукати можна лише картинки"}), 400
        status = process_file(db, _make_finder(), row)
        res = q(db, "SELECT status, site, url, tags_added, candidates FROM srcfind_results WHERE file_id=%s", (file_id,))
    finally:
        db.close()
    return jsonify({"ok": True, "status": status, "site": res and res["site"], "url": res and res["url"],
                    "tags_added": (res or {}).get("tags_added") or 0})


# ── картка файлу: посилання, якщо furry його не дав ───────────────────

def _file_meta(data, file_id, db, **_):
    if data.get("post_url"):
        return data
    with db.cursor() as cur:
        cur.execute("SELECT site, url, artists, candidates FROM srcfind_results WHERE file_id=%s AND status='applied'",
                    (file_id,))
        r = cur.fetchone()
    if not r or not r["url"]:
        return data
    data["post_url"] = r["url"]
    data.setdefault("source", r["site"])
    if not data.get("artist") and r["artists"]:
        data["artist"] = r["artists"]
    try:
        also = (json.loads(r["candidates"] or "{}") or {}).get("also") or []
        if also:
            data["source_links"] = also
    except (ValueError, AttributeError):
        pass
    return data


def register(c):
    global core
    core = c
    core.api.add_routes(bp)
    core.jobs.register(JOB, run_scan)
    core.events.add_filter("file.meta", _file_meta)
