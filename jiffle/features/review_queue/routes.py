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
    accept_source_candidate,
    create_manual_source_job,
    reject_review_item,
    run_manual_source_job,
    create_metadata_refresh_job,
    run_metadata_refresh_job,
    accept_metadata_suggestion,
    reject_metadata_suggestion,
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
    items = [_serialize(row) for row in rows]
    metadata_rows = connection.execute(
        "SELECT suggestion.id, suggestion.media_item_id, suggestion.provider, suggestion.created_at, "
        "suggestion.source_metadata_json, media.file_path, media.media_type, media.width, media.height, media.file_size "
        "FROM metadata_suggestions suggestion JOIN media_items media ON media.id=suggestion.media_item_id "
        "WHERE suggestion.status='pending' ORDER BY suggestion.id LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    metadata_items = [_serialize_metadata(row) for row in metadata_rows]
    items.extend(metadata_items)
    metadata_count = connection.execute(
        "SELECT COUNT(*) FROM metadata_suggestions WHERE status='pending'"
    ).fetchone()[0]
    if metadata_count:
        counts["metadata_update"] = int(metadata_count)
    return jsonify({
        "items": items,
        "page": {"total": int(total) + int(metadata_count), "limit": limit, "offset": offset, "by_reason": counts},
    })


@review_blueprint.get("/api/v1/review-items/<int:review_id>")
def get_review_item(review_id: int):
    row = _review_row(review_id)
    if row is None:
        return _error("review.not_found", "Review item was not found.", 404)
    payload = _serialize(row)
    payload["source_candidates"] = _source_candidates(review_id)
    return jsonify(payload)


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
        active_revision_id=None, edit_operations=(),
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
    payload = request.get_json(silent=True) or {}
    try:
        candidate_id = payload.get("source_candidate_id")
        if candidate_id is not None:
            media_item_id = accept_source_candidate(
                get_database(), settings, review_id, int(candidate_id)
            )
        else:
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


@review_blueprint.get("/api/v1/review-items/<int:review_id>/source-candidates/<int:candidate_id>/thumbnail")
def get_source_candidate_thumbnail(review_id: int, candidate_id: int):
    row = get_database().execute(
        "SELECT stored_path, media_type, content_hash, width, height, file_size FROM import_source_candidates "
        "WHERE id=? AND review_item_id=?", (candidate_id, review_id)
    ).fetchone()
    if row is None:
        return _error("review.candidate_not_found", "The source candidate was not found.", 404)
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    root = settings.resolved_import_staging_path.resolve()
    path = (root / (row["stored_path"] or "")).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return _error("review.file_missing", "The source candidate is unavailable.", 404)
    item = MediaItem(
        id=-candidate_id, file_path=row["stored_path"], media_type=MediaType(row["media_type"]),
        source_url=None, author=None, domain=None, width=row["width"], height=row["height"],
        file_size=row["file_size"], content_hash=row["content_hash"], active_revision_id=None,
        edit_operations=(), created_at="", tags=(),
    )
    try:
        thumbnail = ensure_thumbnail(item, path, settings.thumbnail_path)
    except (OSError, ValueError, RuntimeError):
        return _error("review.thumbnail_unavailable", "Thumbnail is unavailable.", 422)
    return send_file(thumbnail, mimetype="image/jpeg", conditional=True)


@review_blueprint.post("/api/v1/review-items/<int:review_id>/source-candidates/<int:candidate_id>/accept")
def accept_source_candidate_route(review_id: int, candidate_id: int):
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    try:
        media_item_id = accept_source_candidate(get_database(), settings, review_id, candidate_id)
    except ReviewFailure as error:
        return _review_error(error)
    return jsonify({"status": "accepted", "media_item_id": media_item_id})


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


@review_blueprint.post("/api/v1/media/<int:media_id>/metadata-refresh")
@review_blueprint.post("/api/v1/metadata-refresh-jobs")
def refresh_metadata(media_id: int | None = None):
    payload = request.get_json(silent=True) or {}
    if media_id is None:
        try:
            media_id = int(payload.get("media_id"))
        except (TypeError, ValueError):
            media_id = 0
    if media_id < 1:
        return _error("metadata.invalid_media", "A valid media ID is required.", 400)
    connection = get_database()
    source = connection.execute(
        "SELECT provider FROM media_sources WHERE media_item_id=?", (media_id,)
    ).fetchone()
    provider = next(
        (item for item in current_app.config["JIFFLE_SOURCE_PROVIDERS"]
         if source is not None and getattr(item, "provider_name", None) == source["provider"]),
        None,
    )
    if provider is None:
        return _error("metadata.unsupported_source", "No provider supports this source.", 400)
    try:
        job_id = create_metadata_refresh_job(connection, media_id)
    except ReviewFailure as error:
        return _review_error(error)
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    args = (settings.database_path, job_id, media_id, provider)
    if settings.run_jobs_inline:
        run_metadata_refresh_job(*args)
    else:
        Thread(target=run_metadata_refresh_job, args=args, daemon=True).start()
    return jsonify({"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}), 202


@review_blueprint.get("/api/v1/metadata-suggestions")
def list_metadata_suggestions():
    rows = get_database().execute(
        "SELECT suggestion.id, suggestion.media_item_id, suggestion.provider, suggestion.created_at, "
        "suggestion.source_metadata_json, media.file_path, media.media_type, media.width, media.height, media.file_size "
        "FROM metadata_suggestions suggestion JOIN media_items media ON media.id=suggestion.media_item_id "
        "WHERE suggestion.status='pending' ORDER BY suggestion.id"
    ).fetchall()
    return jsonify({"items": [_serialize_metadata(row) for row in rows]})


@review_blueprint.post("/api/v1/metadata-suggestions/<int:suggestion_id>/accept")
def accept_metadata(suggestion_id: int):
    try:
        media_item_id = accept_metadata_suggestion(get_database(), suggestion_id)
    except ReviewFailure as error:
        return _review_error(error)
    return jsonify({"status": "accepted", "media_item_id": media_item_id})


@review_blueprint.post("/api/v1/metadata-suggestions/<int:suggestion_id>/reject")
def reject_metadata(suggestion_id: int):
    try:
        reject_metadata_suggestion(get_database(), suggestion_id)
    except ReviewFailure as error:
        return _review_error(error)
    return jsonify({"status": "rejected"})


def _review_row(review_id):
    return get_database().execute(
        "SELECT review.id, review.reason, review.status, review.created_at, "
        "candidate.original_name, candidate.stored_path, candidate.media_type, "
        "candidate.content_hash, candidate.width, candidate.height, candidate.file_size "
        "FROM review_items review JOIN import_candidates candidate "
        "ON candidate.id=review.import_candidate_id WHERE review.id=?", (review_id,)
    ).fetchone()


def _source_candidates(review_id):
    rows = get_database().execute(
        "SELECT id, rank, match_method, confidence, provider, source_metadata_json, stored_path, "
        "media_type, content_hash, width, height, file_size, status FROM import_source_candidates "
        "WHERE review_item_id=? ORDER BY rank, id", (review_id,)
    ).fetchall()
    import json
    items = []
    for row in rows:
        try:
            metadata = json.loads(row["source_metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        items.append({
            "id": row["id"], "rank": row["rank"], "match_method": row["match_method"],
            "confidence": row["confidence"], "provider": row["provider"],
            "status": row["status"], "source_metadata": metadata,
            "width": row["width"], "height": row["height"], "file_size": row["file_size"],
            "thumbnail_url": f"/api/v1/review-items/{review_id}/source-candidates/{row['id']}/thumbnail",
        })
    return items


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
        "source_candidates": _source_candidates(review_id),
    }


def _serialize_metadata(row):
    return {
        "id": row["id"], "kind": "metadata", "suggestion_id": row["id"],
        "media_id": row["media_item_id"], "reason": "metadata_update",
        "status": "pending", "original_name": f"Media #{row['media_item_id']}",
        "type": row["media_type"], "width": row["width"], "height": row["height"],
        "file_size": row["file_size"], "created_at": row["created_at"],
        "thumbnail_url": f"/api/v1/media/{row['media_item_id']}/thumbnail",
        "content_url": f"/api/v1/media/{row['media_item_id']}/content",
        "source_metadata": _metadata_summary(row["source_metadata_json"]),
    }


def _metadata_summary(raw):
    try:
        import json
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return {
        "parent_id": payload.get("parent_id"),
        "character_tags": payload.get("character_tags", []),
        "tag_count": len(payload.get("tags", [])),
    }


def _review_error(error: ReviewFailure):
    status = 404 if error.code == "review.not_found" else 409
    return _error(error.code, error.message, status)


def _error(code, message, status):
    return jsonify({"error": {"code": code, "message": message, "details": {}}}), status
