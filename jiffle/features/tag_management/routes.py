from flask import Blueprint, jsonify, request

from jiffle.infrastructure.database.connection import get_database


tag_management_blueprint = Blueprint("tag_management_v1", __name__)


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
