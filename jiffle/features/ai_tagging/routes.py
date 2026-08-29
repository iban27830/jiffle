import json
from threading import Thread

from flask import Blueprint, current_app, jsonify, request

from jiffle.configuration.settings import Settings
from jiffle.features.ai_tagging.workflow import (
    create_tagging_job, resolve_suggestion, run_tagging_job,
)
from jiffle.infrastructure.database.connection import get_database

ai_tagging_blueprint = Blueprint("ai_tagging", __name__)


@ai_tagging_blueprint.post("/api/v1/media/<int:media_id>/tagging-jobs")
def start_tagging(media_id: int):
    provider = current_app.config["JIFFLE_TAGGING_PROVIDER"]
    if provider is None:
        return _error("ai.provider_not_configured", "No tagging provider is configured.", 503)
    try:
        job_id = create_tagging_job(get_database(), media_id)
    except LookupError:
        return _error("library.media_not_found", "Media item was not found.", 404)
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    arguments = (settings.database_path, settings, job_id, media_id, provider)
    if settings.run_jobs_inline:
        run_tagging_job(*arguments)
    else:
        Thread(target=run_tagging_job, args=arguments, daemon=True).start()
    return jsonify({"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}), 202


@ai_tagging_blueprint.get("/api/v1/tag-suggestions")
def list_suggestions():
    status = request.args.get("status", "pending")
    if status not in {"pending", "accepted", "rejected"}:
        return _error("ai.invalid_status", "Suggestion status is invalid.", 400)
    rows = get_database().execute(
        "SELECT * FROM tag_suggestions WHERE status=? ORDER BY id", (status,)
    ).fetchall()
    return jsonify({"items": [_serialize(row) for row in rows]})


@ai_tagging_blueprint.post("/api/v1/tag-suggestions/<int:suggestion_id>/approve")
def approve_suggestion(suggestion_id: int):
    payload = request.get_json(silent=True) or {}
    tags = payload.get("tags")
    if tags is not None and (
        not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags)
    ):
        return _error("ai.invalid_tags", "Tags must be a string array.", 400)
    try:
        resolve_suggestion(get_database(), suggestion_id, True, tags)
    except LookupError:
        return _error("ai.suggestion_not_found", "Tag suggestion was not found.", 404)
    except RuntimeError:
        return _error("ai.suggestion_resolved", "Tag suggestion is already resolved.", 409)
    except ValueError:
        return _error("ai.invalid_tags", "Selected tags were not proposed.", 400)
    return jsonify({"status": "accepted"})


@ai_tagging_blueprint.post("/api/v1/tag-suggestions/<int:suggestion_id>/reject")
def reject_suggestion(suggestion_id: int):
    try:
        resolve_suggestion(get_database(), suggestion_id, False)
    except LookupError:
        return _error("ai.suggestion_not_found", "Tag suggestion was not found.", 404)
    except RuntimeError:
        return _error("ai.suggestion_resolved", "Tag suggestion is already resolved.", 409)
    return jsonify({"status": "rejected"})


def _serialize(row):
    return {
        "id": row["id"], "media_item_id": row["media_item_id"],
        "provider": row["provider"],
        "known_tags": json.loads(row["known_tags_json"]),
        "unknown_tags": json.loads(row["unknown_tags_json"]),
        "status": row["status"], "created_at": row["created_at"],
    }


def _error(code, message, status):
    return jsonify({"error": {"code": code, "message": message, "details": {}}}), status
