from pathlib import Path
import shutil
from datetime import datetime, timezone
import re

from flask import Blueprint, current_app, jsonify, request, send_file

from jiffle.configuration.settings import Settings
from jiffle.features.library.domain import LibraryQuery, MediaItem, MediaType
from jiffle.features.library.serialization import serialize_media
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
        "items": [serialize_media(item) for item in page.items],
        "page": {"total": page.total, "limit": page.limit, "offset": page.offset},
    })


@library_blueprint.get("/api/v1/media/<int:media_id>")
def get_media(media_id: int):
    item = _repository().get_media(media_id)
    if item is None:
        return _error("library.media_not_found", "Media item was not found.", 404)
    from jiffle.features.crop_editor.review import media_review
    return jsonify({**serialize_media(item), "crop_review": media_review(
        get_database(), media_id, current_app.config["JIFFLE_SETTINGS"].crop_review_enabled)})

@library_blueprint.delete("/api/v1/media/<int:media_id>")
def delete_media(media_id: int):
    connection = get_database()
    row = connection.execute("SELECT * FROM media_items WHERE id=? AND deleted_at IS NULL", (media_id,)).fetchone()
    if row is None:
        return _error("library.media_not_found", "Media item was not found.", 404)
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    root = settings.media_path.resolve()
    source = _safe_media_path(root, row["file_path"])
    shared_source = connection.execute(
        "SELECT 1 FROM media_items WHERE file_path = ? AND id <> ? AND deleted_at IS NULL LIMIT 1",
        (row["file_path"], media_id),
    ).fetchone()
    has_source_file = source is not None and source.is_file() and shared_source is None
    target: Path | None = None
    if has_source_file:
        quarantine = settings.database_path.parent / "delete-quarantine"
        quarantine.mkdir(parents=True, exist_ok=True)
        target = quarantine / f"{media_id}-{source.name}"
    revisions = (
        connection.execute(
            "SELECT file_path, content_hash FROM media_revisions WHERE media_item_id = ?",
            (media_id,),
        ).fetchall()
        if _has_table(connection, "media_revisions")
        else []
    )
    preview_rows = (
        connection.execute(
            "SELECT * FROM background_previews WHERE media_item_id = ?",
            (media_id,),
        ).fetchall()
        if _has_table(connection, "background_previews")
        else []
    )
    try:
        if has_source_file and target is not None:
            shutil.move(str(source), str(target))
        for source_url in (row["source_url"], row["file_source_url"] if "file_source_url" in row.keys() else None):
            if not source_url:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO blocked_media_signatures "
                "(source_url, reason) VALUES (?, 'deleted')", (source_url,)
            )
        content_hashes = [row["content_hash"]] if row["content_hash"] else []
        if settings.block_previously_deleted or revisions:
            for revision in revisions:
                if revision["content_hash"]:
                    content_hashes.append(revision["content_hash"])
        for content_hash in dict.fromkeys(content_hashes):
            connection.execute(
                "INSERT OR IGNORE INTO blocked_media_signatures "
                "(content_hash, reason) VALUES (?, 'deleted')", (content_hash,)
            )
        if _has_table(connection, "duplicate_matches"):
            connection.execute(
                "UPDATE duplicate_matches SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP "
                "WHERE (left_media_id = ? OR right_media_id = ?) AND status = 'pending'",
                (media_id, media_id),
            )
        if _has_table(connection, "collection_items"):
            connection.execute(
                "DELETE FROM collection_items WHERE media_item_id = ?",
                (media_id,),
            )
        if _has_table(connection, "background_previews"):
            connection.execute(
                "DELETE FROM background_previews WHERE media_item_id = ?",
                (media_id,),
            )
        connection.execute("UPDATE media_items SET deleted_at=CURRENT_TIMESTAMP, content_hash=NULL WHERE id=?", (media_id,))
        connection.execute("INSERT INTO operation_history (event_type, entity_type, entity_id, details_json) VALUES (?, ?, ?, ?)", ("media.deleted", "media", media_id, "{}"))
        connection.commit()
        if target is not None:
            target.unlink(missing_ok=True)
    except Exception:
        connection.rollback()
        if target is not None and target.exists() and source is not None and not source.exists():
            shutil.move(str(target), str(source))
        current_app.logger.exception("Could not delete media %s", media_id)
        return _error("library.delete_failed", "Media could not be deleted.", 500)
    for revision in revisions:
        revision_file = revision["file_path"]
        if not revision_file:
            continue
        shared = connection.execute(
            "SELECT 1 FROM media_items WHERE file_path = ? AND id <> ? AND deleted_at IS NULL LIMIT 1",
            (revision_file, media_id),
        ).fetchone()
        if shared is not None:
            continue
        revision_path = _safe_media_path(root, revision_file)
        if revision_path is not None and revision_path.is_file():
            revision_path.unlink(missing_ok=True)
    preview_dir = settings.database_path.parent / "background-previews"
    for preview_row in preview_rows:
        preview_keys = set(preview_row.keys())
        for column in ("file_path", "mask_path", "preview_path"):
            if column not in preview_keys or not preview_row[column]:
                continue
            raw_preview_path = str(preview_row[column])
            for candidate_root in (preview_dir, root, settings.database_path.parent):
                resolved_preview = _safe_media_path(candidate_root, raw_preview_path)
                if resolved_preview is not None and resolved_preview.is_file():
                    resolved_preview.unlink(missing_ok=True)
                    break
    if settings.thumbnail_path.is_dir():
        for thumbnail in settings.thumbnail_path.glob(f"media-{media_id}*.jpg"):
            thumbnail.unlink(missing_ok=True)
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


_VALID_SORTS = frozenset({
    "newest",
    "oldest",
    "resolution",
    "file_size",
    "random",
    "least_used",
    "most_used",
})
_VALID_ORIENTATIONS = frozenset({"portrait", "landscape", "square"})


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
    media_type: MediaType | None = None
    is_animated: bool | None = None
    if raw_type:
        lowered_type = raw_type.lower()
        if lowered_type in {"animated", "gif"}:
            is_animated = True
        else:
            try:
                media_type = MediaType(lowered_type)
            except ValueError:
                return None, "Type must be image, video, animated, or gif."

    media_ids_list: list[int] = []
    for raw_id_val in request.args.getlist("id"):
        for part in raw_id_val.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                parsed_id = int(part)
            except ValueError:
                return None, "Media ID must be an integer."
            if parsed_id < 1:
                return None, "Media ID must be a positive integer."
            if parsed_id not in media_ids_list:
                media_ids_list.append(parsed_id)
    media_id = media_ids_list[0] if media_ids_list else None

    exclude_media_ids_list: list[int] = []
    for raw_ex_id in request.args.getlist("exclude_id"):
        for part in raw_ex_id.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                parsed_ex_id = int(part)
            except ValueError:
                return None, "Excluded media ID must be an integer."
            if parsed_ex_id < 1:
                return None, "Excluded media ID must be a positive integer."
            if parsed_ex_id not in exclude_media_ids_list:
                exclude_media_ids_list.append(parsed_ex_id)

    orientation = _optional_parameter("orientation")
    if orientation is not None:
        orientation = orientation.lower()
        if orientation not in _VALID_ORIENTATIONS:
            return None, "Orientation must be portrait, landscape, or square."

    try:
        min_width = _optional_int("min_width")
        max_width = _optional_int("max_width")
        min_height = _optional_int("min_height")
        max_height = _optional_int("max_height")
    except ValueError:
        return None, "Dimension filters must be positive integers."

    sort = (_optional_parameter("sort") or "newest").lower()
    if sort not in _VALID_SORTS:
        return None, "Invalid sort order."

    text = _optional_parameter("q")
    remote_id = _optional_parameter("remote_id")
    parent_or_remote_id = _optional_parameter("parent")
    if parent_or_remote_id is None and text:
        parent_match = re.fullmatch(r"parent:(\d+)", text, re.IGNORECASE)
        if parent_match:
            parent_or_remote_id, text = parent_match.group(1), None

    authors = tuple(
        value.strip() for value in request.args.getlist("author") if value.strip()
    )
    exclude_authors = tuple(
        value.strip() for value in request.args.getlist("exclude_author") if value.strip()
    )
    domains = tuple(
        value.strip() for value in request.args.getlist("domain") if value.strip()
    )
    exclude_domains = tuple(
        value.strip() for value in request.args.getlist("exclude_domain") if value.strip()
    )
    sources = tuple(
        value.strip() for value in request.args.getlist("source") if value.strip()
    )
    exclude_sources = tuple(
        value.strip() for value in request.args.getlist("exclude_source") if value.strip()
    )
    has_flags = tuple(
        value.strip().lower() for value in request.args.getlist("has") if value.strip()
    )
    exclude_flags = tuple(
        value.strip().lower()
        for value in request.args.getlist("exclude_has")
        if value.strip()
    )

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
        authors=authors,
        exclude_authors=exclude_authors,
        domains=domains,
        exclude_domains=exclude_domains,
        sources=sources,
        exclude_sources=exclude_sources,
        media_ids=tuple(media_ids_list),
        exclude_media_ids=tuple(exclude_media_ids_list),
        parent_or_remote_id=parent_or_remote_id,
        is_animated=is_animated,
        has_flags=has_flags,
        exclude_flags=exclude_flags,
        orientation=orientation,
        min_width=min_width,
        max_width=max_width,
        min_height=min_height,
        max_height=max_height,
        sort=sort,
    ), None


def _optional_int(name: str) -> int | None:
    raw = _optional_parameter(name)
    if raw is None:
        return None
    val = int(raw)
    if val < 0:
        raise ValueError("Dimension must not be negative.")
    return val


def _optional_parameter(name: str) -> str | None:
    value = request.args.get(name, "").strip()
    return value or None


def _repository() -> SqliteLibraryRepository:
    return SqliteLibraryRepository(get_database())


def _safe_media_path(root: Path, stored_path: str | None) -> Path | None:
    if not stored_path:
        return None
    resolved_root = root.resolve()
    candidate = (resolved_root / stored_path).resolve()
    return candidate if candidate.is_relative_to(resolved_root) else None


def _has_table(connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _resolve_media_path(item: MediaItem) -> Path | None:
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    return _safe_media_path(settings.media_path, item.file_path)


def _error(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message, "details": {}}}), status
