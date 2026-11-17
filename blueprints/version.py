"""Версія архіву: номер збірки, коміт, дата — і чи є новіша збірка."""

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request as flask_request

from core.auth import admin_required
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

bp = Blueprint("version", __name__)

_info = None
_latest = {"t": 0, "data": None}
_latest_lock = threading.Lock()
_CHECK_EVERY = 15 * 60


def version_info() -> dict:
    """Що за збірка зараз запущена (з VERSION.json, який пише Docker-збірка)."""
    global _info
    if _info is not None:
        return _info
    info = {"build": "dev", "sha": "", "image": "", "date": None}
    try:
        with open(os.path.join(BASE_DIR, "VERSION.json"), encoding="utf-8") as f:
            info.update(json.load(f))
    except (OSError, ValueError):
        # запуск не з образу (розробка) — беремо коміт з git, якщо є
        try:
            out = subprocess.run(["git", "log", "-1", "--format=%H %cI"], cwd=BASE_DIR,
                                 capture_output=True, text=True, timeout=3).stdout.split()
            if len(out) == 2:
                info["sha"], info["date"] = out
        except Exception:
            pass
    info["short"] = (info.get("sha") or "")[:7]
    _info = info
    return info


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _check_latest(force=False) -> dict:
    """Дата останньої збірки на Docker Hub (тег latest). Кешується на 15 хв."""
    import requests
    info = version_info()
    image = info.get("image") or ""
    if not image:
        return {"status": "unknown", "reason": "Збірка не з Docker-образу — перевірка недоступна"}
    with _latest_lock:
        if not force and _latest["data"] and time.time() - _latest["t"] < _CHECK_EVERY:
            return _latest["data"]
        try:
            r = requests.get(f"https://hub.docker.com/v2/repositories/{image}/tags/latest", timeout=10)
            r.raise_for_status()
            hub = _parse(r.json().get("last_updated"))
        except Exception as e:
            print(f"  ⚠ Перевірка оновлень (Docker Hub): {e}")
            data = {"status": "unknown",
                    "reason": "Не вдалося перевірити: Docker Hub недоступний або образ приватний"}
        else:
            mine = _parse(info.get("date"))
            # образ публікується через хвилину-дві після запису VERSION.json
            if hub and mine and (hub - mine).total_seconds() > 20 * 60:
                data = {"status": "outdated", "latest_date": hub.isoformat()}
            else:
                data = {"status": "current", "latest_date": hub.isoformat() if hub else None}
        _latest.update(t=time.time(), data=data)
        return data


@bp.route("/api/version")
def api_version():
    data = dict(version_info())
    data.pop("image", None)
    return jsonify(data)


@bp.route("/api/version/check")
@admin_required
def api_version_check():
    data = dict(version_info())
    data.pop("image", None)
    data["latest"] = _check_latest(force=flask_request.args.get("force") == "1")
    data["checked_at"] = datetime.now(timezone.utc).isoformat()
    return jsonify(data)
