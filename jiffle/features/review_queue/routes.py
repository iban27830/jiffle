from pathlib import Path
from threading import Thread

from flask import Blueprint, current_app, jsonify, request, send_file

from jiffle.configuration.settings import Settings
from jiffle.features.imports.url_normalization import normalize_source_url
from jiffle.features.library.domain import MediaItem, MediaType
from jiffle.features.library.thumbnail_cache import ensure_thumbnail
from jiffle.features.review_queue.workflow import (
    ReviewFailure,
    accept_review_item,
    create_manual_source_job,
    reject_review_item,
    run_manual_source_job,
)
from jiffle.infrastructure.database.connection import get_database

review_blueprint = Blueprint("review_queue", __name__)


@review_blueprint.get("/api/v1/review-items")
def list_review_items():
    try:
        limit = int(request.args.get("limit", 20))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return _error("review.invalid_query", "Pagination values must be integers.", 400)
    if not 1 <= limit <= 100 or offset < 0:
        return _error("review.invalid_query", "Pagination is outside its valid range.", 400)
    connection = get_database()
    total = connection.execute(
        "SELECT COUNT(*) FROM review_items WHERE status='pending'"
    ).fetchone()[0]
    counts = {row["reason"]: row["count"] for row in connection.execute(
        "SELECT reason, COUNT(*) AS count FROM review_items WHERE status='pending' GROUP BY reason"
    ).fetchall()}
    rows = connection.execute(
        "SELECT review.id, review.reason, review.status, review.created_at, candidate.original_name, "
        "candidate.media_type, candidate.width, candidate.height, candidate.file_size "
        "FROM review_items review JOIN import_candidates candidate "
        "ON candidate.id=review.import_candidate_id WHERE review.status='pending' "
        "ORDER BY review.id LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    return jsonify({
        "items": [_serialize(row) for row in rows],
        "page": {"total": total, "limit": limit, "offset": offset, "by_reason": counts},
    })


@review_blueprint.get("/api/v1/review-items/<int:review_id>")
def get_review_item(review_id: int):
    row = _review_row(review_id)
    if row is None:
        return _error("review.not_found", "Review item was not found.", 404)
    return jsonify(_serialize(row))


@review_blueprint.get("/api/v1/review-items/<int:review_id>/content")
def get_review_content(review_id: int):
    row = _review_row(review_id)
    path = _review_path(row) if row else None
    if path is None or not path.is_file():
        return _error("review.file_missing", "The staged file is unavailable.", 404)
    return send_file(path, conditional=True)


@review_blueprint.get("/api/v1/review-items/<int:review_id>/thumbnail")
def get_review_thumbnail(review_id: int):
    row = _review_row(review_id)
    path = _review_path(row) if row else None
    if path is None or not path.is_file():
        return _error("review.file_missing", "The staged file is unavailable.", 404)
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    item = MediaItem(
        id=-review_id, file_path=row["stored_path"],
        media_type=MediaType(row["media_type"]), source_url=None, author=None,
        domain=None, width=row["width"], height=row["height"],
        file_size=row["file_size"], content_hash=row["content_hash"],
        created_at=row["created_at"], tags=(),
    )
    try:
        thumbnail = ensure_thumbnail(item, path, settings.thumbnail_path)
    except (OSError, ValueError, RuntimeError):
        return _error("review.thumbnail_unavailable", "Thumbnail is unavailable.", 422)
    return send_file(thumbnail, mimetype="image/jpeg", conditional=True)


@review_blueprint.post("/api/v1/review-items/<int:review_id>/accept")
def accept_review(review_id: int):
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    try:
        media_item_id = accept_review_item(get_database(), settings, review_id)
    except ReviewFailure as error:
        return _review_error(error)
    return jsonify({"status": "accepted", "media_item_id": media_item_id})


@review_blueprint.post("/api/v1/review-items/<int:review_id>/reject")
def reject_review(review_id: int):
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    try:
        reject_review_item(get_database(), settings, review_id)
    except ReviewFailure as error:
        return _review_error(error)
    return jsonify({"status": "rejected"})


@review_blueprint.post("/api/v1/review-items/<int:review_id>/source")
def apply_manual_source(review_id: int):
    payload = request.get_json(silent=True)
    raw_url = payload.get("url") if isinstance(payload, dict) else None
    if not isinstance(raw_url, str):
        return _error("review.invalid_source_url", "URL is required.", 400)
    try:
        source_url = normalize_source_url(raw_url)
    except ValueError as error:
        return _error("review.invalid_source_url", str(error), 400)
    provider = next((item for item in current_app.config["JIFFLE_SOURCE_PROVIDERS"] if item.can_handle(source_url)), None)
    if provider is None:
        return _error("review.unsupported_source", "No provider supports this URL.", 400)
    connection = get_database()
    try:
        job_id = create_manual_source_job(connection, review_id, source_url)
    except ReviewFailure as error:
        return _review_error(error)
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    arguments = (settings.database_path, settings, job_id, review_id, source_url, provider)
    if settings.run_jobs_inline:
        run_manual_source_job(*arguments)
    else:
        Thread(target=run_manual_source_job, args=arguments, daemon=True).start()
    return jsonify({"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}), 202


def _review_row(review_id):
    return get_database().execute(
        "SELECT review.id, review.reason, review.status, review.created_at, "
        "candidate.original_name, candidate.stored_path, candidate.media_type, "
        "candidate.content_hash, candidate.width, candidate.height, candidate.file_size "
        "FROM review_items review JOIN import_candidates candidate "
        "ON candidate.id=review.import_candidate_id WHERE review.id=?", (review_id,)
    ).fetchone()


def _review_path(row) -> Path | None:
    if row is None or not row["stored_path"]:
        return None
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    root = settings.resolved_import_staging_path.resolve()
    candidate = (root / row["stored_path"]).resolve()
    return candidate if candidate.is_relative_to(root) else None


def _serialize(row):
    review_id = row["id"]
    return {
        "id": review_id, "reason": row["reason"], "status": row["status"],
        "original_name": row["original_name"], "type": row["media_type"],
        "width": row["width"], "height": row["height"],
        "file_size": row["file_size"], "created_at": row["created_at"],
        "content_url": f"/api/v1/review-items/{review_id}/content",
        "thumbnail_url": f"/api/v1/review-items/{review_id}/thumbnail",
    }


def _review_error(error: ReviewFailure):
    status = 404 if error.code == "review.not_found" else 409
    return _error(error.code, error.message, status)


def _error(code, message, status):
    return jsonify({"error": {"code": code, "message": message, "details": {}}}), status
