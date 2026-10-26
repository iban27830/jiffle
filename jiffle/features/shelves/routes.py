from flask import Blueprint, jsonify, request

from jiffle.features.shelves.workflow import (
    PRESET_SHELF_COLORS,
    ShelfFailure,
    bulk_update_media_shelves,
    create_shelf,
    delete_shelf,
    get_shelves_for_media_ids,
    list_shelves,
    parse_shelf_ids,
    update_shelf,
)
from jiffle.infrastructure.database.connection import get_database

shelves_blueprint = Blueprint("shelves", __name__)


def _error_response(error: ShelfFailure):
    return (
        jsonify(
            {
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "details": {},
                }
            }
        ),
        error.status_code,
    )


@shelves_blueprint.get("/api/v1/shelves")
def get_shelves_route():
    connection = get_database()
    return jsonify(
        {
            "items": list_shelves(connection),
            "palette": list(PRESET_SHELF_COLORS),
        }
    )


@shelves_blueprint.post("/api/v1/shelves")
def create_shelf_route():
    payload = request.get_json(silent=True) or {}
    connection = get_database()
    try:
        shelf, created = create_shelf(
            connection,
            name=payload.get("name"),
            color=payload.get("color"),
        )
    except ShelfFailure as error:
        return _error_response(error)
    return jsonify({"shelf": shelf, "created": created}), (201 if created else 200)


@shelves_blueprint.patch("/api/v1/shelves/<int:shelf_id>")
def update_shelf_route(shelf_id: int):
    payload = request.get_json(silent=True) or {}
    connection = get_database()
    try:
        shelf = update_shelf(
            connection,
            shelf_id,
            name=payload.get("name") if "name" in payload else None,
            color=payload.get("color") if "color" in payload else None,
            sort_order=payload.get("sort_order") if "sort_order" in payload else None,
        )
    except ShelfFailure as error:
        return _error_response(error)
    return jsonify({"shelf": shelf})


@shelves_blueprint.delete("/api/v1/shelves/<int:shelf_id>")
def delete_shelf_route(shelf_id: int):
    connection = get_database()
    try:
        delete_shelf(connection, shelf_id)
    except ShelfFailure as error:
        return _error_response(error)
    return jsonify({"deleted": True, "id": shelf_id})


@shelves_blueprint.post("/api/v1/shelves/items")
def bulk_shelf_items_route():
    payload = request.get_json(silent=True) or {}
    raw_media_ids = payload.get("media_ids")
    if not isinstance(raw_media_ids, list):
        return _error_response(ShelfFailure("shelves.invalid_media_ids", "media_ids must be an array."))
    media_ids = [
        int(mid)
        for mid in raw_media_ids
        if (isinstance(mid, int) and not isinstance(mid, bool))
        or (isinstance(mid, str) and mid.strip().isdigit())
    ]
    raw_shelves = payload.get("shelf_ids") if "shelf_ids" in payload else payload.get("shelf_id")
    shelf_ids = parse_shelf_ids(raw_shelves)
    action = str(payload.get("action") or "add").strip().lower()
    connection = get_database()
    try:
        updated = bulk_update_media_shelves(connection, media_ids, shelf_ids, action=action)
    except ShelfFailure as error:
        return _error_response(error)
    media_shelves = get_shelves_for_media_ids(connection, media_ids)
    return jsonify(
        {
            "updated": updated,
            "action": action,
            "shelves": list_shelves(connection),
            "media_shelves": {str(mid): list(media_shelves.get(mid, ())) for mid in media_ids},
        }
    )
