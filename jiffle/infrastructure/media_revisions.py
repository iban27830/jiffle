from collections.abc import Sequence
import json
from pathlib import Path
import sqlite3


def create_original_revision(connection, media_item_id: int, settings=None) -> int:
    row = connection.execute(
        "SELECT file_path, width, height, file_size, content_hash, active_revision_id "
        "FROM media_items WHERE id=?",
        (media_item_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Media item was not found.")
    if row[5] is not None:
        return int(row[5])
    cursor = connection.execute(
        "INSERT INTO media_revisions "
        "(media_item_id, file_path, operation, width, height, file_size, content_hash) "
        "VALUES (?, ?, 'original', ?, ?, ?, ?)",
        (media_item_id, row[0], row[1], row[2], row[3], row[4]),
    )
    revision_id = int(cursor.lastrowid)
    connection.execute("UPDATE media_items SET active_revision_id=? WHERE id=?", (revision_id, media_item_id))
    from jiffle.features.crop_editor.review import check_new_media
    check_new_media(connection, settings, media_item_id)
    _enroll_animation_and_trim_review(connection, settings, media_item_id)
    return revision_id


def _enroll_animation_and_trim_review(connection, settings, media_item_id: int) -> None:
    if settings is None:
        return
    try:
        item = connection.execute(
            "SELECT file_path, media_type, is_animated, derived_from_media_id, trim_review_status "
            "FROM media_items WHERE id=?",
            (media_item_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return
    if item is None:
        return
    file_path, media_type, is_animated, derived_from, trim_status = (
        item[0], item[1], item[2], item[3], item[4],
    )
    if media_type == "image" and is_animated is None:
        from jiffle.features.trim_editor.workflow import (
            ANIMATED_IMAGE_EXTENSIONS,
            is_animated_image,
            media_path,
        )
        suffix = Path(str(file_path or "")).suffix.lower()
        if suffix in ANIMATED_IMAGE_EXTENSIONS:
            resolved = media_path(settings.media_path, file_path)
            animated = (
                1
                if resolved is not None and resolved.is_file() and is_animated_image(resolved)
                else 0
            )
        else:
            animated = 0
        connection.execute(
            "UPDATE media_items SET is_animated=? WHERE id=?",
            (animated, media_item_id),
        )
        is_animated = animated
    if (
        getattr(settings, "trim_review_enabled", False)
        and trim_status is None
        and derived_from is None
        and (media_type == "video" or bool(is_animated))
    ):
        connection.execute(
            "UPDATE media_items SET trim_review_status='pending' "
            "WHERE id=? AND trim_review_status IS NULL AND derived_from_media_id IS NULL",
            (media_item_id,),
        )


def active_edit_operations(connection, media_item_id: int) -> tuple[str, ...]:
    rows = connection.execute(
        "WITH RECURSIVE chain(id, parent_revision_id, operation) AS ("
        "SELECT r.id, r.parent_revision_id, r.operation FROM media_revisions r "
        "JOIN media_items m ON m.active_revision_id=r.id WHERE m.id=? "
        "UNION ALL SELECT parent.id, parent.parent_revision_id, parent.operation "
        "FROM media_revisions parent JOIN chain child ON parent.id=child.parent_revision_id) "
        "SELECT operation FROM chain WHERE operation<>'original' ORDER BY id",
        (media_item_id,),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def active_edit_operations_map(
    connection: sqlite3.Connection,
    media_item_ids: Sequence[int],
) -> dict[int, tuple[str, ...]]:
    ids = tuple(dict.fromkeys(int(media_item_id) for media_item_id in media_item_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        "WITH RECURSIVE chain(media_item_id, id, parent_revision_id, operation) AS ("
        "SELECT m.id, r.id, r.parent_revision_id, r.operation FROM media_revisions r "
        f"JOIN media_items m ON m.active_revision_id=r.id WHERE m.id IN ({placeholders}) "
        "UNION ALL SELECT child.media_item_id, parent.id, parent.parent_revision_id, parent.operation "
        "FROM media_revisions parent JOIN chain child ON parent.id=child.parent_revision_id) "
        "SELECT media_item_id, operation FROM chain WHERE operation<>'original' ORDER BY media_item_id, id",
        ids,
    ).fetchall()
    operations: dict[int, list[str]] = {media_item_id: [] for media_item_id in ids}
    for row in rows:
        operations.setdefault(int(row[0]), []).append(str(row[1]))
    return {media_item_id: tuple(values) for media_item_id, values in operations.items()}


def revision_details(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}
