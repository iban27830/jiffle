from collections.abc import Sequence
import json
import re
import sqlite3

PRESET_SHELF_COLORS: tuple[str, ...] = (
    "#0ea5e9",
    "#10b981",
    "#f59e0b",
    "#ef4444",
    "#8b5cf6",
    "#ec4899",
    "#14b8a6",
    "#6366f1",
    "#84cc16",
    "#f97316",
)

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class ShelfFailure(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def normalize_shelf_name(value: object) -> str:
    if not isinstance(value, str):
        raise ShelfFailure("shelves.invalid_name", "Shelf name is required.")
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned:
        raise ShelfFailure("shelves.invalid_name", "Shelf name cannot be empty.")
    if len(cleaned) > 80:
        raise ShelfFailure("shelves.invalid_name", "Shelf name must be 80 characters or fewer.")
    if any(ord(ch) < 32 for ch in cleaned):
        raise ShelfFailure("shelves.invalid_name", "Shelf name contains invalid characters.")
    return cleaned


def normalize_shelf_color(value: object, fallback: str | None = None) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        if fallback is not None:
            return fallback
        return PRESET_SHELF_COLORS[0]
    if not isinstance(value, str):
        raise ShelfFailure("shelves.invalid_color", "Shelf color must be a hex color like #0ea5e9.")
    cleaned = value.strip().lower()
    if not _HEX_COLOR_RE.match(cleaned):
        raise ShelfFailure("shelves.invalid_color", "Shelf color must be a 6-digit hex color like #0ea5e9.")
    return cleaned


def shelf_token(name: str) -> str:
    return re.sub(r"\s+", "_", name.strip())


def parse_shelf_ids(raw: object) -> tuple[int, ...]:
    if raw is None or raw is False or raw == "":
        return ()
    items: list[object] = []
    if isinstance(raw, int) and not isinstance(raw, bool):
        items = [raw]
    elif isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return ()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    items = parsed
            except Exception:
                items = stripped.split(",")
        else:
            items = stripped.split(",")
    elif isinstance(raw, Sequence):
        for entry in raw:
            if isinstance(entry, str) and ("," in entry or entry.strip().startswith("[")):
                items.extend(parse_shelf_ids(entry))
            else:
                items.append(entry)
    result: list[int] = []
    for entry in items:
        if isinstance(entry, bool):
            continue
        if isinstance(entry, int):
            val = entry
        elif isinstance(entry, str) and entry.strip().isdigit():
            val = int(entry.strip())
        else:
            continue
        if val > 0 and val not in result:
            result.append(val)
    return tuple(result)


def list_shelves(connection: sqlite3.Connection) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT
            s.id,
            s.name,
            s.color,
            s.sort_order,
            s.created_at,
            COUNT(item.id) AS item_count
        FROM shelves s
        LEFT JOIN media_shelf_items msi ON msi.shelf_id = s.id
        LEFT JOIN media_items item ON item.id = msi.media_item_id AND item.deleted_at IS NULL
        GROUP BY s.id
        ORDER BY s.sort_order ASC, LOWER(s.name) ASC, s.id ASC
        """
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "token": shelf_token(str(row["name"])),
            "color": str(row["color"]),
            "sort_order": int(row["sort_order"]),
            "item_count": int(row["item_count"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def get_shelf(connection: sqlite3.Connection, shelf_id: int) -> dict[str, object]:
    row = connection.execute(
        """
        SELECT
            s.id,
            s.name,
            s.color,
            s.sort_order,
            s.created_at,
            COUNT(item.id) AS item_count
        FROM shelves s
        LEFT JOIN media_shelf_items msi ON msi.shelf_id = s.id
        LEFT JOIN media_items item ON item.id = msi.media_item_id AND item.deleted_at IS NULL
        WHERE s.id = ?
        GROUP BY s.id
        """,
        (shelf_id,),
    ).fetchone()
    if row is None:
        raise ShelfFailure("shelves.not_found", "Shelf was not found.", 404)
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "token": shelf_token(str(row["name"])),
        "color": str(row["color"]),
        "sort_order": int(row["sort_order"]),
        "item_count": int(row["item_count"]),
        "created_at": row["created_at"],
    }


def create_shelf(
    connection: sqlite3.Connection,
    name: object,
    color: object = None,
) -> tuple[dict[str, object], bool]:
    normalized_name = normalize_shelf_name(name)
    existing = connection.execute(
        "SELECT id, color FROM shelves WHERE LOWER(name) = LOWER(?)",
        (normalized_name,),
    ).fetchone()
    if existing is not None:
        shelf_id = int(existing["id"])
        if color is not None and isinstance(color, str) and color.strip():
            new_color = normalize_shelf_color(color, str(existing["color"]))
            connection.execute(
                "UPDATE shelves SET color = ? WHERE id = ?",
                (new_color, shelf_id),
            )
            connection.commit()
        return get_shelf(connection, shelf_id), False

    count_row = connection.execute("SELECT COUNT(*), COALESCE(MAX(sort_order), -1) FROM shelves").fetchone()
    count = int(count_row[0]) if count_row else 0
    next_sort = (int(count_row[1]) + 1) if count_row else 0
    default_color = PRESET_SHELF_COLORS[count % len(PRESET_SHELF_COLORS)]
    resolved_color = normalize_shelf_color(color, fallback=default_color)
    cursor = connection.execute(
        "INSERT INTO shelves (name, color, sort_order) VALUES (?, ?, ?)",
        (normalized_name, resolved_color, next_sort),
    )
    shelf_id = int(cursor.lastrowid)
    connection.commit()
    return get_shelf(connection, shelf_id), True


def update_shelf(
    connection: sqlite3.Connection,
    shelf_id: int,
    *,
    name: object = None,
    color: object = None,
    sort_order: object = None,
) -> dict[str, object]:
    current = get_shelf(connection, shelf_id)
    new_name = normalize_shelf_name(name) if name is not None else str(current["name"])
    new_color = normalize_shelf_color(color, str(current["color"])) if color is not None else str(current["color"])
    if sort_order is not None:
        if isinstance(sort_order, bool) or not isinstance(sort_order, int):
            raise ShelfFailure("shelves.invalid_sort_order", "sort_order must be an integer.")
        new_sort = int(sort_order)
    else:
        new_sort = int(current["sort_order"])

    conflict = connection.execute(
        "SELECT id FROM shelves WHERE LOWER(name) = LOWER(?) AND id <> ?",
        (new_name, shelf_id),
    ).fetchone()
    if conflict is not None:
        raise ShelfFailure("shelves.duplicate_name", "A shelf with this name already exists.", 409)

    connection.execute(
        "UPDATE shelves SET name = ?, color = ?, sort_order = ? WHERE id = ?",
        (new_name, new_color, new_sort, shelf_id),
    )
    connection.commit()
    return get_shelf(connection, shelf_id)


def delete_shelf(connection: sqlite3.Connection, shelf_id: int) -> None:
    get_shelf(connection, shelf_id)
    connection.execute("DELETE FROM shelves WHERE id = ?", (shelf_id,))
    connection.commit()


def assign_shelves_to_media(
    connection: sqlite3.Connection,
    media_item_id: int | None,
    shelf_ids: Sequence[int] | object,
) -> list[dict[str, object]]:
    if not media_item_id or media_item_id <= 0:
        return []
    parsed_ids = parse_shelf_ids(shelf_ids)
    if not parsed_ids:
        return []
    placeholders = ",".join("?" for _ in parsed_ids)
    valid_rows = connection.execute(
        f"SELECT id, name, color, sort_order FROM shelves WHERE id IN ({placeholders}) "
        "ORDER BY sort_order ASC, LOWER(name) ASC, id ASC",
        parsed_ids,
    ).fetchall()
    if not valid_rows:
        return []
    item_exists = connection.execute(
        "SELECT 1 FROM media_items WHERE id = ? AND deleted_at IS NULL",
        (media_item_id,),
    ).fetchone()
    if not item_exists:
        return []
    connection.executemany(
        "INSERT OR IGNORE INTO media_shelf_items (shelf_id, media_item_id) VALUES (?, ?)",
        [(int(row["id"]), int(media_item_id)) for row in valid_rows],
    )
    return [
        {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "token": shelf_token(str(row["name"])),
            "color": str(row["color"]),
            "sort_order": int(row["sort_order"]),
        }
        for row in valid_rows
    ]


def resolve_shelf_summaries(
    connection: sqlite3.Connection,
    shelf_ids: Sequence[int] | object,
) -> list[dict[str, object]]:
    parsed_ids = parse_shelf_ids(shelf_ids)
    if not parsed_ids:
        return []
    placeholders = ",".join("?" for _ in parsed_ids)
    rows = connection.execute(
        f"SELECT id, name, color, sort_order FROM shelves WHERE id IN ({placeholders}) "
        "ORDER BY sort_order ASC, LOWER(name) ASC, id ASC",
        parsed_ids,
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "token": shelf_token(str(row["name"])),
            "color": str(row["color"]),
            "sort_order": int(row["sort_order"]),
        }
        for row in rows
    ]


def bulk_update_media_shelves(
    connection: sqlite3.Connection,
    media_ids: Sequence[int],
    shelf_ids: Sequence[int],
    action: str = "add",
) -> dict[str, int]:
    clean_media_ids = tuple(
        dict.fromkeys(int(mid) for mid in media_ids if isinstance(mid, int) and not isinstance(mid, bool) and mid > 0)
    )
    clean_shelf_ids = parse_shelf_ids(shelf_ids)
    if not clean_media_ids:
        raise ShelfFailure("shelves.invalid_media_ids", "At least one media item ID is required.")
    if not clean_shelf_ids:
        raise ShelfFailure("shelves.invalid_shelf_ids", "At least one shelf ID is required.")
    if action not in {"add", "remove"}:
        raise ShelfFailure("shelves.invalid_action", "Action must be 'add' or 'remove'.")

    shelf_placeholders = ",".join("?" for _ in clean_shelf_ids)
    valid_shelves = [
        int(row[0])
        for row in connection.execute(
            f"SELECT id FROM shelves WHERE id IN ({shelf_placeholders})",
            clean_shelf_ids,
        ).fetchall()
    ]
    if not valid_shelves:
        raise ShelfFailure("shelves.not_found", "Selected shelf was not found.", 404)

    media_placeholders = ",".join("?" for _ in clean_media_ids)
    live_media = [
        int(row[0])
        for row in connection.execute(
            f"SELECT id FROM media_items WHERE id IN ({media_placeholders}) AND deleted_at IS NULL",
            clean_media_ids,
        ).fetchall()
    ]
    if not live_media:
        return {"updated": 0, "added": 0, "already_present": 0, "removed": 0, "already_absent": 0}

    valid_shelf_ph = ",".join("?" for _ in valid_shelves)
    live_media_ph = ",".join("?" for _ in live_media)
    existing_pairs = {
        (int(row[0]), int(row[1]))
        for row in connection.execute(
            f"SELECT shelf_id, media_item_id FROM media_shelf_items "
            f"WHERE shelf_id IN ({valid_shelf_ph}) AND media_item_id IN ({live_media_ph})",
            (*valid_shelves, *live_media),
        ).fetchall()
    }

    if action == "add":
        added_media_ids = [
            mid for mid in live_media if any((sid, mid) not in existing_pairs for sid in valid_shelves)
        ]
        already_present = len(live_media) - len(added_media_ids)
        pairs = [(sid, mid) for sid in valid_shelves for mid in live_media if (sid, mid) not in existing_pairs]
        if pairs:
            connection.executemany(
                "INSERT OR IGNORE INTO media_shelf_items (shelf_id, media_item_id) VALUES (?, ?)",
                pairs,
            )
            connection.commit()
        return {
            "updated": len(added_media_ids),
            "added": len(added_media_ids),
            "already_present": already_present,
            "removed": 0,
            "already_absent": 0,
        }

    removed_media_ids = [
        mid for mid in live_media if any((sid, mid) in existing_pairs for sid in valid_shelves)
    ]
    already_absent = len(live_media) - len(removed_media_ids)
    if removed_media_ids:
        connection.execute(
            f"DELETE FROM media_shelf_items WHERE shelf_id IN ({valid_shelf_ph}) AND media_item_id IN ({live_media_ph})",
            (*valid_shelves, *live_media),
        )
        connection.commit()
    return {
        "updated": len(removed_media_ids),
        "added": 0,
        "already_present": 0,
        "removed": len(removed_media_ids),
        "already_absent": already_absent,
    }


def get_shelves_for_media_ids(
    connection: sqlite3.Connection,
    media_ids: Sequence[int],
) -> dict[int, tuple[dict[str, object], ...]]:
    if not media_ids:
        return {}
    placeholders = ",".join("?" for _ in media_ids)
    rows = connection.execute(
        f"""
        SELECT
            msi.media_item_id,
            s.id,
            s.name,
            s.color,
            s.sort_order
        FROM media_shelf_items msi
        JOIN shelves s ON s.id = msi.shelf_id
        WHERE msi.media_item_id IN ({placeholders})
        ORDER BY s.sort_order ASC, LOWER(s.name) ASC, s.id ASC
        """,
        tuple(media_ids),
    ).fetchall()
    grouped: dict[int, list[dict[str, object]]] = {}
    for row in rows:
        mid = int(row["media_item_id"])
        grouped.setdefault(mid, []).append(
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "token": shelf_token(str(row["name"])),
                "color": str(row["color"]),
                "sort_order": int(row["sort_order"]),
            }
        )
    return {mid: tuple(items) for mid, items in grouped.items()}
