from collections.abc import Sequence
import json
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
    return revision_id


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
