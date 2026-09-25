import json
from pathlib import Path
import shutil
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
    _move_file(source, quarantine)
    try:
        if merge_metadata:
            _merge_metadata(connection, keep, remove)
        for source_url in (remove["source_url"], remove["file_source_url"] if "file_source_url" in remove.keys() else None):
            if source_url:
                connection.execute(
                    "INSERT OR IGNORE INTO blocked_media_signatures (source_url, reason) VALUES (?, 'deleted')",
                    (source_url,),
                )
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
        try:
            _move_file(quarantine, source)
        except DuplicateFailure:
            # Keep the original failure; a quarantined file can be recovered
            # manually from the delete-quarantine directory.
            pass
        raise
    quarantine.unlink(missing_ok=True)
    return keep_id


def mark_match_as_family(connection: sqlite3.Connection, match_id: int) -> int:
    """Keep both matched files and place them in one user-defined family."""
    match = _pending_match(connection, match_id)
    media_ids = (int(match["left_media_id"]), int(match["right_media_id"]))
    rows = connection.execute(
        "SELECT id, family_id FROM media_items "
        "WHERE id IN (?, ?) AND deleted_at IS NULL ORDER BY id",
        media_ids,
    ).fetchall()
    if len(rows) != 2:
        raise DuplicateFailure("duplicates.media_missing", "A matched media item is missing.")

    family_ids = {int(row["family_id"]) for row in rows if row["family_id"] is not None}
    if not family_ids:
        cursor = connection.execute("INSERT INTO media_families DEFAULT VALUES")
        family_id = int(cursor.lastrowid)
    else:
        family_id = min(family_ids)
        if len(family_ids) > 1:
            placeholders = ", ".join("?" for _ in family_ids)
            connection.execute(
                f"UPDATE media_items SET family_id=? WHERE family_id IN ({placeholders})",
                (family_id, *family_ids),
            )
            connection.execute(
                f"DELETE FROM media_families WHERE id IN ({placeholders}) AND id<>?",
                (*family_ids, family_id),
            )
    connection.execute(
        "UPDATE media_items SET family_id=? WHERE id IN (?, ?)",
        (family_id, *media_ids),
    )
    connection.execute(
        "UPDATE duplicate_matches SET status='resolved', resolution='family', "
        "resolved_at=CURRENT_TIMESTAMP WHERE id=?",
        (match_id,),
    )
    _history(connection, "duplicate.family", match_id, {
        "family_id": family_id,
        "media_ids": list(media_ids),
    })
    connection.commit()
    return family_id


def _merge_metadata(connection, keep, remove):
    connection.execute(
        "INSERT OR IGNORE INTO media_tags (media_item_id, tag) "
        "SELECT ?, tag FROM media_tags WHERE media_item_id=?",
        (keep["id"], remove["id"]),
    )
    updates = {}
    available = set(keep.keys())
    for field in ("source_url", "file_source_url", "author", "domain", "parent_id"):
        if field not in available:
            continue
        if not keep[field] and remove[field]:
            updates[field] = remove[field]
    if "character_tags_json" in available:
        keep_characters = _json_tags(keep["character_tags_json"])
        remove_characters = _json_tags(remove["character_tags_json"])
        merged_characters = sorted(keep_characters | remove_characters)
        if merged_characters != sorted(keep_characters):
            updates["character_tags_json"] = json.dumps(merged_characters)
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


def _json_tags(raw):
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return set()
    return {str(value) for value in values} if isinstance(values, list) else set()


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


def _move_file(source: Path, destination: Path) -> None:
    """Move a file even when the two paths live on different filesystems.

    The media library and the application state are separate mounts on the NAS
    (the library is on the HDD volume, the state on the SSD volume), and
    ``os.replace`` fails there with ``EXDEV``. ``shutil.move`` keeps the same
    atomic rename on one filesystem and falls back to a copy-and-delete move
    across filesystems.
    """
    try:
        destination.unlink(missing_ok=True)
        shutil.move(str(source), str(destination))
    except OSError as error:
        # A half-written copy must not be mistaken for a complete quarantined file.
        if source.is_file() and destination.is_file():
            destination.unlink(missing_ok=True)
        raise DuplicateFailure(
            "duplicates.file_move_failed",
            "The removable media file could not be moved.",
        ) from error


def _history(connection, event_type, match_id, details):
    connection.execute(
        "INSERT INTO operation_history "
        "(event_type, entity_type, entity_id, details_json) "
        "VALUES (?, 'duplicate_match', ?, ?)",
        (event_type, match_id, json.dumps(details)),
    )
