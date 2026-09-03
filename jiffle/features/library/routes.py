from pathlib import Path
import shutil
from datetime import datetime, timezone
import re

from flask import Blueprint, current_app, jsonify, request, send_file

from jiffle.configuration.settings import Settings
from jiffle.features.library.domain import LibraryQuery, MediaItem, MediaType
from jiffle.features.library.sqlite_repository import SqliteLibraryRepository
from jiffle.features.library.thumbnail_cache import ensure_thumbnail
from jiffle.infrastructure.database.connection import get_database

library_blueprint = Blueprint("library", __name__)


@library_blueprint.get("/api/v1/media")
def list_media():
    query, error = _parse_query()
    if error:
        return _error("library.invalid_query", error, 400)
    page = _repository().list_media(query)
    return jsonify({
        "items": [_serialize(item) for item in page.items],
        "page": {"total": page.total, "limit": page.limit, "offset": page.offset},
    })


@library_blueprint.get("/api/v1/media/<int:media_id>")
def get_media(media_id: int):
    item = _repository().get_media(media_id)
    if item is None:
        return _error("library.media_not_found", "Media item was not found.", 404)
    return jsonify(_serialize(item))

@library_blueprint.delete("/api/v1/media/<int:media_id>")
def delete_media(media_id: int):
    connection = get_database()
    row = connection.execute("SELECT * FROM media_items WHERE id=? AND deleted_at IS NULL", (media_id,)).fetchone()
    if row is None:
        return _error("library.media_not_found", "Media item was not found.", 404)
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    root = settings.media_path.resolve()
    source = (root / row["file_path"]).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        return _error("library.media_file_missing", "Media file is unavailable.", 404)
    quarantine = settings.database_path.parent / "delete-quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    target = quarantine / f"{media_id}-{source.name}"
    try:
        shutil.move(str(source), str(target))
        if row["source_url"]:
            connection.execute(
                "INSERT OR IGNORE INTO blocked_media_signatures "
                "(source_url, reason) VALUES (?, 'deleted')", (row["source_url"],)
            )
        if row["content_hash"]:
            connection.execute(
                "INSERT OR IGNORE INTO blocked_media_signatures "
                "(content_hash, reason) VALUES (?, 'deleted')", (row["content_hash"],)
            )
        connection.execute("UPDATE media_items SET deleted_at=CURRENT_TIMESTAMP, content_hash=NULL WHERE id=?", (media_id,))
        connection.execute("INSERT INTO operation_history (event_type, entity_type, entity_id, details_json) VALUES (?, ?, ?, ?)", ("media.deleted", "media", media_id, "{}"))
        connection.commit()
        target.unlink(missing_ok=True)
    except Exception:
        connection.rollback()
        if target.exists() and not source.exists():
            shutil.move(str(target), str(source))
        current_app.logger.exception("Could not delete media %s", media_id)
        return _error("library.delete_failed", "Media could not be deleted.", 500)
    return jsonify({"id": media_id, "deleted": True})


@library_blueprint.get("/api/v1/media/<int:media_id>/content")
def get_media_content(media_id: int):
    item = _repository().get_media(media_id)
    if item is None:
        return _error("library.media_not_found", "Media item was not found.", 404)
    path = _resolve_media_path(item)
    if path is None or not path.is_file():
        return _error("library.media_file_missing", "Media file is unavailable.", 404)
    return send_file(path, conditional=True)


@library_blueprint.get("/api/v1/media/<int:media_id>/thumbnail")
def get_media_thumbnail(media_id: int):
    item = _repository().get_media(media_id)
    if item is None:
        return _error("library.media_not_found", "Media item was not found.", 404)
    source = _resolve_media_path(item)
    if source is None or not source.is_file():
        return _error("library.media_file_missing", "Media file is unavailable.", 404)
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    try:
        thumbnail = ensure_thumbnail(item, source, settings.thumbnail_path)
    except (OSError, ValueError, RuntimeError):
        current_app.logger.exception("Could not create thumbnail for media %s", media_id)
        return _error(
            "library.thumbnail_unavailable", "Thumbnail could not be created.", 422
        )
    return send_file(thumbnail, mimetype="image/jpeg", conditional=True)


def _parse_query() -> tuple[LibraryQuery | None, str | None]:
    try:
        limit = int(request.args.get("limit", 20))
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        return None, "Pagination values must be integers."
    if not 1 <= limit <= 100:
        return None, "Limit must be between 1 and 100."
    if offset < 0:
        return None, "Offset must not be negative."
    raw_type = _optional_parameter("type")
    try:
        media_type = MediaType(raw_type) if raw_type else None
    except ValueError:
        return None, "Type must be image or video."
    raw_id = _optional_parameter("id")
    try:
        media_id = int(raw_id) if raw_id else None
    except ValueError:
        return None, "Media ID must be an integer."
    if media_id is not None and media_id < 1:
        return None, "Media ID must be a positive integer."
    text = _optional_parameter("q")
    remote_id = _optional_parameter("remote_id")
    if remote_id is None and text:
        parent_match = re.fullmatch(r"parent:(\d+)", text, re.IGNORECASE)
        if parent_match:
            remote_id, text = parent_match.group(1), None
    return LibraryQuery(
        limit=limit,
        offset=offset,
        tag=_optional_parameter("tag"),
        exclude_tag=_optional_parameter("exclude_tag"),
        author=_optional_parameter("author"),
        domain=_optional_parameter("domain"),
        media_type=media_type,
        text=text,
        media_id=media_id,
        tags=tuple(value.strip().lower() for value in request.args.getlist("tag") if value.strip()),
        excluded_tags=tuple(value.strip().lower() for value in request.args.getlist("exclude_tag") if value.strip()),
        parent_id=_optional_parameter("parent_id"),
        remote_id=remote_id,
    ), None


def _optional_parameter(name: str) -> str | None:
    value = request.args.get(name, "").strip()
    return value or None


def _repository() -> SqliteLibraryRepository:
    return SqliteLibraryRepository(get_database())


def _resolve_media_path(item: MediaItem) -> Path | None:
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    root = settings.media_path.resolve()
    candidate = (root / item.file_path).resolve()
    return candidate if candidate.is_relative_to(root) else None


def _serialize(item: MediaItem) -> dict[str, object]:
    return {
        "id": item.id,
        "type": item.media_type.value,
        "source_url": item.source_url,
        "author": item.author,
        "domain": item.domain,
        "width": item.width,
        "height": item.height,
        "file_size": item.file_size,
        "created_at": item.created_at,
        "active_revision_id": item.active_revision_id,
        "is_edited": bool(item.edit_operations),
        "edit_operations": list(item.edit_operations),
        "tags": list(item.tags),
        "character_tags": list(item.character_tags),
        "characters": list(item.character_tags),
        "parent_id": item.parent_id,
        "parent_media_id": item.parent_media_id,
        "remote_id": item.remote_id,
        "parent_url": item.parent_url,
        "has_parent": bool(item.parent_id),
        "content_url": f"/api/v1/media/{item.id}/content?revision={item.active_revision_id or 0}",
        "thumbnail_url": f"/api/v1/media/{item.id}/thumbnail?revision={item.active_revision_id or 0}",
    }


def _error(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message, "details": {}}}), status
