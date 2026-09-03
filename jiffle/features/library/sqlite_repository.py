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

    def list_media(self, query: LibraryQuery) -> LibraryPage:
        where_sql, parameters = _build_filter(query, self.connection)
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
        if _table_has_column(self.connection, "media_sources", "media_item_id"):
            source_row = self.connection.execute(
                "SELECT * FROM media_sources WHERE media_item_id = ? LIMIT 1",
                (row["id"],),
            ).fetchone()
        if not author:
            if source_row is not None and source_row["author"]:
                author = source_row["author"]
            else:
                author_row = self.connection.execute(
                    "SELECT author FROM media_sources "
                    "WHERE media_item_id = ? AND author IS NOT NULL AND TRIM(author) <> '' "
                    "LIMIT 1",
                    (row["id"],),
                ).fetchone() if _table_has_column(self.connection, "media_sources", "author") else None
                author = author_row["author"] if author_row else None
        tags = tuple(
            tag_row[0]
            for tag_row in self.connection.execute(
                "SELECT tag FROM media_tags WHERE media_item_id = ? ORDER BY tag",
                (row["id"],),
            )
        )
        parent_id = row["parent_id"] if "parent_id" in keys else None
        character_tags = _decode_tags(row["character_tags_json"] if "character_tags_json" in keys else None)
        remote_id = None
        parent_media_id = None
        parent_url = None
        if source_row is not None:
            remote_id = source_row["remote_id"] if "remote_id" in source_row.keys() else None
            if parent_id is None and "parent_id" in source_row.keys():
                parent_id = source_row["parent_id"]
            if not character_tags and "character_tags_json" in source_row.keys():
                character_tags = _decode_tags(source_row["character_tags_json"])
            if remote_id and "provider" in source_row.keys() and parent_id:
                parent = self.connection.execute(
                    "SELECT item.id FROM media_items item "
                    "JOIN media_sources source ON source.media_item_id=item.id "
                    "WHERE item.deleted_at IS NULL AND source.provider=? AND source.remote_id=? "
                    "LIMIT 1",
                    (source_row["provider"], parent_id),
                ).fetchone()
                parent_media_id = int(parent[0]) if parent else None
            if parent_id:
                canonical = source_row["canonical_url"] if "canonical_url" in source_row.keys() else None
                parent_url = _parent_url(canonical or row["source_url"] or "", parent_id) or None
        return MediaItem(
            id=row["id"],
            file_path=row["file_path"],
            media_type=MediaType(row["media_type"]),
            source_url=row["source_url"],
            author=author,
            domain=row["domain"],
            width=row["width"],
            height=row["height"],
            file_size=row["file_size"],
            content_hash=row["content_hash"],
            active_revision_id=row["active_revision_id"] if "active_revision_id" in row.keys() else None,
            edit_operations=active_edit_operations(self.connection, row["id"])
            if "active_revision_id" in row.keys() else (),
            created_at=row["created_at"],
            tags=tags,
            character_tags=character_tags,
            parent_id=parent_id,
            parent_media_id=parent_media_id,
            remote_id=remote_id,
            parent_url=parent_url,
        )


def _build_filter(query: LibraryQuery, connection) -> tuple[str, tuple[object, ...]]:
    clauses: list[str] = ["item.deleted_at IS NULL"]
    parameters: list[object] = []
    if query.media_id is not None:
        clauses.append("item.id = ?")
        parameters.append(query.media_id)
    include_tags = query.tags or ((query.tag,) if query.tag else ())
    exclude_tags = query.excluded_tags or ((query.exclude_tag,) if query.exclude_tag else ())
    aliases = {}
    for row in connection.execute("SELECT canonical_tag, alias FROM tag_aliases").fetchall():
        aliases.setdefault(row["canonical_tag"].lower(), set()).add(row["alias"].lower())
    for canonical, values in list(aliases.items()):
        values.add(canonical)
    def expanded(value):
        value = value.lower()
        for canonical, values in aliases.items():
            if value in values: return tuple(values)
        return (value,)
    for tag_value in include_tags:
        values = expanded(tag_value)
        clauses.append("EXISTS (SELECT 1 FROM media_tags tag WHERE tag.media_item_id = item.id AND tag.tag IN (" + ",".join("?" for _ in values) + "))")
        parameters.extend(values)
    for tag_value in exclude_tags:
        clauses.append("NOT EXISTS (SELECT 1 FROM media_tags excluded_tag WHERE excluded_tag.media_item_id = item.id AND excluded_tag.tag = ?)")
        parameters.append(tag_value)
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
        if _table_has_column(connection, "media_items", "parent_id"):
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
