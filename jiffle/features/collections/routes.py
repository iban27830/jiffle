from threading import Thread

from flask import Blueprint, current_app, jsonify, request
from sqlite3 import IntegrityError
from urllib.parse import urlsplit

from jiffle.configuration.settings import Settings
from jiffle.features.collections.workflow import (
    CollectionFailure, build_collection_preview, create_export_job, normalize_tags,
    preview_export, replace_collection_items, run_export_job,
)
from jiffle.infrastructure.database.connection import get_database

collections_blueprint = Blueprint("collections_v1", __name__)


@collections_blueprint.get("/api/v1/collections")
def list_collections():
    rows = get_database().execute(
        "SELECT collection.*, COUNT(member.media_item_id) AS item_count, "
        "SUM(CASE WHEN limits.max_author > 0 AND authors.author_count > limits.max_author AND authors.limited_author=1 "
        "THEN 1 ELSE 0 END) AS author_warning_count "
        "FROM collections collection LEFT JOIN collection_items member "
        "ON member.collection_id=collection.id "
        "LEFT JOIN (SELECT collection_id, COALESCE(NULLIF(LOWER(TRIM(item.author)),''),'unknown') author, "
        "COUNT(*) author_count, CASE WHEN LOWER(TRIM(COALESCE(item.author,''))) IN "
        "('', 'unknown', 'автор_неизвестен', 'ии_теггер', 'local_ai') THEN 0 ELSE 1 END limited_author "
        "FROM collection_items ci JOIN media_items item ON item.id=ci.media_item_id "
        "GROUP BY collection_id, COALESCE(NULLIF(LOWER(TRIM(item.author)),''),'unknown')) authors "
        "ON authors.collection_id=collection.id AND authors.author=COALESCE(NULLIF(LOWER(TRIM((SELECT author FROM media_items WHERE id=member.media_item_id))),''),'unknown') "
        "CROSS JOIN (SELECT ? max_author) limits "
        "GROUP BY collection.id ORDER BY datetime(collection.created_at) DESC, collection.id DESC",
        (current_app.config["JIFFLE_SETTINGS"].max_items_per_author,),
    ).fetchall()
    connection = get_database()
    items = []
    for row in rows:
        item = dict(row)
        covers = connection.execute(
            "SELECT media_item_id FROM collection_items WHERE collection_id=? ORDER BY position LIMIT 2",
            (row["id"],),
        ).fetchall()
        item["cover_urls"] = [f"/api/v1/media/{cover[0]}/thumbnail" for cover in covers]
        items.append(item)
    return jsonify({"items": items})


@collections_blueprint.post("/api/v1/collections")
def create_collection():
    payload = request.get_json(silent=True)
    name = payload.get("name", "").strip() if isinstance(payload, dict) and isinstance(payload.get("name"), str) else ""
    if not name or len(name) > 120:
        return _error("collections.invalid_name", "Collection name is invalid.", 400)
    media_ids = payload.get("media_item_ids") if isinstance(payload, dict) else None
    included = payload.get("included_tags", []) if isinstance(payload, dict) else []
    excluded = payload.get("excluded_tags", []) if isinstance(payload, dict) else []
    requested_count = payload.get("requested_count") if isinstance(payload, dict) else None
    try:
        included_tags = normalize_tags(included)
        excluded_tags = normalize_tags(excluded)
        if media_ids is not None:
            if not isinstance(media_ids, list) or not all(isinstance(value, int) for value in media_ids):
                raise CollectionFailure("collections.invalid_items", "media_item_ids must be an integer array.")
            if not isinstance(requested_count, int) or requested_count != len(media_ids):
                raise CollectionFailure("collections.invalid_count", "The saved composition must be complete.")
            _validate_composition(get_database(), media_ids, included_tags, excluded_tags)
        connection = get_database()
        cursor = connection.execute(
            "INSERT INTO collections (name, requested_count) VALUES (?, ?)",
            (name, requested_count),
        )
        collection_id = int(cursor.lastrowid)
        connection.executemany(
            "INSERT INTO collection_tags (collection_id, tag, disposition) VALUES (?, ?, ?)",
            ((collection_id, tag, disposition) for disposition, tags in
             (("include", included_tags), ("exclude", excluded_tags)) for tag in tags),
        )
        if media_ids is not None:
            connection.executemany(
                "INSERT INTO collection_items (collection_id, media_item_id, position, added_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                ((collection_id, media_id, position) for position, media_id in enumerate(media_ids)),
            )
        connection.commit()
    except IntegrityError:
        get_database().rollback()
        return _error("collections.name_exists", "Collection name already exists.", 409)
    except CollectionFailure as error:
        get_database().rollback()
        return _collection_error(error)
    return jsonify({"id": collection_id, "name": name}), 201


@collections_blueprint.post("/api/v1/collection-previews")
def create_collection_preview():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("collections.invalid_request", "A JSON object is required.", 400)
    try:
        included = normalize_tags(payload.get("included_tags", []))
        excluded = normalize_tags(payload.get("excluded_tags", []))
        count = payload.get("requested_count")
        excluded_ids = payload.get("excluded_ids", [])
        if not isinstance(count, int) or isinstance(count, bool):
            raise CollectionFailure("collections.invalid_count", "Requested count must be an integer.")
        if not isinstance(excluded_ids, list) or not all(isinstance(value, int) for value in excluded_ids):
            raise CollectionFailure("collections.invalid_items", "excluded_ids must be an integer array.")
        settings = current_app.config["JIFFLE_SETTINGS"]
        preview = build_collection_preview(
            get_database(), included, excluded, count, settings.max_items_per_author,
            tuple(excluded_ids),
        )
    except CollectionFailure as error:
        return _collection_error(error)
    return jsonify({
        "requested_count": preview.requested_count,
        "available_count": preview.available_count,
        "can_save": preview.can_save,
        "max_similarity": preview.max_similarity,
        "most_similar_collection": preview.most_similar_collection,
        "items": [{
            **item.__dict__,
            "content_url": f"/api/v1/media/{item.id}/content",
            "thumbnail_url": f"/api/v1/media/{item.id}/thumbnail",
        } for item in preview.items],
    })


@collections_blueprint.post("/api/v1/collection-previews/replacement")
def replace_collection_preview_item():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("collections.invalid_request", "A JSON object is required.", 400)
    current_ids = payload.get("current_ids", [])
    rejected_ids = payload.get("rejected_ids", [])
    if (not isinstance(current_ids, list) or not all(isinstance(value, int) for value in current_ids)
            or not isinstance(rejected_ids, list) or not all(isinstance(value, int) for value in rejected_ids)):
        return _error("collections.invalid_items", "Replacement identifiers are invalid.", 400)
    try:
        included = normalize_tags(payload.get("included_tags", []))
        excluded = normalize_tags(payload.get("excluded_tags", []))
        settings = current_app.config["JIFFLE_SETTINGS"]
        preview = build_collection_preview(
            get_database(), included, excluded, 1, settings.max_items_per_author,
            tuple(dict.fromkeys((*current_ids, *rejected_ids))),
        )
        if not preview.items:
            raise CollectionFailure("collections.replacement_unavailable", "No replacement is available.")
        item = preview.items[0]
        author_count = 0
        if item.author and item.author.strip().lower() not in {
            "unknown", "автор_неизвестен", "ии_теггер", "local_ai"
        }:
            placeholders = ",".join("?" for _ in current_ids) or "NULL"
            author_count = get_database().execute(
                f"SELECT COUNT(*) FROM media_items WHERE id IN ({placeholders}) AND LOWER(TRIM(author))=?",
                (*current_ids, item.author.strip().lower()),
            ).fetchone()[0]
        warning = bool(settings.max_items_per_author and author_count >= settings.max_items_per_author)
    except CollectionFailure as error:
        return _collection_error(error)
    return jsonify({
        **item.__dict__, "author_limit_exceeded": warning,
        "content_url": f"/api/v1/media/{item.id}/content",
        "thumbnail_url": f"/api/v1/media/{item.id}/thumbnail",
    })


@collections_blueprint.get("/api/v1/collection-presets")
def list_collection_presets():
    connection = get_database()
    rows = connection.execute("SELECT * FROM collection_presets ORDER BY name COLLATE NOCASE").fetchall()
    return jsonify({"items": [_preset_json(connection, row) for row in rows]})


@collections_blueprint.post("/api/v1/collection-presets")
def create_collection_preset():
    return _save_preset(None)


@collections_blueprint.put("/api/v1/collection-presets/<int:preset_id>")
def update_collection_preset(preset_id: int):
    return _save_preset(preset_id)


@collections_blueprint.delete("/api/v1/collection-presets/<int:preset_id>")
def delete_collection_preset(preset_id: int):
    cursor = get_database().execute("DELETE FROM collection_presets WHERE id=?", (preset_id,))
    get_database().commit()
    if cursor.rowcount == 0:
        return _error("collections.preset_not_found", "Preset was not found.", 404)
    return "", 204


@collections_blueprint.get("/api/v1/collections/<int:collection_id>")
def get_collection(collection_id: int):
    connection = get_database()
    collection = connection.execute("SELECT * FROM collections WHERE id=?", (collection_id,)).fetchone()
    if collection is None:
        return _error("collections.not_found", "Collection was not found.", 404)
    rows = connection.execute(
        "SELECT member.position, item.id, item.media_type, item.source_url, item.author, item.domain, "
        "item.width, item.height, item.file_size FROM collection_items member "
        "JOIN media_items item ON item.id=member.media_item_id "
        "WHERE member.collection_id=? ORDER BY member.position", (collection_id,)
    ).fetchall()
    return jsonify({
        "id": collection["id"], "name": collection["name"],
        "jiggie_url": collection["jiggie_url"],
        "created_at": collection["created_at"], "updated_at": collection["updated_at"],
        "items": [{**dict(row),
            "content_url": f"/api/v1/media/{row['id']}/content",
            "thumbnail_url": f"/api/v1/media/{row['id']}/thumbnail",
        } for row in rows],
    })


@collections_blueprint.patch("/api/v1/collections/<int:collection_id>")
def rename_collection(collection_id: int):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not payload:
        return _error("collections.invalid_update", "A non-empty JSON object is required.", 400)
    if any(key not in {"name", "jiggie_url"} for key in payload):
        return _error("collections.invalid_update", "The update contains an unknown field.", 400)
    assignments = []
    parameters = []
    if "name" in payload:
        name = payload["name"].strip() if isinstance(payload["name"], str) else ""
        if not name or len(name) > 120:
            return _error("collections.invalid_name", "Collection name is invalid.", 400)
        assignments.append("name=?")
        parameters.append(name)
    if "jiggie_url" in payload:
        jiggie_url = payload["jiggie_url"].strip() if isinstance(payload["jiggie_url"], str) else ""
        if jiggie_url:
            parsed = urlsplit(jiggie_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or len(jiggie_url) > 2000:
                return _error("collections.invalid_jiggie_url", "Jiggie URL is invalid.", 400)
        assignments.append("jiggie_url=?")
        parameters.append(jiggie_url or None)
    try:
        cursor = get_database().execute(
            f"UPDATE collections SET {', '.join(assignments)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (*parameters, collection_id),
        )
        get_database().commit()
    except IntegrityError:
        return _error("collections.name_exists", "Collection name already exists.", 409)
    if cursor.rowcount == 0:
        return _error("collections.not_found", "Collection was not found.", 404)
    collection = get_database().execute(
        "SELECT id, name, jiggie_url, created_at, updated_at FROM collections WHERE id=?",
        (collection_id,),
    ).fetchone()
    return jsonify(dict(collection))


@collections_blueprint.delete("/api/v1/collections/<int:collection_id>")
def delete_collection(collection_id: int):
    cursor = get_database().execute("DELETE FROM collections WHERE id=?", (collection_id,))
    get_database().commit()
    if cursor.rowcount == 0:
        return _error("collections.not_found", "Collection was not found.", 404)
    return "", 204


@collections_blueprint.put("/api/v1/collections/<int:collection_id>/items")
def set_collection_items(collection_id: int):
    payload = request.get_json(silent=True)
    media_ids = payload.get("media_item_ids") if isinstance(payload, dict) else None
    if not isinstance(media_ids, list) or not all(isinstance(value, int) for value in media_ids):
        return _error("collections.invalid_items", "media_item_ids must be an integer array.", 400)
    try:
        replace_collection_items(get_database(), collection_id, media_ids)
    except CollectionFailure as error:
        return _collection_error(error)
    return jsonify({"collection_id": collection_id, "item_count": len(media_ids)})


@collections_blueprint.get("/api/v1/collections/<int:collection_id>/export-preview")
def get_export_preview(collection_id: int):
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    try:
        preview = preview_export(get_database(), settings, collection_id)
    except CollectionFailure as error:
        return _collection_error(error)
    return jsonify({
        "item_count": preview.item_count, "total_size": preview.total_size,
        "author_counts": preview.author_counts, "violations": list(preview.violations),
        "warnings": list(preview.warnings),
        "can_export": not preview.violations,
    })


@collections_blueprint.post("/api/v1/collections/<int:collection_id>/export-jobs")
def start_export(collection_id: int):
    connection = get_database()
    try:
        job_id = create_export_job(connection, collection_id)
    except CollectionFailure as error:
        return _collection_error(error)
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    arguments = (settings.database_path, settings, job_id, collection_id)
    if settings.run_jobs_inline:
        run_export_job(*arguments)
    else:
        Thread(target=run_export_job, args=arguments, daemon=True).start()
    return jsonify({"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}), 202


@collections_blueprint.get("/api/v1/export-runs")
def list_export_runs():
    rows = get_database().execute(
        "SELECT run.* FROM export_runs run ORDER BY run.id DESC"
    ).fetchall()
    return jsonify({"items": [dict(row) for row in rows]})


@collections_blueprint.get("/api/v1/archived-exports")
def list_archived_exports():
    rows = get_database().execute(
        "SELECT archive.*, COUNT(item.media_item_id) AS mapped_item_count "
        "FROM archived_exports archive LEFT JOIN archived_export_items item "
        "ON item.export_id=archive.id GROUP BY archive.id "
        "ORDER BY COALESCE(archive.exported_at, archive.created_at) DESC, archive.id DESC"
    ).fetchall()
    return jsonify({"items": [dict(row) for row in rows]})


@collections_blueprint.get("/api/v1/archived-exports/<int:export_id>")
def get_archived_export(export_id: int):
    connection = get_database()
    archive = connection.execute(
        "SELECT * FROM archived_exports WHERE id=?", (export_id,)
    ).fetchone()
    if archive is None:
        return _error("exports.archive_not_found", "Archived export was not found.", 404)
    rows = connection.execute(
        "SELECT member.position, media.id, media.media_type, media.source_url, media.author, media.domain, "
        "media.width, media.height, media.file_size "
        "FROM archived_export_items member JOIN media_items media "
        "ON media.id=member.media_item_id WHERE member.export_id=? ORDER BY member.position",
        (export_id,),
    ).fetchall()
    return jsonify({
        **dict(archive),
        "items": [{
            **dict(row),
            "content_url": f"/api/v1/media/{row['id']}/content",
            "thumbnail_url": f"/api/v1/media/{row['id']}/thumbnail",
        } for row in rows],
    })


def _collection_error(error):
    if error.code in {"collections.not_found", "collections.preset_not_found"}:
        status = 404
    elif error.code == "collections.duplicate_item":
        status = 409
    else:
        status = 400
    return _error(error.code, error.message, status)


def _validate_composition(connection, media_ids, included_tags, excluded_tags):
    if len(media_ids) != len(set(media_ids)):
        raise CollectionFailure("collections.duplicate_item", "A collection cannot contain an item twice.")
    for media_id in media_ids:
        row = connection.execute(
            "SELECT id FROM media_items WHERE id=? AND deleted_at IS NULL", (media_id,)
        ).fetchone()
        if row is None:
            raise CollectionFailure("collections.media_not_found", "One or more media items were not found.")
        tags = {
            value[0] for value in connection.execute(
                "SELECT tag FROM media_tags WHERE media_item_id=?", (media_id,)
            )
        }
        if not set(included_tags).issubset(tags) or set(excluded_tags) & tags:
            raise CollectionFailure(
                "collections.composition_stale", "One or more items no longer match the collection tags."
            )


def _preset_json(connection, row):
    tags = connection.execute(
        "SELECT tag, disposition FROM collection_preset_tags WHERE preset_id=? "
        "ORDER BY disposition, tag", (row["id"],)
    ).fetchall()
    return {
        "id": row["id"], "name": row["name"],
        "requested_count": row["requested_count"],
        "included_tags": [tag["tag"] for tag in tags if tag["disposition"] == "include"],
        "excluded_tags": [tag["tag"] for tag in tags if tag["disposition"] == "exclude"],
    }


def _save_preset(preset_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("collections.invalid_request", "A JSON object is required.", 400)
    name = payload.get("name", "").strip() if isinstance(payload.get("name"), str) else ""
    count = payload.get("requested_count")
    try:
        included = normalize_tags(payload.get("included_tags", []))
        excluded = normalize_tags(payload.get("excluded_tags", []))
        if not name or len(name) > 120:
            raise CollectionFailure("collections.invalid_name", "Preset name is invalid.")
        if not included:
            raise CollectionFailure("collections.tags_required", "At least one included tag is required.")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 1000:
            raise CollectionFailure("collections.invalid_count", "Requested count must be from 1 to 1000.")
        connection = get_database()
        if preset_id is None:
            cursor = connection.execute(
                "INSERT INTO collection_presets (name, requested_count) VALUES (?, ?)", (name, count)
            )
            preset_id = int(cursor.lastrowid)
        else:
            cursor = connection.execute(
                "UPDATE collection_presets SET name=?, requested_count=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (name, count, preset_id),
            )
            if cursor.rowcount == 0:
                raise CollectionFailure("collections.preset_not_found", "Preset was not found.")
            connection.execute("DELETE FROM collection_preset_tags WHERE preset_id=?", (preset_id,))
        connection.executemany(
            "INSERT INTO collection_preset_tags (preset_id, tag, disposition) VALUES (?, ?, ?)",
            ((preset_id, tag, disposition) for disposition, values in
             (("include", included), ("exclude", excluded)) for tag in values),
        )
        connection.commit()
    except IntegrityError:
        get_database().rollback()
        return _error("collections.preset_name_exists", "Preset name already exists.", 409)
    except CollectionFailure as error:
        get_database().rollback()
        return _collection_error(error)
    row = get_database().execute("SELECT * FROM collection_presets WHERE id=?", (preset_id,)).fetchone()
    return jsonify(_preset_json(get_database(), row)), 201 if request.method == "POST" else 200


def _error(code, message, status):
    return jsonify({"error": {"code": code, "message": message, "details": {}}}), status
