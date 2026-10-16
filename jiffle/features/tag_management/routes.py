import json

from flask import Blueprint, jsonify, request

from jiffle.features.library.serialization import serialize_media
from jiffle.features.library.sqlite_repository import SqliteLibraryRepository
from jiffle.features.tag_management.normalization import (
    escape_glob_prefix,
    normalize_tag,
    normalize_tag_name,
    normalize_tag_prefix,
)
from jiffle.infrastructure.database.connection import get_database


tag_management_blueprint = Blueprint("tag_management_v1", __name__)


@tag_management_blueprint.get("/api/v1/tags")
def list_tag_suggestions():
    raw_q = request.args.get("q")
    raw_prefix = (raw_q or "").strip()
    if not raw_prefix:
        return _error("tags.invalid_query", "A search prefix is required.", 400)
    try:
        limit = int(request.args.get("limit", 20))
    except (TypeError, ValueError):
        return _error("tags.invalid_query", "Limit must be an integer.", 400)
    limit = max(1, min(limit, 50))
    connection = get_database()

    lower_raw = raw_prefix.lower()
    if lower_raw.startswith("author:"):
        author_prefix = raw_prefix.split(":", 1)[1].strip().strip('"').lower()
        if len(author_prefix) < 1:
            return jsonify({"items": [], "query": raw_prefix})
        rows = connection.execute(
            "SELECT COALESCE(NULLIF(TRIM(item.author), ''), source.author) AS raw_author "
            "FROM media_items item "
            "LEFT JOIN media_sources source ON source.media_item_id = item.id "
            "WHERE item.deleted_at IS NULL"
        ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            raw_author = row["raw_author"]
            if not raw_author:
                continue
            seen_in_item: set[str] = set()
            for part in str(raw_author).split(","):
                normalized = part.strip().lower()
                if normalized and normalized not in seen_in_item:
                    seen_in_item.add(normalized)
                    counts[normalized] = counts.get(normalized, 0) + 1
        prefix_matches = sorted(
            ((a, c) for a, c in counts.items() if a.startswith(author_prefix)),
            key=lambda pair: (-pair[1], pair[0]),
        )
        substring_matches = sorted(
            (
                (a, c)
                for a, c in counts.items()
                if not a.startswith(author_prefix) and author_prefix in a
            ),
            key=lambda pair: (-pair[1], pair[0]),
        )
        combined = (prefix_matches + substring_matches)[:limit]
        return jsonify({
            "items": [
                {"tag": f"author:{a}", "count": int(c), "kind": "author"}
                for a, c in combined
            ],
            "query": raw_prefix,
        })

    if lower_raw.startswith(("source:", "domain:")):
        op, val_raw = raw_prefix.split(":", 1)
        op = op.lower()
        val_prefix = val_raw.strip().strip('"').lower()
        counts: dict[str, int] = {}
        if op == "source":
            rows = connection.execute(
                "SELECT LOWER(TRIM(source.provider)) AS val, COUNT(DISTINCT item.id) AS usage "
                "FROM media_items item "
                "JOIN media_sources source ON source.media_item_id = item.id "
                "WHERE item.deleted_at IS NULL "
                "AND source.provider IS NOT NULL AND TRIM(source.provider) <> '' "
                "GROUP BY LOWER(TRIM(source.provider))"
            ).fetchall()
            for row in rows:
                if row["val"]:
                    counts[str(row["val"])] = int(row["usage"])
        else:
            rows = connection.execute(
                "SELECT LOWER(TRIM(item.domain)) AS val, COUNT(*) AS usage "
                "FROM media_items item "
                "WHERE item.deleted_at IS NULL "
                "AND item.domain IS NOT NULL AND TRIM(item.domain) <> '' "
                "GROUP BY LOWER(TRIM(item.domain))"
            ).fetchall()
            for row in rows:
                if row["val"]:
                    counts[str(row["val"])] = int(row["usage"])
        prefix_matches = sorted(
            ((v, c) for v, c in counts.items() if v.startswith(val_prefix)),
            key=lambda pair: (-pair[1], pair[0]),
        )
        substring_matches = sorted(
            (
                (v, c)
                for v, c in counts.items()
                if not v.startswith(val_prefix) and val_prefix in v
            ),
            key=lambda pair: (-pair[1], pair[0]),
        )
        combined = (prefix_matches + substring_matches)[:limit]
        return jsonify({
            "items": [
                {"tag": f"{op}:{v}", "count": int(c), "kind": op}
                for v, c in combined
            ],
            "query": raw_prefix,
        })

    prefix = normalize_tag_name(raw_prefix)
    if not prefix:
        return _error("tags.invalid_query", "A search prefix is required.", 400)
    glob_pattern = escape_glob_prefix(prefix)
    rows = connection.execute(
        "SELECT LOWER(tag) AS tag, COUNT(*) AS usage FROM media_tags "
        "WHERE LOWER(tag) GLOB ? "
        "AND EXISTS (SELECT 1 FROM media_items item "
        "WHERE item.id = media_tags.media_item_id AND item.deleted_at IS NULL) "
        "GROUP BY LOWER(tag) ORDER BY usage DESC, LOWER(tag) ASC LIMIT ?",
        (glob_pattern, limit),
    ).fetchall()
    by_tag: dict[str, dict[str, object]] = {
        str(row["tag"]): {"tag": str(row["tag"]), "count": int(row["usage"])}
        for row in rows
    }

    alias_rows = connection.execute(
        "SELECT canonical_tag, alias FROM tag_aliases "
        "WHERE LOWER(alias) GLOB ? OR LOWER(canonical_tag) GLOB ? "
        "ORDER BY LOWER(canonical_tag), LOWER(alias)",
        (glob_pattern, glob_pattern),
    ).fetchall()
    if alias_rows:
        matched_canonicals: dict[str, str | None] = {}
        for row in alias_rows:
            canonical = str(row["canonical_tag"]).strip().lower()
            alias = str(row["alias"]).strip().lower()
            if not canonical:
                continue
            alias_matched = connection.execute(
                "SELECT ? GLOB ?", (alias, glob_pattern)
            ).fetchone()[0]
            if canonical not in matched_canonicals:
                matched_canonicals[canonical] = alias if alias_matched else None
            elif matched_canonicals[canonical] is None and alias_matched:
                matched_canonicals[canonical] = alias

        for canonical, matched_alias in matched_canonicals.items():
            group_rows = connection.execute(
                "SELECT alias FROM tag_aliases WHERE LOWER(canonical_tag) = ?",
                (canonical,),
            ).fetchall()
            group_tags = sorted(
                {canonical, *(str(r["alias"]).strip().lower() for r in group_rows if str(r["alias"]).strip())}
            )
            placeholders = ",".join("?" for _ in group_tags)
            group_count = int(
                connection.execute(
                    f"SELECT COUNT(DISTINCT mt.media_item_id) FROM media_tags mt "
                    f"JOIN media_items item ON item.id = mt.media_item_id "
                    f"WHERE item.deleted_at IS NULL AND LOWER(mt.tag) IN ({placeholders})",
                    group_tags,
                ).fetchone()[0]
            )
            if group_count > 0:
                entry: dict[str, object] = {"tag": canonical, "count": group_count}
                if matched_alias is not None:
                    entry["matched_alias"] = matched_alias
                by_tag[canonical] = entry

    items = sorted(
        by_tag.values(),
        key=lambda item: (-int(item["count"]), str(item["tag"])),
    )[:limit]

    if len(items) < limit:
        seen_tags = {str(item["tag"]).lower() for item in items}
        remaining = limit - len(items)
        like_escaped = (
            prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        sub_rows = connection.execute(
            "SELECT LOWER(tag) AS tag, COUNT(*) AS usage FROM media_tags "
            "WHERE LOWER(tag) LIKE ? ESCAPE '\\' "
            "AND EXISTS (SELECT 1 FROM media_items item "
            "WHERE item.id = media_tags.media_item_id AND item.deleted_at IS NULL) "
            "GROUP BY LOWER(tag) ORDER BY usage DESC, LOWER(tag) ASC LIMIT ?",
            (f"%{like_escaped}%", limit * 2),
        ).fetchall()
        for row in sub_rows:
            tag_val = str(row["tag"])
            if tag_val.lower() in seen_tags:
                continue
            seen_tags.add(tag_val.lower())
            items.append({"tag": tag_val, "count": int(row["usage"])})
            if len(items) >= limit:
                break

    return jsonify({
        "items": items[:limit],
        "query": prefix,
    })


list_tags = list_tag_suggestions


@tag_management_blueprint.post("/api/v1/media/<int:media_id>/tags")
def add_media_tag(media_id: int):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("tags.invalid_tag", "A JSON body with a tag is required.", 400)
    try:
        tag = normalize_tag(payload.get("tag"))
    except ValueError as error:
        return _error("tags.invalid_tag", str(error), 400)
    connection = get_database()
    if connection.execute(
        "SELECT 1 FROM media_items WHERE id=? AND deleted_at IS NULL", (media_id,)
    ).fetchone() is None:
        return _error("library.media_not_found", "Media item was not found.", 404)
    cursor = connection.execute(
        "INSERT INTO media_tags (media_item_id, tag, origin) VALUES (?, ?, 'manual') "
        "ON CONFLICT(media_item_id, tag) DO NOTHING",
        (media_id, tag),
    )
    added = cursor.rowcount > 0
    if added:
        _record_tag_change(connection, "media.tag_added", media_id, tag)
    connection.commit()
    media = _media_payload(media_id)
    media["tag_change"] = {"tag": tag, "added": bool(added)}
    return jsonify(media)


@tag_management_blueprint.delete("/api/v1/media/<int:media_id>/tags")
def remove_media_tag(media_id: int):
    try:
        tag = normalize_tag(request.args.get("tag"))
    except ValueError as error:
        return _error("tags.invalid_tag", str(error), 400)
    connection = get_database()
    if connection.execute(
        "SELECT 1 FROM media_items WHERE id=? AND deleted_at IS NULL", (media_id,)
    ).fetchone() is None:
        return _error("library.media_not_found", "Media item was not found.", 404)
    existing = connection.execute(
        "SELECT origin FROM media_tags WHERE media_item_id=? AND tag=?",
        (media_id, tag),
    ).fetchone()
    if existing is None:
        return _error("tags.tag_not_found", "This tag is not on the media item.", 404)
    if existing["origin"] != "manual":
        return _error(
            "tags.imported_tag", "Tags from the source post cannot be removed.", 409
        )
    connection.execute(
        "DELETE FROM media_tags WHERE media_item_id=? AND tag=?", (media_id, tag)
    )
    _record_tag_change(connection, "media.tag_removed", media_id, tag)
    connection.commit()
    media = _media_payload(media_id)
    media["tag_change"] = {"tag": tag, "added": False}
    return jsonify(media)


def _media_payload(media_id: int) -> dict:
    item = SqliteLibraryRepository(get_database()).get_media(media_id)
    return serialize_media(item)


def _record_tag_change(connection, event_type: str, media_id: int, tag: str) -> None:
    connection.execute(
        "INSERT INTO operation_history (event_type, entity_type, entity_id, details_json) "
        "VALUES (?, 'media', ?, ?)",
        (event_type, media_id, json.dumps({"tag": tag})),
    )


@tag_management_blueprint.get("/api/v1/tag-rules")
def get_tag_rules():
    connection = get_database()
    rows = connection.execute(
        "SELECT tag, disposition FROM tag_rules ORDER BY tag"
    ).fetchall()
    aliases = connection.execute(
        "SELECT canonical_tag, alias FROM tag_aliases ORDER BY canonical_tag, alias"
    ).fetchall()
    return jsonify({
        "preferred": [row["tag"] for row in rows if row["disposition"] == "preferred"],
        "blocked": [row["tag"] for row in rows if row["disposition"] == "blocked"],
        "aliases": _aliases_payload(aliases),
    })


@tag_management_blueprint.put("/api/v1/tag-rules")
def replace_tag_rules():
    payload = request.get_json(silent=True)
    try:
        preferred = _tag_list(payload, "preferred")
        blocked = _tag_list(payload, "blocked")
        aliases = _alias_map(payload)
    except ValueError as error:
        return _error("tags.invalid_rules", str(error), 400)
    if set(preferred) & set(blocked):
        return _error("tags.conflicting_rule", "A tag cannot be preferred and blocked.", 400)
    connection = get_database()
    try:
        connection.execute("DELETE FROM tag_aliases")
        connection.execute("DELETE FROM tag_rules")
        connection.executemany(
            "INSERT INTO tag_rules (tag, disposition) VALUES (?, 'preferred')",
            ((tag,) for tag in preferred),
        )
        connection.executemany(
            "INSERT INTO tag_rules (tag, disposition) VALUES (?, 'blocked')",
            ((tag,) for tag in blocked),
        )
        connection.executemany(
            "INSERT INTO tag_aliases (canonical_tag, alias) VALUES (?, ?)",
            ((canonical, alias) for canonical, values in aliases.items() for alias in values),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return jsonify({
        "preferred_count": len(preferred),
        "blocked_count": len(blocked),
        "alias_count": sum(len(values) for values in aliases.values()),
    })


def _tag_list(payload, key):
    values = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"{key} must be an array of tags.")
    normalized = sorted({value.strip() for value in values if value.strip()})
    if any(len(value) > 200 for value in normalized):
        raise ValueError("Tags must not exceed 200 characters.")
    return normalized


def _alias_map(payload):
    raw = payload.get("aliases") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        raise ValueError("aliases must be an object.")
    result = {}
    for canonical, values in raw.items():
        if not isinstance(canonical, str) or not canonical.strip():
            raise ValueError("Alias canonical tags must be non-empty strings.")
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError("Each alias group must be an array of tags.")
        result[canonical.strip()] = sorted({value.strip() for value in values if value.strip()})
    return result


def _aliases_payload(rows):
    result = {}
    for row in rows:
        result.setdefault(row["canonical_tag"], []).append(row["alias"])
    return result


def _error(code, message, status):
    return jsonify({"error": {"code": code, "message": message, "details": {}}}), status
