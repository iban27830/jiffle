from threading import Thread

from flask import Blueprint, current_app, jsonify, request

from jiffle.infrastructure.database.connection import get_database
from .workflow import (
    TrimFailure,
    approve_review_item,
    create_review_scan_job,
    list_review_items,
    normalize_segments,
    run_review_scan,
    run_trim_job,
    start_review_scan,
    trim_state,
)


trim_blueprint = Blueprint("trim_editor", __name__)


@trim_blueprint.get("/api/v1/trim-reviews")
def list_trim_reviews():
    status = (request.args.get("status") or "pending").strip().lower()
    if status not in {"pending", "approved", "all"}:
        return _error("trim.invalid_status", "Unknown review status.", 400)
    connection = get_database()
    settings = current_app.config["JIFFLE_SETTINGS"]
    items = list_review_items(connection, settings, status)
    pending = connection.execute(
        "SELECT COUNT(*) FROM media_items WHERE deleted_at IS NULL "
        "AND derived_from_media_id IS NULL AND trim_review_status='pending'"
    ).fetchone()[0]
    return jsonify({"items": items, "total": len(items), "pending": int(pending)})


@trim_blueprint.post("/api/v1/trim-reviews/<int:media_id>/approve")
def approve_trim_review(media_id: int):
    try:
        approve_review_item(get_database(), media_id)
    except TrimFailure as error:
        return _trim_error(error)
    return jsonify({"status": "approved", "media_id": media_id})


@trim_blueprint.post("/api/v1/trim-scan-jobs")
def create_trim_scan_job():
    settings = current_app.config["JIFFLE_SETTINGS"]
    connection = get_database()
    job_id = create_review_scan_job(connection, settings)
    start_review_scan(settings, job_id)
    return jsonify({"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}), 202


@trim_blueprint.get("/api/v1/trim-scan-jobs/active")
def active_trim_scan():
    row = get_database().execute(
        "SELECT b.id, b.status, b.progress, t.cancel_requested, t.scanned_count, "
        "t.candidate_count FROM background_jobs b JOIN trim_scan_jobs t ON t.job_id=b.id "
        "WHERE b.status IN ('pending', 'running') ORDER BY b.id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return jsonify({"job": None})
    return jsonify({"job": {
        "id": row["id"],
        "status": row["status"],
        "progress": row["progress"],
        "cancel_requested": bool(row["cancel_requested"]),
        "scanned": row["scanned_count"],
        "candidates": row["candidate_count"],
        "status_url": f"/api/v1/jobs/{row['id']}",
    }})


@trim_blueprint.post("/api/v1/trim-scan-jobs/<int:job_id>/cancel")
def cancel_trim_scan(job_id: int):
    cursor = get_database().execute(
        "UPDATE trim_scan_jobs SET cancel_requested=1 WHERE job_id=?", (job_id,)
    )
    get_database().commit()
    if cursor.rowcount != 1:
        return _error("trim.scan_not_found", "Review scan was not found.", 404)
    return jsonify({"status": "cancelling"})


@trim_blueprint.get("/api/v1/media/<int:media_id>/trim-state")
def get_trim_state(media_id: int):
    try:
        state = trim_state(get_database(), current_app.config["JIFFLE_SETTINGS"], media_id)
    except TrimFailure as error:
        return _trim_error(error)
    return jsonify(state)


@trim_blueprint.post("/api/v1/media/<int:media_id>/trim-jobs")
def create_trim_job(media_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        segments = normalize_segments(payload.get("segments"))
    except TrimFailure as error:
        return _trim_error(error)
    settings = current_app.config["JIFFLE_SETTINGS"]
    connection = get_database()
    connection.execute(
        "INSERT INTO background_jobs (job_type, status) VALUES ('media_trim', 'pending')"
    )
    job_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
    connection.commit()
    args = (settings.database_path, settings, int(job_id), media_id, segments)
    if settings.run_jobs_inline:
        run_trim_job(*args)
    else:
        Thread(target=run_trim_job, args=args, daemon=True).start()
    return jsonify({"job_id": int(job_id), "status_url": f"/api/v1/jobs/{job_id}"}), 202


def _trim_error(error: TrimFailure):
    status = 404 if error.code.endswith(("not_found", "file_missing")) else 400
    return jsonify({
        "error": {"code": error.code, "message": error.message, "details": error.details}
    }), status


def _error(code, message, status):
    return jsonify({"error": {"code": code, "message": message, "details": {}}}), status
