from dataclasses import replace
from pathlib import Path
import platform
import webbrowser
from flask import Blueprint, current_app, jsonify, request
import requests

from jiffle.configuration.settings import Settings, persist_settings
from jiffle.features.ai_tagging.adapters import build_tagging_provider
from jiffle.features.imports.source_adapters.registry import build_source_providers
from jiffle.infrastructure.database.connection import get_database

settings_blueprint = Blueprint("settings_api", __name__)


@settings_blueprint.get("/api/v1/settings")
def get_settings():
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    return jsonify(_public_settings(settings))


@settings_blueprint.patch("/api/v1/settings")
def update_settings():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("settings.invalid_request", "A JSON object is required.", 400)
    allowed = {
        "media_path", "thumbnail_path", "import_staging_path", "export_path",
        "max_items_per_author", "max_export_size_bytes", "block_previously_deleted",
        "ai_api_url",
        "ai_api_key", "ai_api_model", "ai_api_format", "ai_tagging_prompt",
        "danbooru_login", "danbooru_api_key", "e621_login", "e621_api_key",
        "gelbooru_user_id", "gelbooru_api_key", "furaffinity_cookie_a",
        "furaffinity_cookie_b",
    }
    if any(key not in allowed for key in payload):
        return _error("settings.unknown_field", "The request contains an unknown field.", 400)
    current: Settings = current_app.config["JIFFLE_SETTINGS"]
    try:
        updated = _validated_update(current, payload)
        persist_settings(updated)
    except (TypeError, ValueError) as error:
        return _error("settings.invalid_value", str(error), 400)
    except OSError:
        return _error("settings.write_failed", "Settings could not be saved.", 500)
    current_app.config["JIFFLE_SETTINGS"] = updated
    current_app.config["JIFFLE_TAGGING_PROVIDER"] = build_tagging_provider(updated)
    if not current_app.config["JIFFLE_CUSTOM_SOURCE_PROVIDERS"]:
        current_app.config["JIFFLE_SOURCE_PROVIDERS"] = build_source_providers(updated)
    return jsonify(_public_settings(updated))


@settings_blueprint.post("/api/v1/settings/choose-directory")
def choose_directory():
    payload = request.get_json(silent=True) or {}
    initial_path = payload.get("initial_path")
    if initial_path is not None and not isinstance(initial_path, str):
        return _error("settings.invalid_path", "initial_path must be a string.", 400)
    if platform.system() == "Android":
        return _error(
            "settings.directory_picker_unavailable",
            "Android browsers cannot provide an absolute server filesystem path.",
            501,
        )
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        return _error(
            "settings.directory_picker_unavailable",
            "A native directory picker is not available on this system.",
            501,
        )
    root = None
    try:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            parent=root,
            initialdir=initial_path if initial_path and Path(initial_path).is_dir() else None,
            mustexist=True,
        )
    except (tkinter.TclError, OSError):
        return _error(
            "settings.directory_picker_unavailable",
            "A native directory picker is not available on this system.",
            501,
        )
    finally:
        if root is not None:
            root.destroy()
    return jsonify({"path": str(Path(selected).resolve()) if selected else None})


@settings_blueprint.post("/api/v1/settings/ai-provider/test")
def test_ai_provider():
    provider = current_app.config["JIFFLE_TAGGING_PROVIDER"]
    if provider is None:
        return _error("ai.provider_not_configured", "No tagging provider is configured.", 503)
    try:
        provider.check_connection()
    except (requests.RequestException, AttributeError):
        return _error("ai.connection_failed", "The tagging provider could not be reached.", 502)
    return jsonify({"status": "ok", "provider": provider.provider_name})


@settings_blueprint.post("/api/v1/settings/source-providers/<provider_name>/test")
def test_source_provider(provider_name: str):
    provider = next((item for item in current_app.config["JIFFLE_SOURCE_PROVIDERS"] if item.provider_name == provider_name), None)
    if provider is None:
        return _error("providers.not_found", "Source provider was not found.", 404)
    try:
        provider.check_connection()
    except (requests.RequestException, ValueError, AttributeError):
        return _error("providers.connection_failed", "The source provider could not be authenticated.", 502)
    return jsonify({"status": "ok", "provider": provider.provider_name})


@settings_blueprint.post("/api/v1/settings/furaffinity/open-login")
def open_furaffinity_login():
    try:
        opened = webbrowser.open("https://www.furaffinity.net/login/")
    except webbrowser.Error:
        opened = False
    if not opened:
        return _error("providers.browser_failed", "The browser could not be opened.", 500)
    return jsonify({"status": "opened"})


@settings_blueprint.get("/api/v1/history")
def list_history():
    try:
        limit = int(request.args.get("limit", 50))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return _error("history.invalid_query", "Pagination values must be integers.", 400)
    if not 1 <= limit <= 200 or offset < 0:
        return _error("history.invalid_query", "Pagination is outside its valid range.", 400)
    event_type = request.args.get("event_type")
    entity_type = request.args.get("entity_type")
    clauses = []
    parameters = []
    if event_type:
        clauses.append("event_type=?")
        parameters.append(event_type)
    if entity_type:
        clauses.append("entity_type=?")
        parameters.append(entity_type)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    connection = get_database()
    total = connection.execute(
        f"SELECT COUNT(*) FROM operation_history {where}", parameters
    ).fetchone()[0]
    rows = connection.execute(
        f"SELECT * FROM operation_history {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*parameters, limit, offset),
    ).fetchall()
    return jsonify({
        "items": [dict(row) for row in rows],
        "page": {"total": total, "limit": limit, "offset": offset},
    })


@settings_blueprint.post("/api/v1/maintenance/thumbnail-cache/clear")
def clear_thumbnail_cache():
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    root = settings.thumbnail_path.resolve()
    removed = 0
    if root.is_dir():
        for path in root.iterdir():
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                path.unlink()
                removed += 1
    return jsonify({"removed": removed})


def _validated_update(settings, payload):
    values = dict(payload)
    for field in ("media_path", "thumbnail_path", "import_staging_path", "export_path"):
        if field not in values:
            continue
        value = values[field]
        if value is None or (isinstance(value, str) and not value.strip()):
            defaults = {
                "media_path": settings.configuration_path.parent / "media",
                "thumbnail_path": settings.configuration_path.parent / "thumbnails",
                "import_staging_path": settings.configuration_path.parent / "import-staging",
                "export_path": settings.configuration_path.parent / "collections",
            }
            values[field] = defaults[field]
        elif not isinstance(value, str):
            raise ValueError(f"{field} must be a path or empty.")
        else:
            values[field] = Path(value.strip()).expanduser().resolve()
    if "max_items_per_author" in values:
        value = values["max_items_per_author"]
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 1000:
            raise ValueError("max_items_per_author must be an integer from 0 to 1000.")
    if "max_export_size_bytes" in values:
        value = values["max_export_size_bytes"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError("max_export_size_bytes must be a positive integer.")
    if "block_previously_deleted" in values and not isinstance(
        values["block_previously_deleted"], bool
    ):
        raise ValueError("block_previously_deleted must be boolean.")
    if "ai_api_format" in values and values["ai_api_format"] not in {"openai", "gemini"}:
        raise ValueError("ai_api_format must be openai or gemini.")
    for field in (
        "ai_api_url", "ai_api_key", "ai_api_model", "danbooru_login",
        "danbooru_api_key", "e621_login", "e621_api_key", "gelbooru_user_id",
        "gelbooru_api_key", "furaffinity_cookie_a", "furaffinity_cookie_b",
    ):
        if field in values and values[field] is not None and not isinstance(values[field], str):
            raise ValueError(f"{field} must be a string or null.")
    if "ai_tagging_prompt" in values:
        prompt = values["ai_tagging_prompt"]
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 10000:
            raise ValueError("ai_tagging_prompt is invalid.")
    return replace(settings, **values)


def _public_settings(settings):
    return {
        "media_path": str(settings.media_path),
        "thumbnail_path": str(settings.thumbnail_path),
        "import_staging_path": str(settings.resolved_import_staging_path),
        "export_path": str(settings.resolved_export_path),
        "max_items_per_author": settings.max_items_per_author,
        "max_export_size_bytes": settings.max_export_size_bytes,
        "block_previously_deleted": settings.block_previously_deleted,
        "ai_api_url": settings.ai_api_url,
        "ai_api_model": settings.ai_api_model,
        "ai_api_format": settings.ai_api_format,
        "ai_tagging_prompt": settings.ai_tagging_prompt,
        "ai_api_key_configured": bool(settings.ai_api_key),
        "danbooru_login": settings.danbooru_login,
        "danbooru_api_key_configured": bool(settings.danbooru_api_key),
        "e621_login": settings.e621_login,
        "e621_api_key_configured": bool(settings.e621_api_key),
        "gelbooru_user_id": settings.gelbooru_user_id,
        "gelbooru_api_key_configured": bool(settings.gelbooru_api_key),
        "furaffinity_cookie_a_configured": bool(settings.furaffinity_cookie_a),
        "furaffinity_cookie_b_configured": bool(settings.furaffinity_cookie_b),
    }


def _error(code, message, status):
    return jsonify({"error": {"code": code, "message": message, "details": {}}}), status
