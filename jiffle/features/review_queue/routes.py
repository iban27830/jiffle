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
    create_review_reimport_job,
    create_manual_source_job,
    reject_review_item,
    run_manual_source_job,
    run_review_reimport_job,
    create_metadata_refresh_job,
    run_metadata_refresh_job,
    accept_metadata_suggestion,
    reject_metadata_suggestion,
)
from jiffle.infrastructure.database.connection import get_database

review_blueprint = Blueprint("review_queue", __name__)

_REVIEW_FILTERS = ("all", "source_found", "needs_source")


@review_blueprint.get("/api/v1/review-items")
def list_review_items():
    try:
        limit = int(request.args.get("limit", 60))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return _error("review.invalid_query", "Pagination values must be integers.", 400)
    if not 1 <= limit <= 100 or offset < 0:
        return _error("review.invalid_query", "Pagination is outside its valid range.", 400)
    queue_filter = request.args.get("filter", "all") or "all"
    if queue_filter not in _REVIEW_FILTERS:
        return _error("review.invalid_query", "Unknown review category.", 400)
    connection = get_database()
    # The category narrows the candidate cards without changing what "All" shows,
    # so a user can jump straight to the cards where a source was already found.
    candidate_filter = ""
    if queue_filter == "source_found":
        candidate_filter = (
            " AND EXISTS (SELECT 1 FROM import_source_candidates candidate "
            "WHERE candidate.review_item_id=review_items.id AND candidate.status='pending')"
        )
    elif queue_filter == "needs_source":
        candidate_filter = (
            " AND NOT EXISTS (SELECT 1 FROM import_source_candidates candidate "
            "WHERE candidate.review_item_id=review_items.id AND candidate.status='pending')"
        )
    candidates_sql = (
        "SELECT 'candidate' AS kind, id AS item_id, created_at FROM review_items "
        "WHERE status='pending'" + candidate_filter
    )
    if queue_filter == "all":
        queue_sql = (
            candidates_sql
            + " UNION ALL SELECT 'metadata' AS kind, id AS item_id, created_at "
            "FROM metadata_suggestions WHERE status='pending'"
        )
    else:
        queue_sql = candidates_sql
    total = connection.execute(f"SELECT COUNT(*) FROM ({queue_sql})").fetchone()[0]
    counts = {row["reason"]: row["count"] for row in connection.execute(
        "SELECT reason, COUNT(*) AS count FROM review_items WHERE status='pending' GROUP BY reason"
    ).fetchall()}
    # Paginate the combined queue so Review items and metadata suggestions share
    # one scrollable list instead of paging each table independently.
    page = connection.execute(
        f"SELECT kind, item_id FROM ({queue_sql}) "
        "ORDER BY created_at ASC, kind ASC, item_id ASC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    items = []
    candidate_ids = [
        int(entry["item_id"]) for entry in page if entry["kind"] == "candidate"
    ]
    search_summaries = _review_search_summaries(candidate_ids)
    for entry in page:
        if entry["kind"] == "candidate":
            review_id = int(entry["item_id"])
            row = _review_row(review_id)
            if row is not None:
                items.append(_serialize(row, search_summaries.get(review_id)))
        else:
            row = _metadata_row(entry["item_id"])
            if row is not None:
                items.append(_serialize_metadata(row))
    metadata_count = connection.execute(
        "SELECT COUNT(*) FROM metadata_suggestions WHERE status='pending'"
    ).fetchone()[0]
    if metadata_count:
        counts["metadata_update"] = int(metadata_count)
    pending_reviews = connection.execute(
        "SELECT COUNT(*) FROM review_items WHERE status='pending'"
    ).fetchone()[0]
    with_candidates = connection.execute(
        "SELECT COUNT(*) FROM review_items review WHERE review.status='pending' AND EXISTS ("
        "SELECT 1 FROM import_source_candidates candidate "
        "WHERE candidate.review_item_id=review.id AND candidate.status='pending')"
    ).fetchone()[0]
    return jsonify({
        "items": items,
        "page": {
            "total": int(total), "limit": limit, "offset": offset, "by_reason": counts,
            "filters": {
                "all": int(pending_reviews) + int(metadata_count),
                "source_found": int(with_candidates),
                "needs_source": int(pending_reviews) - int(with_candidates),
            },
        },
    })


@review_blueprint.get("/api/v1/review-items/<int:review_id>")
def get_review_item(review_id: int):
    row = _review_row(review_id)
    if row is None:
        return _error("review.not_found", "Review item was not found.", 404)
    payload = _serialize(row, _review_search_summaries([review_id]).get(review_id))
    payload["source_candidates"] = _source_candidates(review_id)
    return jsonify(payload)


@review_blueprint.get("/api/v1/review-items/<int:review_id>/search-log")
def get_review_search_log(review_id: int):
    """Return every source search recorded for one review card."""
    if _review_row(review_id) is None:
        return _error("review.not_found", "Review item was not found.", 404)
    return jsonify({"items": _review_search_attempts(review_id)})


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
        media_type=MediaType(row["media_type"]), source_url=None, file_source_url=None, author=None,
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
    row, path = _source_candidate_file(review_id, candidate_id)
    if row is None:
        return _error("review.candidate_not_found", "The source candidate was not found.", 404)
    if path is None:
        return _error("review.file_missing", "The source candidate is unavailable.", 404)
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    item = MediaItem(
        id=-candidate_id, file_path=row["stored_path"], media_type=MediaType(row["media_type"]),
        source_url=None, file_source_url=None, author=None, domain=None, width=row["width"], height=row["height"],
        file_size=row["file_size"], content_hash=row["content_hash"], active_revision_id=None,
        edit_operations=(), created_at="", tags=(),
    )
    try:
        thumbnail = ensure_thumbnail(item, path, settings.thumbnail_path)
    except (OSError, ValueError, RuntimeError):
        return _error("review.thumbnail_unavailable", "Thumbnail is unavailable.", 422)
    return send_file(thumbnail, mimetype="image/jpeg", conditional=True)


@review_blueprint.get("/api/v1/review-items/<int:review_id>/source-candidates/<int:candidate_id>/content")
def get_source_candidate_content(review_id: int, candidate_id: int):
    # The comparison view shows the candidate at full size, next to the staged file.
    row, path = _source_candidate_file(review_id, candidate_id)
    if row is None:
        return _error("review.candidate_not_found", "The source candidate was not found.", 404)
    if path is None:
        return _error("review.file_missing", "The source candidate is unavailable.", 404)
    return send_file(path, conditional=True)


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


@review_blueprint.post("/api/v1/review-items/reimport")
def reimport_review_items():
    """Re-run source resolution for selected pending review items."""
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("review_item_ids", payload.get("ids"))
    if not isinstance(raw_ids, list) or not raw_ids:
        return _error("review.invalid_request", "A list of review item IDs is required.", 400)
    try:
        review_ids = [int(value) for value in raw_ids]
    except (TypeError, ValueError):
        return _error("review.invalid_request", "Review item IDs must be integers.", 400)
    connection = get_database()
    try:
        job_id = create_review_reimport_job(connection, review_ids)
    except ReviewFailure as error:
        return _review_error(error)
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    arguments = (
        settings.database_path, settings, job_id, review_ids,
        current_app.config["JIFFLE_SOURCE_PROVIDERS"],
        current_app.config["JIFFLE_MEDIA_DOWNLOADER"],
    )
    if settings.run_jobs_inline:
        run_review_reimport_job(*arguments)
    else:
        Thread(target=run_review_reimport_job, args=arguments, daemon=True).start()
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
        "SELECT item.source_url, source.provider FROM media_items item "
        "LEFT JOIN media_sources source ON source.media_item_id=item.id "
        "WHERE item.id=? AND item.deleted_at IS NULL", (media_id,)
    ).fetchone()
    source_url = source["source_url"] if source else None
    provider = next(
         (item for item in current_app.config["JIFFLE_SOURCE_PROVIDERS"]
          if source_url and item.can_handle(source_url)),
        None,
    )
    if provider is None:
        return _error("metadata.unsupported_source", "No provider supports this source.", 400)
    try:
        job_id = create_metadata_refresh_job(connection, media_id)
    except ReviewFailure as error:
        return _review_error(error)
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    connection.execute(
        "UPDATE metadata_suggestions SET provider=? WHERE job_id=?",
        (getattr(provider, "provider_name", source["provider"]), job_id),
    )
    connection.commit()
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


def _metadata_row(suggestion_id):
    return get_database().execute(
        "SELECT suggestion.id, suggestion.media_item_id, suggestion.provider, suggestion.created_at, "
        "suggestion.source_metadata_json, media.file_path, media.media_type, media.width, media.height, media.file_size "
        "FROM metadata_suggestions suggestion JOIN media_items media ON media.id=suggestion.media_item_id "
        "WHERE suggestion.status='pending' AND suggestion.id=?", (suggestion_id,)
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
            "media_type": row["media_type"],
            "width": row["width"], "height": row["height"], "file_size": row["file_size"],
            "thumbnail_url": f"/api/v1/review-items/{review_id}/source-candidates/{row['id']}/thumbnail",
            "content_url": f"/api/v1/review-items/{review_id}/source-candidates/{row['id']}/content",
        })
    return items


def _review_search_summaries(review_ids):
    """Return ``{review_id: summary}`` for the cards that were already searched."""
    unique_ids = [int(value) for value in dict.fromkeys(review_ids)]
    if not unique_ids:
        return {}
    placeholders = ",".join("?" for _ in unique_ids)
    rows = get_database().execute(
        "SELECT review_item_id, outcome, code, message, created_at "
        f"FROM review_search_attempts WHERE review_item_id IN ({placeholders}) "
        "ORDER BY id",
        unique_ids,
    ).fetchall()
    summaries = {}
    for row in rows:
        review_id = int(row["review_item_id"])
        summary = summaries.setdefault(review_id, {"count": 0})
        summary["count"] += 1
        # Rows are ordered by id, so the last write is the latest attempt.
        summary.update({
            "last_outcome": row["outcome"],
            "last_code": row["code"],
            "last_message": row["message"],
            "last_at": row["created_at"],
        })
    return summaries


def _review_search_attempts(review_id):
    rows = get_database().execute(
        "SELECT id, outcome, code, message, details_json, created_at "
        "FROM review_search_attempts WHERE review_item_id=? ORDER BY id",
        (review_id,),
    ).fetchall()
    import json
    items = []
    for row in rows:
        try:
            details = json.loads(row["details_json"] or "{}")
        except (TypeError, ValueError):
            details = {}
        items.append({
            "id": row["id"], "outcome": row["outcome"], "code": row["code"],
            "message": row["message"], "details": details,
            "created_at": row["created_at"],
        })
    return items


def _source_candidate_file(review_id, candidate_id):
    """Resolve a stored candidate row and its staged file, if it is safe to serve."""
    row = get_database().execute(
        "SELECT stored_path, media_type, content_hash, width, height, file_size FROM import_source_candidates "
        "WHERE id=? AND review_item_id=?", (candidate_id, review_id)
    ).fetchone()
    if row is None:
        return None, None
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    root = settings.resolved_import_staging_path.resolve()
    path = (root / (row["stored_path"] or "")).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return row, None
    return row, path


def _review_path(row) -> Path | None:
    if row is None or not row["stored_path"]:
        return None
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    root = settings.resolved_import_staging_path.resolve()
    candidate = (root / row["stored_path"]).resolve()
    return candidate if candidate.is_relative_to(root) else None


def _serialize(row, search=None):
    review_id = row["id"]
    return {
        "id": review_id, "kind": "candidate",
        "reason": row["reason"], "status": row["status"],
        "original_name": row["original_name"], "type": row["media_type"],
        "width": row["width"], "height": row["height"],
        "file_size": row["file_size"], "created_at": row["created_at"],
        "content_url": f"/api/v1/review-items/{review_id}/content",
        "thumbnail_url": f"/api/v1/review-items/{review_id}/thumbnail",
        "source_candidates": _source_candidates(review_id),
        "search": search or {"count": 0},
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
