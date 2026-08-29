import json
import os
from pathlib import Path
import sqlite3

from jiffle.configuration.settings import Settings


class DuplicateFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def ignore_match(connection: sqlite3.Connection, match_id: int) -> None:
    match = _pending_match(connection, match_id)
    connection.execute(
        "UPDATE duplicate_matches SET status='ignored', resolution='ignored', "
        "resolved_at=CURRENT_TIMESTAMP WHERE id=?", (match_id,)
    )
    _history(connection, "duplicate.ignored", match_id, {
        "left_media_id": match["left_media_id"],
        "right_media_id": match["right_media_id"],
    })
    connection.commit()


def resolve_match(
    connection: sqlite3.Connection,
    settings: Settings,
    match_id: int,
    keep_side: str,
    merge_metadata: bool,
) -> int:
    match = _pending_match(connection, match_id)
    keep_id = int(match[f"{keep_side}_media_id"])
    remove_side = "right" if keep_side == "left" else "left"
    remove_id = int(match[f"{remove_side}_media_id"])
    keep = connection.execute(
        "SELECT * FROM media_items WHERE id=? AND deleted_at IS NULL", (keep_id,)
    ).fetchone()
    remove = connection.execute(
        "SELECT * FROM media_items WHERE id=? AND deleted_at IS NULL", (remove_id,)
    ).fetchone()
    if keep is None or remove is None:
        raise DuplicateFailure("duplicates.media_missing", "A matched media item is missing.")
    source = _media_path(settings.media_path, remove["file_path"])
    if source is None or not source.is_file():
        raise DuplicateFailure("duplicates.file_missing", "The removable media file is missing.")
    quarantine_root = settings.database_path.parent / "delete-quarantine"
    quarantine_root.mkdir(parents=True, exist_ok=True)
    quarantine = quarantine_root / f"duplicate-{match_id}-{source.name}"
    os.replace(source, quarantine)
    try:
        if merge_metadata:
            _merge_metadata(connection, keep, remove)
        connection.execute(
            "UPDATE media_items SET deleted_at=CURRENT_TIMESTAMP, content_hash=NULL WHERE id=?",
            (remove_id,),
        )
        connection.execute(
            "UPDATE duplicate_matches SET status='resolved', resolution=?, "
            "resolved_at=CURRENT_TIMESTAMP WHERE id=?",
            (f"kept_{keep_side}" + ("_merged" if merge_metadata else ""), match_id),
        )
        _history(connection, "duplicate.resolved", match_id, {
            "kept_media_id": keep_id, "removed_media_id": remove_id,
            "metadata_merged": merge_metadata,
        })
        connection.commit()
    except Exception:
        connection.rollback()
        os.replace(quarantine, source)
        raise
    quarantine.unlink(missing_ok=True)
    return keep_id


def _merge_metadata(connection, keep, remove):
    connection.execute(
        "INSERT OR IGNORE INTO media_tags (media_item_id, tag) "
        "SELECT ?, tag FROM media_tags WHERE media_item_id=?",
        (keep["id"], remove["id"]),
    )
    updates = {}
    for field in ("source_url", "author", "domain"):
        if not keep[field] and remove[field]:
            updates[field] = remove[field]
    if updates:
        assignments = ", ".join(f"{field}=?" for field in updates)
        connection.execute(
            f"UPDATE media_items SET {assignments} WHERE id=?",
            (*updates.values(), keep["id"]),
        )
    keep_source = connection.execute(
        "SELECT 1 FROM media_sources WHERE media_item_id=?", (keep["id"],)
    ).fetchone()
    if not keep_source:
        connection.execute(
            "UPDATE media_sources SET media_item_id=? WHERE media_item_id=?",
            (keep["id"], remove["id"]),
        )


def _pending_match(connection, match_id):
    row = connection.execute(
        "SELECT * FROM duplicate_matches WHERE id=?", (match_id,)
    ).fetchone()
    if row is None:
        raise DuplicateFailure("duplicates.not_found", "Duplicate match was not found.")
    if row["status"] != "pending":
        raise DuplicateFailure("duplicates.already_resolved", "Duplicate match is already resolved.")
    return row


def _media_path(root_path: Path, stored_path: str) -> Path | None:
    root = root_path.resolve()
    candidate = (root / stored_path).resolve()
    return candidate if candidate.is_relative_to(root) else None


def _history(connection, event_type, match_id, details):
    connection.execute(
        "INSERT INTO operation_history "
        "(event_type, entity_type, entity_id, details_json) "
        "VALUES (?, 'duplicate_match', ?, ?)",
        (event_type, match_id, json.dumps(details)),
    )
