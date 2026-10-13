import json
import re
import sqlite3

from jiffle.features.library.domain import (
    LibraryPage,
    LibraryQuery,
    MediaItem,
    MediaType,
)
from jiffle.infrastructure.media_revisions import active_edit_operations


class SqliteLibraryRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self._column_cache: dict[tuple[str, str], bool] = {}
        self._table_cache: dict[str, bool] = {}

    def _table_has_column(self, table: str, column: str) -> bool:
        key = (table, column)
        if key not in self._column_cache:
            self._column_cache[key] = _table_has_column(self.connection, table, column)
        return self._column_cache[key]

    def _table_exists(self, table: str) -> bool:
        if table not in self._table_cache:
            self._table_cache[table] = _table_exists(self.connection, table)
        return self._table_cache[table]

    def list_media(self, query: LibraryQuery) -> LibraryPage:
        where_sql, parameters = _build_filter(
            query, self.connection, has_column=self._table_has_column
        )
        total = self.connection.execute(
            f"SELECT COUNT(*) FROM media_items item {where_sql}", parameters
        ).fetchone()[0]
        rows = self.connection.execute(
            "SELECT item.* FROM media_items item "
            f"{where_sql} ORDER BY item.id DESC LIMIT ? OFFSET ?",
            (*parameters, query.limit, query.offset),
        ).fetchall()
        items = tuple(self._to_media_item(row) for row in rows)
        return LibraryPage(items, int(total), query.limit, query.offset)

    def get_media(self, media_id: int) -> MediaItem | None:
        row = self.connection.execute(
            "SELECT * FROM media_items WHERE id = ? AND deleted_at IS NULL", (media_id,)
        ).fetchone()
        return self._to_media_item(row) if row else None

    def _to_media_item(self, row: sqlite3.Row) -> MediaItem:
        # Older imports may have left media_items.author empty while retaining
        # the provider metadata in media_sources. Use that metadata for display.
        author = row["author"]
        keys = set(row.keys())
        source_row = None
        if self._table_has_column("media_sources", "media_item_id"):
            if self._table_has_column("media_sources", "author"):
                source_row = self.connection.execute(
                    "SELECT * FROM media_sources WHERE media_item_id = ? "
                    "ORDER BY CASE WHEN author IS NOT NULL AND TRIM(author) <> '' THEN 0 ELSE 1 END "
                    "LIMIT 1",
                    (row["id"],),
                ).fetchone()
            else:
                source_row = self.connection.execute(
                    "SELECT * FROM media_sources WHERE media_item_id = ? LIMIT 1",
                    (row["id"],),
                ).fetchone()
        source_keys = set(source_row.keys()) if source_row is not None else set()
        if not author and source_row is not None and "author" in source_keys:
            source_author = source_row["author"]
            if source_author and str(source_author).strip():
                author = source_author
        has_tag_origin = self._table_has_column("media_tags", "origin")
        tag_rows = self.connection.execute(
            "SELECT tag, origin FROM media_tags WHERE media_item_id = ? ORDER BY tag"
            if has_tag_origin
            else "SELECT tag, NULL FROM media_tags WHERE media_item_id = ? ORDER BY tag",
            (row["id"],),
        ).fetchall()
        tags = tuple(tag_row[0] for tag_row in tag_rows)
        manual_tags = tuple(
            tag_row[0] for tag_row in tag_rows if tag_row[1] == "manual"
        )
        parent_id = row["parent_id"] if "parent_id" in keys else None
        character_tags = _decode_tags(row["character_tags_json"] if "character_tags_json" in keys else None)
        remote_id = None
        parent_media_id = None
        parent_url = None
        family_id = row["family_id"] if "family_id" in keys else None
        derived_from_media_id = (
            row["derived_from_media_id"] if "derived_from_media_id" in keys else None
        )
        trim_start_ms = row["trim_start_ms"] if "trim_start_ms" in keys else None
        trim_end_ms = row["trim_end_ms"] if "trim_end_ms" in keys else None
        trim_index = row["trim_index"] if "trim_index" in keys else None
        is_animated = row["is_animated"] if "is_animated" in keys else None
        fragment_count = 0
        if "derived_from_media_id" in keys:
            fragment_count = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM media_items "
                    "WHERE derived_from_media_id=? AND deleted_at IS NULL",
                    (row["id"],),
                ).fetchone()[0]
            )
        relatives: tuple[int, ...] = ()
        if source_row is not None:
            remote_id = source_row["remote_id"] if "remote_id" in source_keys else None
            if parent_id is None and "parent_id" in source_keys:
                parent_id = source_row["parent_id"]
            if not character_tags and "character_tags_json" in source_keys:
                character_tags = _decode_tags(source_row["character_tags_json"])
            if remote_id and "provider" in source_keys and parent_id:
                parent = self.connection.execute(
                    "SELECT item.id FROM media_items item "
                    "JOIN media_sources source ON source.media_item_id=item.id "
                    "WHERE item.deleted_at IS NULL AND source.provider=? AND source.remote_id=? "
                    "LIMIT 1",
                    (source_row["provider"], parent_id),
                ).fetchone()
                parent_media_id = int(parent[0]) if parent else None
            if parent_id:
                canonical = source_row["canonical_url"] if "canonical_url" in source_keys else None
                parent_url = _parent_url(canonical or row["source_url"] or "", parent_id) or None
        if family_id is not None and self._table_exists("media_families"):
            relatives = tuple(
                int(relative[0])
                for relative in self.connection.execute(
                    "SELECT id FROM media_items "
                    "WHERE family_id=? AND id<>? AND deleted_at IS NULL ORDER BY id",
                    (family_id, row["id"]),
                ).fetchall()
            )
        active_revision_id = row["active_revision_id"] if "active_revision_id" in keys else None
        return MediaItem(
            id=row["id"],
            file_path=row["file_path"],
            media_type=MediaType(row["media_type"]),
            source_url=row["source_url"],
            file_source_url=row["file_source_url"] if "file_source_url" in keys else None,
            author=author,
            domain=row["domain"],
            width=row["width"],
            height=row["height"],
            file_size=row["file_size"],
            content_hash=row["content_hash"],
            active_revision_id=active_revision_id,
            edit_operations=active_edit_operations(self.connection, row["id"])
            if active_revision_id is not None else (),
            created_at=row["created_at"],
            tags=tags,
            manual_tags=manual_tags,
            character_tags=character_tags,
            parent_id=parent_id,
            parent_media_id=parent_media_id,
            remote_id=remote_id,
            parent_url=parent_url,
            family_id=int(family_id) if family_id is not None else None,
            relatives=relatives,
            derived_from_media_id=(
                int(derived_from_media_id) if derived_from_media_id is not None else None
            ),
            trim_start_ms=int(trim_start_ms) if trim_start_ms is not None else None,
            trim_end_ms=int(trim_end_ms) if trim_end_ms is not None else None,
            trim_index=int(trim_index) if trim_index is not None else None,
            fragment_count=fragment_count,
            auto_collection_excluded=fragment_count > 0,
            is_animated=bool(is_animated) if is_animated is not None else None,
        )


def _build_filter(
    query: LibraryQuery, connection, has_column=None
) -> tuple[str, tuple[object, ...]]:
    clauses: list[str] = ["item.deleted_at IS NULL"]
    parameters: list[object] = []
    if query.media_id is not None:
        clauses.append("item.id = ?")
        parameters.append(query.media_id)
    include_tags = query.tags or ((query.tag,) if query.tag else ())
    exclude_tags = query.excluded_tags or ((query.exclude_tag,) if query.exclude_tag else ())
    aliases: dict[str, set[str]] = {}
    if include_tags or exclude_tags:
        for row in connection.execute("SELECT canonical_tag, alias FROM tag_aliases").fetchall():
            aliases.setdefault(row["canonical_tag"].lower(), set()).add(row["alias"].lower())
        for canonical, values in list(aliases.items()):
            values.add(canonical)

    def expanded(value):
        value = value.lower()
        for canonical, values in aliases.items():
            if value in values:
                return tuple(values)
        return (value,)

    for tag_value in include_tags:
        values = expanded(tag_value)
        clauses.append(
            "EXISTS (SELECT 1 FROM media_tags tag WHERE tag.media_item_id = item.id AND tag.tag IN ("
            + ",".join("?" for _ in values)
            + "))"
        )
        parameters.extend(values)
    for tag_value in exclude_tags:
        values = expanded(tag_value)
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM media_tags excluded_tag WHERE excluded_tag.media_item_id = item.id AND excluded_tag.tag IN ("
            + ",".join("?" for _ in values)
            + "))"
        )
        parameters.extend(values)
    if query.author:
        clauses.append(
            "(',' || LOWER(REPLACE(COALESCE(NULLIF(TRIM(item.author), ''), "
            "(SELECT author FROM media_sources source "
            "WHERE source.media_item_id = item.id AND author IS NOT NULL "
            "AND TRIM(author) <> '' LIMIT 1)), ', ', ',')) || ',') LIKE ?"
        )
        parameters.append(f"%,{query.author.strip().lower()},%")
    if query.domain:
        clauses.append("item.domain = ?")
        parameters.append(query.domain)
    if query.media_type:
        clauses.append("item.media_type = ?")
        parameters.append(query.media_type.value)
    if query.parent_id:
        check_column = (
            has_column("media_items", "parent_id")
            if has_column is not None
            else _table_has_column(connection, "media_items", "parent_id")
        )
        if check_column:
            clauses.append("item.parent_id = ?")
            parameters.append(query.parent_id)
        else:
            clauses.append("EXISTS (SELECT 1 FROM media_sources source WHERE source.media_item_id=item.id AND source.parent_id = ?)")
            parameters.append(query.parent_id)
    if query.remote_id:
        clauses.append("EXISTS (SELECT 1 FROM media_sources source WHERE source.media_item_id=item.id AND source.remote_id = ?)")
        parameters.append(query.remote_id)
    if query.text:
        pattern = f"%{query.text}%"
        clauses.append(
            "(item.author LIKE ? OR item.domain LIKE ? OR item.source_url LIKE ? "
            "OR EXISTS (SELECT 1 FROM media_tags text_tag "
            "WHERE text_tag.media_item_id = item.id AND text_tag.tag LIKE ?))"
        )
        parameters.extend((pattern, pattern, pattern, pattern))
    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    return where_sql, tuple(parameters)


def _table_has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in connection.execute(f"PRAGMA table_info({table})"))


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _decode_tags(raw) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return ()
    return tuple(str(value) for value in values if str(value).strip()) if isinstance(values, (list, tuple)) else ()


def _parent_url(canonical_url: str, parent_id: str) -> str:
    if "/posts/" in canonical_url:
        return re.sub(r"(/posts/)\d+", rf"\g<1>{parent_id}", canonical_url)
    if "id=" in canonical_url:
        return re.sub(r"([?&]id=)\d+", rf"\g<1>{parent_id}", canonical_url)
    if "/view/" in canonical_url:
        return re.sub(r"(/view/)\d+", rf"\g<1>{parent_id}", canonical_url)
    return canonical_url
