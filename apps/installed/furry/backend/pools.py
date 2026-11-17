"""Пули: списки для пікера в «Артах» і для зовнішніх архівів.

Дані — у furry_pools / furry_pool_names / furry_pool_items (poolstore.py).
Кількість сторінок зберігається в самому пулі (cnt), тож список пулів —
це просте читання однієї таблиці, без підрахунків по всьому архіву.
"""

import threading
import time

from flask import Blueprint, jsonify, request as flask_request

from core.db import get_db, qq
from core.warmcache import WarmCache
from . import poolstore

bp = Blueprint("pools", __name__)


def _invalidate_pools_cache():
    """Сумісність зі старими викликами: кешу більше немає — список читається
    з furry_pools напряму."""


def all_pools(db, hidden_folders=None, artist_id=None) -> list:
    args, where = [], ["p.cnt > 0"]
    if artist_id:
        where.append("p.artist_id = %s"); args.append(int(artist_id))
    if hidden_folders:
        where.append(f"a.folder_name NOT IN ({','.join(['%s'] * len(hidden_folders))})")
        args.extend(hidden_folders)
    return qq(db, f"""SELECT p.name, p.artist_id, a.artist_name, p.source, p.cnt
                      FROM furry_pools p JOIN artists a ON a.id = p.artist_id
                      WHERE {' AND '.join(where)}
                      ORDER BY p.cnt DESC, p.name""", args)


def _compute_pool_items(db):
    """Хто в якому пулі: {file_id: (pool_id, ...)} — кілька десятків тисяч
    записів, у пам'яті це дрібниця. Для підрахунку пулів з тегами."""
    import pymysql
    out = {}
    with db.cursor(pymysql.cursors.Cursor) as cur:
        cur.execute("SELECT file_id, pool_id FROM furry_pool_items")
        for fid, pid in cur.fetchall():
            out[fid] = out.get(fid, ()) + (pid,)
    return out


pool_items_cache = WarmCache("pool-items", _compute_pool_items)


def reconcile_loop():
    """Страховка: раз на годину звіряємо «рідні» пули з теками файлів."""
    from core.db import _connect
    while True:
        time.sleep(3600)
        try:
            db = _connect()
            try:
                poolstore.reconcile_all(db)
            finally:
                db.close()
        except Exception as e:
            print(f"  ⚠ Звірка пулів: {e}")


@bp.route("/api/pools")
def api_pools():
    """Список пулів з кількістю сторінок — для пікера в галереї."""
    artist_id = flask_request.args.get("artist_id", None)
    tag_ids   = flask_request.args.getlist("tag_id")
    try:
        db = get_db()
        if not tag_ids:
            rows = all_pools(db, artist_id=artist_id)
        else:
            # з тегами — рахуємо лише сторінки, що мають усі обрані теги:
            # файли з кожним тегом (короткий запит по індексу тегу),
            # перетин — у пам'яті, а хто в якому пулі — з готової карти.
            # Жодного проходу по всьому архіву.
            import pymysql
            files = None
            with db.cursor(pymysql.cursors.Cursor) as cur:
                for tid in dict.fromkeys(int(t) for t in tag_ids):
                    cur.execute("SELECT download_id FROM download_tags WHERE tag_id=%s", (tid,))
                    got = [r[0] for r in cur.fetchall()]
                    files = set(got) if files is None else files.intersection(got)
                    if not files:
                        break
            counts = {}
            items = pool_items_cache.get()
            for fid in files or ():
                for pid in items.get(fid, ()):
                    counts[pid] = counts.get(pid, 0) + 1
            rows = []
            if counts:
                ph = ",".join(["%s"] * len(counts))
                args = list(counts)
                extra = ""
                if artist_id:
                    extra = " AND p.artist_id = %s"; args.append(int(artist_id))
                rows = list(qq(db, f"""SELECT p.id, p.name, p.artist_id, a.artist_name, p.source
                                  FROM furry_pools p JOIN artists a ON a.id = p.artist_id
                                  WHERE p.id IN ({ph}){extra}""", args))
                for r in rows:
                    r["cnt"] = counts[r.pop("id")]
                rows.sort(key=lambda r: -r["cnt"])
        db.close()
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@bp.route("/api/external-access/pools")
def api_external_access_pools():
    from blueprints.federation import _validate_external_key, get_hidden_folders_for_key
    key = flask_request.headers.get("X-Access-Key", "")
    valid, key_id = _validate_external_key(key, flask_request.headers.get("X-Identity", ""))
    if not valid:
        return jsonify({"error": "Ключ доступу недійсний або деактивований"}), 403

    hidden = get_hidden_folders_for_key(key_id)
    db = get_db()
    try:
        return jsonify(all_pools(db, hidden))
    finally:
        db.close()


@bp.route("/api/external-archives/<archive_id>/pools")
def api_external_archive_pools(archive_id):
    """Список серій підключеного чужого архіву."""
    import requests as req
    from blueprints.federation import _get_external_cfg_or_404
    cfg = _get_external_cfg_or_404(archive_id)
    if not cfg:
        return jsonify({"error": "Архів не знайдено"}), 404
    base = cfg.get("base_url", "").rstrip("/")
    try:
        r = req.get(f"{base}/api/external-access/pools", headers={
            "X-Access-Key": cfg.get("access_key", ""),
            "X-Identity":   cfg.get("my_identity", ""),
        }, timeout=15)
    except req.exceptions.RequestException as e:
        return jsonify({"error": f"Не вдалося звернутись до архіву: {e}"}), 502
    if r.status_code == 404:
        # у того архіву стара версія — адреси серій там ще немає
        return jsonify({"unsupported": True, "pools": []})
    if r.status_code == 403:
        return jsonify({"error": "Ключ доступу недійсний або деактивований власником архіву"}), 403
    return jsonify({"pools": r.json()})
