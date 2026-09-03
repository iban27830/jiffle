from threading import Thread

from flask import Blueprint, current_app, jsonify, request

from jiffle.configuration.settings import Settings
from jiffle.features.duplicates.resolution import (
    DuplicateFailure, ignore_match, mark_match_as_family, resolve_match,
)
from jiffle.features.duplicates.scan import create_duplicate_scan_job, run_duplicate_scan_job
from jiffle.infrastructure.database.connection import get_database

duplicates_blueprint = Blueprint("duplicates_v1", __name__)


@duplicates_blueprint.post("/api/v1/duplicate-scan-jobs")
def start_duplicate_scan():
    payload = request.get_json(silent=True) or {}
    try:
        threshold = float(payload.get("threshold", 90))
    except (TypeError, ValueError):
        return _error("duplicates.invalid_threshold", "Threshold must be numeric.", 400)
    if not 70 <= threshold <= 100:
        return _error("duplicates.invalid_threshold", "Threshold must be between 70 and 100.", 400)
    job_id = create_duplicate_scan_job(get_database(), threshold)
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    arguments = (settings.database_path, settings, job_id, threshold)
    if settings.run_jobs_inline:
        run_duplicate_scan_job(*arguments)
    else:
        Thread(target=run_duplicate_scan_job, args=arguments, daemon=True).start()
    return jsonify({"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}), 202


@duplicates_blueprint.get("/api/v1/duplicate-matches")
def list_duplicate_matches():
    status = request.args.get("status", "pending")
    if status not in {"pending", "ignored", "resolved"}:
        return _error("duplicates.invalid_status", "Duplicate status is invalid.", 400)
    rows = get_database().execute(
        "SELECT dm.*, left_item.file_path AS left_path, left_item.width AS left_width, "
        "left_item.height AS left_height, left_item.file_size AS left_size, "
        "right_item.file_path AS right_path, right_item.width AS right_width, "
        "right_item.height AS right_height, right_item.file_size AS right_size "
        "FROM duplicate_matches dm "
        "JOIN media_items left_item ON left_item.id=dm.left_media_id "
        "JOIN media_items right_item ON right_item.id=dm.right_media_id "
        "WHERE dm.status=? ORDER BY dm.confidence DESC, dm.id", (status,)
    ).fetchall()
    return jsonify({"items": [_serialize(row) for row in rows]})


@duplicates_blueprint.post("/api/v1/duplicate-matches/<int:match_id>/ignore")
def ignore_duplicate(match_id: int):
    try:
        ignore_match(get_database(), match_id)
    except DuplicateFailure as error:
        return _duplicate_error(error)
    return jsonify({"status": "ignored"})


@duplicates_blueprint.post("/api/v1/duplicate-matches/<int:match_id>/resolve")
def resolve_duplicate(match_id: int):
    payload = request.get_json(silent=True) or {}
    keep = payload.get("keep")
    merge_metadata = payload.get("merge_metadata", False)
    if keep not in {"left", "right"} or not isinstance(merge_metadata, bool):
        return _error("duplicates.invalid_resolution", "Resolution command is invalid.", 400)
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    try:
        media_id = resolve_match(
            get_database(), settings, match_id, keep, merge_metadata
        )
    except DuplicateFailure as error:
        return _duplicate_error(error)
    return jsonify({"status": "resolved", "kept_media_id": media_id})


@duplicates_blueprint.post("/api/v1/duplicate-matches/<int:match_id>/family")
def family_duplicate(match_id: int):
    try:
        family_id = mark_match_as_family(get_database(), match_id)
    except DuplicateFailure as error:
        return _duplicate_error(error)
    return jsonify({
        "status": "resolved",
        "resolution": "family",
        "family_id": family_id,
    })


def _serialize(row):
    return {
        "id": row["id"], "method": row["match_method"],
        "confidence": row["confidence"], "status": row["status"],
        "resolution": row["resolution"],
        "left": _media_summary(row, "left"),
        "right": _media_summary(row, "right"),
    }


def _media_summary(row, side):
    media_id = row[f"{side}_media_id"]
    return {
        "id": media_id, "width": row[f"{side}_width"],
        "height": row[f"{side}_height"], "file_size": row[f"{side}_size"],
        "content_url": f"/api/v1/media/{media_id}/content",
        "thumbnail_url": f"/api/v1/media/{media_id}/thumbnail",
    }


def _duplicate_error(error):
    status = 404 if error.code == "duplicates.not_found" else 409
    return _error(error.code, error.message, status)


def _error(code, message, status):
    return jsonify({"error": {"code": code, "message": message, "details": {}}}), status
