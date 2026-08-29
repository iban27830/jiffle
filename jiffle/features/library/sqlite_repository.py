import sqlite3

from jiffle.features.library.domain import (
    LibraryPage,
    LibraryQuery,
    MediaItem,
    MediaType,
)


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
        if not author:
            source_row = self.connection.execute(
                "SELECT author FROM media_sources "
                "WHERE media_item_id = ? AND author IS NOT NULL AND TRIM(author) <> '' "
                "LIMIT 1",
                (row["id"],),
            ).fetchone()
            author = source_row["author"] if source_row else None
        tags = tuple(
            tag_row[0]
            for tag_row in self.connection.execute(
                "SELECT tag FROM media_tags WHERE media_item_id = ? ORDER BY tag",
                (row["id"],),
            )
        )
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
            created_at=row["created_at"],
            tags=tags,
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
