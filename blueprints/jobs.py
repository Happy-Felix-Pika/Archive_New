"""Фонові задачі ядра: історія й скасування.

Задачі — механізм ЯДРА, тому ці адреси мають працювати навіть тоді,
коли жодного застосунку не встановлено.
"""

from flask import Blueprint, jsonify, request as flask_request

from core.auth import login_required
from core.jobs import cancel_job, list_jobs, registered_types

bp = Blueprint("jobs", __name__)


@bp.route("/api/jobs")
@login_required
def api_jobs():
    job_type = flask_request.args.get("type") or None
    limit    = min(int(flask_request.args.get("limit", 50)), 200)
    rows = list_jobs(job_type, limit)
    for r in rows:
        for k in ("created_at", "started_at", "finished_at"):
            if r.get(k):
                r[k] = r[k].isoformat()
    return jsonify(rows)


@bp.route("/api/jobs/types")
@login_required
def api_job_types():
    """Які типи задач узагалі хтось уміє виконувати — залежить від застосунків."""
    return jsonify(registered_types())


@bp.route("/api/jobs/<int:job_id>/cancel", methods=["POST"])
@login_required
def api_job_cancel(job_id):
    return jsonify({"ok": cancel_job(job_id)})
