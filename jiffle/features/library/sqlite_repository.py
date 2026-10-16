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
        order_by = self._order_by_clause(query.sort)
        rows = self.connection.execute(
            "SELECT item.* FROM media_items item "
            f"{where_sql} ORDER BY {order_by} LIMIT ? OFFSET ?",
            (*parameters, query.limit, query.offset),
        ).fetchall()
        items = self._hydrate_media_items(rows)
        return LibraryPage(items, int(total), query.limit, query.offset)

    def _order_by_clause(self, sort: str) -> str:
        sort_key = (sort or "newest").lower()
        if sort_key == "oldest":
            return "item.id ASC"
        if sort_key == "resolution":
            return "(COALESCE(item.width, 0) * COALESCE(item.height, 0)) DESC, item.id DESC"
        if sort_key == "file_size":
            return "COALESCE(item.file_size, 0) DESC, item.id DESC"
        if sort_key == "random":
            return "RANDOM()"
        if sort_key == "least_used":
            if self._table_exists("collection_items"):
                return "(SELECT COUNT(*) FROM collection_items ci WHERE ci.media_item_id = item.id) ASC, item.id DESC"
            return "item.id DESC"
        if sort_key == "most_used":
            if self._table_exists("collection_items"):
                return "(SELECT COUNT(*) FROM collection_items ci WHERE ci.media_item_id = item.id) DESC, item.id DESC"
            return "item.id DESC"
        return "item.id DESC"

    def get_media(self, media_id: int) -> MediaItem | None:
        row = self.connection.execute(
            "SELECT * FROM media_items WHERE id = ? AND deleted_at IS NULL", (media_id,)
        ).fetchone()
        return self._hydrate_media_items((row,))[0] if row else None

    def _to_media_item(self, row: sqlite3.Row) -> MediaItem:
        return self._hydrate_media_items((row,))[0]

    def _hydrate_media_items(self, rows) -> tuple[MediaItem, ...]:
        if not rows:
            return ()
        media_ids = [int(row["id"]) for row in rows]
        id_placeholders = ",".join("?" for _ in media_ids)
        keys = set(rows[0].keys())

        source_by_media_id: dict[int, sqlite3.Row] = {}
        source_keys: set[str] = set()
        if self._table_has_column("media_sources", "media_item_id"):
            order_parts = ["media_item_id"]
            if self._table_has_column("media_sources", "author"):
                order_parts.append(
                    "CASE WHEN author IS NOT NULL AND TRIM(author) <> '' THEN 0 ELSE 1 END"
                )
            order_parts.append(
                "id ASC" if self._table_has_column("media_sources", "id") else "rowid ASC"
            )
            source_rows = self.connection.execute(
                f"SELECT * FROM media_sources WHERE media_item_id IN ({id_placeholders}) "
                f"ORDER BY {', '.join(order_parts)}",
                media_ids,
            ).fetchall()
            if source_rows:
                source_keys = set(source_rows[0].keys())
            for source_row in source_rows:
                source_by_media_id.setdefault(int(source_row["media_item_id"]), source_row)

        parent_media_by_key: dict[tuple[object, str], int] = {}
        if source_by_media_id and "provider" in source_keys and "remote_id" in source_keys:
            parent_pairs: list[tuple[object, str]] = []
            seen_pairs: set[tuple[object, str]] = set()
            for row in rows:
                item_id = int(row["id"])
                source_row = source_by_media_id.get(item_id)
                if source_row is None:
                    continue
                remote_id = source_row["remote_id"]
                parent_id = row["parent_id"] if "parent_id" in keys else None
                if parent_id is None and "parent_id" in source_keys:
                    parent_id = source_row["parent_id"]
                provider = source_row["provider"]
                if remote_id and provider and parent_id:
                    pair = (provider, str(parent_id))
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        parent_pairs.append(pair)
            if parent_pairs:
                pair_clauses = " OR ".join(
                    "(source.provider = ? AND source.remote_id = ?)" for _ in parent_pairs
                )
                pair_params = [value for pair in parent_pairs for value in pair]
                for parent_row in self.connection.execute(
                    "SELECT source.provider, source.remote_id, item.id "
                    "FROM media_items item "
                    "JOIN media_sources source ON source.media_item_id = item.id "
                    f"WHERE item.deleted_at IS NULL AND ({pair_clauses}) "
                    "ORDER BY item.id ASC",
                    pair_params,
                ).fetchall():
                    parent_media_by_key.setdefault(
                        (parent_row["provider"], str(parent_row["remote_id"])),
                        int(parent_row["id"]),
                    )

        has_tag_origin = self._table_has_column("media_tags", "origin")
        tag_sql = (
            f"SELECT media_item_id, tag, origin FROM media_tags "
            f"WHERE media_item_id IN ({id_placeholders}) ORDER BY media_item_id, tag"
            if has_tag_origin
            else f"SELECT media_item_id, tag, NULL AS origin FROM media_tags "
            f"WHERE media_item_id IN ({id_placeholders}) ORDER BY media_item_id, tag"
        )
        tags_by_media_id: dict[int, list[str]] = {}
        manual_tags_by_media_id: dict[int, list[str]] = {}
        for tag_row in self.connection.execute(tag_sql, media_ids).fetchall():
            item_id = int(tag_row[0])
            tags_by_media_id.setdefault(item_id, []).append(tag_row[1])
            if tag_row[2] == "manual":
                manual_tags_by_media_id.setdefault(item_id, []).append(tag_row[1])

        fragment_counts: dict[int, int] = {}
        if "derived_from_media_id" in keys:
            for frag_row in self.connection.execute(
                f"SELECT derived_from_media_id, COUNT(*) FROM media_items "
                f"WHERE derived_from_media_id IN ({id_placeholders}) AND deleted_at IS NULL "
                f"GROUP BY derived_from_media_id",
                media_ids,
            ).fetchall():
                fragment_counts[int(frag_row[0])] = int(frag_row[1])

        family_members: dict[int, list[int]] = {}
        if "family_id" in keys and self._table_exists("media_families"):
            family_ids = list(
                dict.fromkeys(
                    int(row["family_id"])
                    for row in rows
                    if row["family_id"] is not None
                )
            )
            if family_ids:
                family_placeholders = ",".join("?" for _ in family_ids)
                for member_row in self.connection.execute(
                    f"SELECT family_id, id FROM media_items "
                    f"WHERE family_id IN ({family_placeholders}) AND deleted_at IS NULL "
                    f"ORDER BY id",
                    family_ids,
                ).fetchall():
                    family_members.setdefault(int(member_row[0]), []).append(
                        int(member_row[1])
                    )

        edit_operations_by_media_id: dict[int, list[str]] = {}
        if "active_revision_id" in keys and self._table_exists("media_revisions"):
            revision_media_ids = [
                int(row["id"])
                for row in rows
                if row["active_revision_id"] is not None
            ]
            if revision_media_ids:
                rev_placeholders = ",".join("?" for _ in revision_media_ids)
                for rev_row in self.connection.execute(
                    "WITH RECURSIVE chain(media_item_id, id, parent_revision_id, operation, depth) AS ("
                    "SELECT m.id, r.id, r.parent_revision_id, r.operation, 0 "
                    "FROM media_revisions r "
                    "JOIN media_items m ON m.active_revision_id = r.id "
                    f"WHERE m.id IN ({rev_placeholders}) AND r.operation <> 'original' "
                    "UNION ALL "
                    "SELECT child.media_item_id, parent.id, parent.parent_revision_id, parent.operation, child.depth + 1 "
                    "FROM media_revisions parent "
                    "JOIN chain child ON parent.id = child.parent_revision_id "
                    "WHERE parent.operation <> 'original'"
                    ") "
                    "SELECT media_item_id, operation FROM chain ORDER BY media_item_id, id",
                    revision_media_ids,
                ).fetchall():
                    edit_operations_by_media_id.setdefault(int(rev_row[0]), []).append(
                        str(rev_row[1])
                    )

        items: list[MediaItem] = []
        for row in rows:
            item_id = int(row["id"])
            # Older imports may have left media_items.author empty while retaining
            # the provider metadata in media_sources. Use that metadata for display.
            author = row["author"]
            source_row = source_by_media_id.get(item_id)
            if not author and source_row is not None and "author" in source_keys:
                source_author = source_row["author"]
                if source_author and str(source_author).strip():
                    author = source_author
            tags = tuple(tags_by_media_id.get(item_id, ()))
            manual_tags = tuple(manual_tags_by_media_id.get(item_id, ()))
            parent_id = row["parent_id"] if "parent_id" in keys else None
            character_tags = _decode_tags(
                row["character_tags_json"] if "character_tags_json" in keys else None
            )
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
            fragment_count = fragment_counts.get(item_id, 0)
            relatives: tuple[int, ...] = ()
            if source_row is not None:
                remote_id = (
                    source_row["remote_id"] if "remote_id" in source_keys else None
                )
                if parent_id is None and "parent_id" in source_keys:
                    parent_id = source_row["parent_id"]
                if not character_tags and "character_tags_json" in source_keys:
                    character_tags = _decode_tags(source_row["character_tags_json"])
                if remote_id and "provider" in source_keys and parent_id:
                    parent_media_id = parent_media_by_key.get(
                        (source_row["provider"], str(parent_id))
                    )
                if parent_id:
                    canonical = (
                        source_row["canonical_url"]
                        if "canonical_url" in source_keys
                        else None
                    )
                    parent_url = (
                        _parent_url(canonical or row["source_url"] or "", parent_id)
                        or None
                    )
            if family_id is not None and family_members:
                relatives = tuple(
                    member_id
                    for member_id in family_members.get(int(family_id), ())
                    if member_id != item_id
                )
            active_revision_id = (
                row["active_revision_id"] if "active_revision_id" in keys else None
            )
            items.append(
                MediaItem(
                    id=row["id"],
                    file_path=row["file_path"],
                    media_type=MediaType(row["media_type"]),
                    source_url=row["source_url"],
                    file_source_url=(
                        row["file_source_url"] if "file_source_url" in keys else None
                    ),
                    author=author,
                    domain=row["domain"],
                    width=row["width"],
                    height=row["height"],
                    file_size=row["file_size"],
                    content_hash=row["content_hash"],
                    active_revision_id=active_revision_id,
                    edit_operations=(
                        tuple(edit_operations_by_media_id.get(item_id, ()))
                        if active_revision_id is not None
                        else ()
                    ),
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
                        int(derived_from_media_id)
                        if derived_from_media_id is not None
                        else None
                    ),
                    trim_start_ms=(
                        int(trim_start_ms) if trim_start_ms is not None else None
                    ),
                    trim_end_ms=int(trim_end_ms) if trim_end_ms is not None else None,
                    trim_index=int(trim_index) if trim_index is not None else None,
                    fragment_count=fragment_count,
                    auto_collection_excluded=fragment_count > 0,
                    is_animated=bool(is_animated) if is_animated is not None else None,
                )
            )
        return tuple(items)


def _build_filter(
    query: LibraryQuery, connection, has_column=None
) -> tuple[str, tuple[object, ...]]:
    def col_exists(table: str, column: str) -> bool:
        if has_column is not None:
            return has_column(table, column)
        return _table_has_column(connection, table, column)

    clauses: list[str] = ["item.deleted_at IS NULL"]
    parameters: list[object] = []

    media_ids: list[int] = list(query.media_ids)
    if query.media_id is not None and query.media_id not in media_ids:
        media_ids.append(query.media_id)
    if len(media_ids) == 1:
        clauses.append("item.id = ?")
        parameters.append(media_ids[0])
    elif len(media_ids) > 1:
        clauses.append(f"item.id IN ({','.join('?' for _ in media_ids)})")
        parameters.extend(media_ids)

    if query.exclude_media_ids:
        clauses.append(
            f"item.id NOT IN ({','.join('?' for _ in query.exclude_media_ids)})"
        )
        parameters.extend(query.exclude_media_ids)

    include_tags = tuple(
        dict.fromkeys([*(query.tags or ()), *((query.tag,) if query.tag else ())])
    )
    exclude_tags = tuple(
        dict.fromkeys(
            [
                *(query.excluded_tags or ()),
                *((query.exclude_tag,) if query.exclude_tag else ()),
            ]
        )
    )
    aliases: dict[str, set[str]] = {}
    if (include_tags or exclude_tags) and _table_exists(connection, "tag_aliases"):
        for row in connection.execute(
            "SELECT canonical_tag, alias FROM tag_aliases"
        ).fetchall():
            aliases.setdefault(row["canonical_tag"].lower(), set()).add(
                row["alias"].lower()
            )
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
            "EXISTS (SELECT 1 FROM media_tags tag WHERE tag.media_item_id = item.id AND LOWER(tag.tag) IN ("
            + ",".join("?" for _ in values)
            + "))"
        )
        parameters.extend(values)
    for tag_value in exclude_tags:
        values = expanded(tag_value)
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM media_tags excluded_tag WHERE excluded_tag.media_item_id = item.id AND LOWER(excluded_tag.tag) IN ("
            + ",".join("?" for _ in values)
            + "))"
        )
        parameters.extend(values)

    author_expr = (
        "(',' || LOWER(REPLACE(COALESCE(NULLIF(TRIM(item.author), ''), "
        "(SELECT author FROM media_sources source "
        "WHERE source.media_item_id = item.id AND author IS NOT NULL "
        "AND TRIM(author) <> '' LIMIT 1), ''), ', ', ',')) || ',')"
    )
    authors = list(
        dict.fromkeys(
            [
                *(a.strip().lower() for a in query.authors if a and a.strip()),
                *(
                    (query.author.strip().lower(),)
                    if query.author and query.author.strip()
                    else ()
                ),
            ]
        )
    )
    if len(authors) == 1:
        clauses.append(f"{author_expr} LIKE ?")
        parameters.append(f"%,{authors[0]},%")
    elif len(authors) > 1:
        clauses.append(
            "(" + " OR ".join(f"{author_expr} LIKE ?" for _ in authors) + ")"
        )
        parameters.extend(f"%,{a},%" for a in authors)

    for ex_author in query.exclude_authors:
        cleaned = ex_author.strip().lower()
        if cleaned:
            clauses.append(f"{author_expr} NOT LIKE ?")
            parameters.append(f"%,{cleaned},%")

    domains = list(
        dict.fromkeys(
            [
                *(d.strip().lower() for d in query.domains if d and d.strip()),
                *(
                    (query.domain.strip().lower(),)
                    if query.domain and query.domain.strip()
                    else ()
                ),
            ]
        )
    )
    if domains:
        domain_subclauses = []
        for dom in domains:
            domain_subclauses.append(
                "(LOWER(COALESCE(item.domain, '')) = ? OR LOWER(COALESCE(item.domain, '')) LIKE ?)"
            )
            parameters.extend((dom, f"%{dom}%"))
        clauses.append("(" + " OR ".join(domain_subclauses) + ")")

    for ex_dom in query.exclude_domains:
        cleaned = ex_dom.strip().lower()
        if cleaned:
            clauses.append(
                "NOT (LOWER(COALESCE(item.domain, '')) = ? OR LOWER(COALESCE(item.domain, '')) LIKE ?)"
            )
            parameters.extend((cleaned, f"%{cleaned}%"))

    has_source_provider = col_exists("media_sources", "provider")
    if query.sources:
        source_subclauses = []
        for src in query.sources:
            cleaned = src.strip().lower()
            if not cleaned:
                continue
            if has_source_provider:
                source_subclauses.append(
                    "(EXISTS (SELECT 1 FROM media_sources s WHERE s.media_item_id = item.id AND LOWER(s.provider) = ?) "
                    "OR LOWER(COALESCE(item.domain, '')) LIKE ?)"
                )
                parameters.extend((cleaned, f"%{cleaned}%"))
            else:
                source_subclauses.append("LOWER(COALESCE(item.domain, '')) LIKE ?")
                parameters.append(f"%{cleaned}%")
        if source_subclauses:
            clauses.append("(" + " OR ".join(source_subclauses) + ")")

    for ex_src in query.exclude_sources:
        cleaned = ex_src.strip().lower()
        if not cleaned:
            continue
        if has_source_provider:
            clauses.append(
                "NOT (EXISTS (SELECT 1 FROM media_sources s WHERE s.media_item_id = item.id AND LOWER(s.provider) = ?) "
                "OR LOWER(COALESCE(item.domain, '')) LIKE ?)"
            )
            parameters.extend((cleaned, f"%{cleaned}%"))
        else:
            clauses.append("NOT (LOWER(COALESCE(item.domain, '')) LIKE ?)")
            parameters.append(f"%{cleaned}%")

    if query.media_type:
        clauses.append("item.media_type = ?")
        parameters.append(query.media_type.value)

    if query.parent_id:
        if col_exists("media_items", "parent_id"):
            clauses.append("item.parent_id = ?")
            parameters.append(query.parent_id)
        else:
            clauses.append(
                "EXISTS (SELECT 1 FROM media_sources source WHERE source.media_item_id=item.id AND source.parent_id = ?)"
            )
            parameters.append(query.parent_id)

    if query.remote_id:
        clauses.append(
            "EXISTS (SELECT 1 FROM media_sources source WHERE source.media_item_id=item.id AND source.remote_id = ?)"
        )
        parameters.append(query.remote_id)

    if query.parent_or_remote_id:
        por_parts: list[str] = []
        por_params: list[object] = []
        if col_exists("media_items", "parent_id"):
            por_parts.append("item.parent_id = ?")
            por_params.append(query.parent_or_remote_id)
        src_has_remote = col_exists("media_sources", "remote_id")
        src_has_parent = col_exists("media_sources", "parent_id")
        if src_has_remote and src_has_parent:
            por_parts.append(
                "EXISTS (SELECT 1 FROM media_sources s WHERE s.media_item_id = item.id AND (s.remote_id = ? OR s.parent_id = ?))"
            )
            por_params.extend((query.parent_or_remote_id, query.parent_or_remote_id))
        elif src_has_remote:
            por_parts.append(
                "EXISTS (SELECT 1 FROM media_sources s WHERE s.media_item_id = item.id AND s.remote_id = ?)"
            )
            por_params.append(query.parent_or_remote_id)
        elif src_has_parent:
            por_parts.append(
                "EXISTS (SELECT 1 FROM media_sources s WHERE s.media_item_id = item.id AND s.parent_id = ?)"
            )
            por_params.append(query.parent_or_remote_id)
        if por_parts:
            clauses.append("(" + " OR ".join(por_parts) + ")")
            parameters.extend(por_params)

    gif_sql = (
        "(LOWER(COALESCE(item.original_filename, '')) LIKE '%.gif' OR LOWER(COALESCE(item.file_path, '')) LIKE '%.gif')"
        if col_exists("media_items", "original_filename")
        else "LOWER(COALESCE(item.file_path, '')) LIKE '%.gif'"
    )
    has_is_animated_col = col_exists("media_items", "is_animated")
    animated_sql = (
        f"(item.is_animated = 1 OR {gif_sql})"
        if has_is_animated_col
        else gif_sql
    )

    if query.is_animated is True:
        clauses.append(animated_sql)
    elif query.is_animated is False:
        if has_is_animated_col:
            clauses.append(
                f"(COALESCE(item.is_animated, 0) = 0 AND (item.is_animated IS NOT NULL OR NOT ({gif_sql})))"
            )
        else:
            clauses.append(f"NOT ({gif_sql})")

    def flag_expression(flag_name: str) -> str | None:
        flag = flag_name.strip().lower()
        if flag == "parent":
            parts = []
            if col_exists("media_items", "parent_id"):
                parts.append(
                    "(item.parent_id IS NOT NULL AND TRIM(item.parent_id) <> '')"
                )
            if col_exists("media_sources", "parent_id"):
                parts.append(
                    "EXISTS (SELECT 1 FROM media_sources s WHERE s.media_item_id = item.id AND s.parent_id IS NOT NULL AND TRIM(s.parent_id) <> '')"
                )
            return "(" + " OR ".join(parts) + ")" if parts else "0"
        if flag == "children":
            if not col_exists("media_sources", "remote_id"):
                return "0"
            child_conds = []
            if col_exists("media_items", "parent_id"):
                child_conds.append("child.parent_id = self_s.remote_id")
            if col_exists("media_sources", "parent_id"):
                child_conds.append(
                    "EXISTS (SELECT 1 FROM media_sources child_s WHERE child_s.media_item_id = child.id AND child_s.parent_id = self_s.remote_id)"
                )
            if not child_conds:
                return "0"
            return (
                "EXISTS (SELECT 1 FROM media_sources self_s "
                "WHERE self_s.media_item_id = item.id AND self_s.remote_id IS NOT NULL AND TRIM(self_s.remote_id) <> '' "
                f"AND EXISTS (SELECT 1 FROM media_items child WHERE child.deleted_at IS NULL AND ({' OR '.join(child_conds)})))"
            )
        if flag == "family":
            if not col_exists("media_items", "family_id"):
                return "0"
            return (
                "(item.family_id IS NOT NULL AND EXISTS ("
                "SELECT 1 FROM media_items fam WHERE fam.deleted_at IS NULL "
                "AND fam.family_id = item.family_id AND fam.id <> item.id))"
            )
        if flag == "source":
            parts = ["(item.source_url IS NOT NULL AND TRIM(item.source_url) <> '')"]
            if col_exists("media_sources", "canonical_url"):
                parts.append(
                    "EXISTS (SELECT 1 FROM media_sources s WHERE s.media_item_id = item.id AND s.canonical_url IS NOT NULL AND TRIM(s.canonical_url) <> '')"
                )
            return "(" + " OR ".join(parts) + ")"
        if flag == "edited":
            parts = []
            if col_exists("media_items", "derived_from_media_id"):
                parts.append("item.derived_from_media_id IS NOT NULL")
            if _table_exists(connection, "media_revisions") and col_exists(
                "media_items", "active_revision_id"
            ):
                rev_conds = []
                if col_exists("media_revisions", "revision_number"):
                    rev_conds.append("COALESCE(r.revision_number, 1) > 1")
                if col_exists("media_revisions", "operation"):
                    rev_conds.append(
                        "(r.operation IS NOT NULL AND r.operation <> 'original')"
                    )
                if col_exists("media_revisions", "parent_revision_id"):
                    rev_conds.append("r.parent_revision_id IS NOT NULL")
                if rev_conds:
                    parts.append(
                        f"EXISTS (SELECT 1 FROM media_revisions r WHERE r.id = item.active_revision_id AND ({' OR '.join(rev_conds)}))"
                    )
            return "(" + " OR ".join(parts) + ")" if parts else "0"
        if flag in {"animated", "gif"}:
            return animated_sql
        return None

    for flag in query.has_flags:
        expr = flag_expression(flag)
        if expr:
            clauses.append(expr)

    for flag in query.exclude_flags:
        expr = flag_expression(flag)
        if expr:
            clauses.append(f"NOT ({expr})")

    if query.orientation:
        orient = query.orientation.strip().lower()
        if orient == "portrait":
            clauses.append("item.height > item.width")
        elif orient == "landscape":
            clauses.append("item.width > item.height")
        elif orient == "square":
            clauses.append("item.width = item.height AND item.width IS NOT NULL")

    if query.min_width is not None:
        clauses.append("item.width >= ?")
        parameters.append(query.min_width)
    if query.max_width is not None:
        clauses.append("item.width <= ?")
        parameters.append(query.max_width)
    if query.min_height is not None:
        clauses.append("item.height >= ?")
        parameters.append(query.min_height)
    if query.max_height is not None:
        clauses.append("item.height <= ?")
        parameters.append(query.max_height)

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
