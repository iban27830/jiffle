from dataclasses import replace
from pathlib import Path
import platform
import webbrowser
from flask import Blueprint, current_app, jsonify, request
import requests

from jiffle.configuration.settings import Settings, persist_settings
from jiffle.features.background_editor.runtime import (
    clear_runtime_cache,
    preferred_device_name,
    preferred_model_name,
    validate_huggingface_token,
)
from jiffle.features.background_editor.workflow import (
    BackgroundFailure,
    background_device_value,
    background_model_value,
)
from jiffle.features.imports.source_adapters.registry import build_source_providers
from jiffle.infrastructure.database.connection import get_database
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure

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
        "max_items_per_author", "max_image_export_size_bytes",
        "max_video_export_size_bytes", "export_format_rules", "block_previously_deleted",
        "crop_vision_url", "crop_vision_key", "crop_vision_model", "crop_vision_format",
        "crop_min_area_percent", "crop_padding_percent", "crop_background_tolerance", "crop_selected_analysis",
        "background_model", "background_device",
        "huggingface_token",
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
    if (
        updated.background_model != current.background_model
        or updated.background_device != current.background_device
    ):
        clear_runtime_cache()
    if not current_app.config["JIFFLE_CUSTOM_SOURCE_PROVIDERS"]:
        current_app.config["JIFFLE_SOURCE_PROVIDERS"] = build_source_providers(updated)
    return jsonify(_public_settings(updated))


@settings_blueprint.post("/api/v1/settings/huggingface/test")
def test_huggingface_access():
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return _error("settings.invalid_request", "A JSON object is required.", 400)
    if any(key != "token" for key in payload):
        return _error("settings.unknown_field", "The request contains an unknown field.", 400)

    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    try:
        token = settings.huggingface_token
        if "token" in payload:
            token = _validated_update(
                settings, {"huggingface_token": payload["token"]}
            ).huggingface_token
        account = validate_huggingface_token(
            token,
            preferred_model_name(
                settings.background_model, settings.background_device
            ),
        )
    except (TypeError, ValueError) as error:
        return _error("settings.invalid_value", str(error), 400)
    except BackgroundFailure as error:
        statuses = {
            "background.huggingface_token_required": 400,
            "background.huggingface_token_invalid": 401,
            "background.huggingface_access_denied": 403,
            "background.huggingface_unavailable": 503,
        }
        return _error(error.code, error.message, statuses.get(error.code, 503))
    return jsonify({
        "status": "ok",
        "model": preferred_model_name(
            settings.background_model, settings.background_device
        ),
        "device": preferred_device_name(settings.background_device),
        "account": account,
    })


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


@settings_blueprint.post("/api/v1/settings/source-providers/<provider_name>/test")
def test_source_provider(provider_name: str):
    provider = next((item for item in current_app.config["JIFFLE_SOURCE_PROVIDERS"] if item.provider_name == provider_name), None)
    if provider is None:
        return _error("providers.not_found", "Source provider was not found.", 404)
    try:
        provider.check_connection()
    except SourceProviderFailure as error:
        return _error(error.code, error.message, 502)
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
        if entity_type == "background_job":
            clauses.append(
                "entity_id IN (SELECT id FROM background_jobs WHERE parent_job_id IS NULL)"
            )
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
    for field in ("max_image_export_size_bytes", "max_video_export_size_bytes"):
        if field not in values:
            continue
        value = values[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{field} must be a positive integer.")
    if "export_format_rules" in values:
        rules = values["export_format_rules"]
        if not isinstance(rules, dict):
            raise ValueError("export_format_rules must be an object.")
        allowed_sources = {"jpg", "jpeg", "png", "gif", "webp", "mp4", "webm"}
        allowed_targets = {"jpg", "png", "webp", "mp4"}
        normalized = []
        for source, target in rules.items():
            if not isinstance(source, str) or not isinstance(target, str):
                raise ValueError("Export format rules must contain string formats.")
            source = source.strip().lower().lstrip(".")
            target = target.strip().lower().lstrip(".")
            if source not in allowed_sources or target not in allowed_targets:
                raise ValueError("An export format rule contains an unsupported format.")
            if target == "mp4" and source not in {"gif", "mp4", "webm"}:
                raise ValueError("Only GIF and video formats can be converted to MP4.")
            if source in {"mp4", "webm"} and target != "mp4":
                raise ValueError("Video formats can only be converted to MP4.")
            normalized.append((source, target))
        values["export_format_rules"] = tuple(normalized)
    if "block_previously_deleted" in values and not isinstance(
        values["block_previously_deleted"], bool
    ):
        raise ValueError("block_previously_deleted must be boolean.")
    if "crop_vision_format" in values and values["crop_vision_format"] not in {"openai", "gemini"}:
        raise ValueError("crop_vision_format must be openai or gemini.")
    if "crop_selected_analysis" in values and values["crop_selected_analysis"] not in {"local", "vision"}:
        raise ValueError("crop_selected_analysis must be local or vision.")
    if "background_model" in values:
        values["background_model"] = background_model_value(values["background_model"])
    if "background_device" in values:
        values["background_device"] = background_device_value(values["background_device"])
    if "crop_min_area_percent" in values:
        value=values["crop_min_area_percent"]
        if not isinstance(value,(int,float)) or isinstance(value,bool) or not 1<=value<=50:raise ValueError("crop_min_area_percent must be from 1 to 50.")
    if "crop_padding_percent" in values:
        value=values["crop_padding_percent"]
        if not isinstance(value,(int,float)) or isinstance(value,bool) or not 0<=value<=10:raise ValueError("crop_padding_percent must be from 0 to 10.")
    if "crop_background_tolerance" in values:
        value=values["crop_background_tolerance"]
        if not isinstance(value,int) or isinstance(value,bool) or not 5<=value<=40:raise ValueError("crop_background_tolerance must be from 5 to 40.")
    if "huggingface_token" in values:
        token = values["huggingface_token"]
        if token is None or (isinstance(token, str) and not token.strip()):
            values["huggingface_token"] = None
        elif not isinstance(token, str):
            raise ValueError("huggingface_token must be a string or null.")
        else:
            token = token.strip()
            if len(token) > 512 or any(character.isspace() for character in token):
                raise ValueError("huggingface_token is not a valid access token.")
            values["huggingface_token"] = token
    for field in (
        "crop_vision_url", "crop_vision_key", "crop_vision_model", "danbooru_login",
        "danbooru_api_key", "e621_login", "e621_api_key", "gelbooru_user_id",
        "gelbooru_api_key", "furaffinity_cookie_a", "furaffinity_cookie_b",
    ):
        if field in values and values[field] is not None and not isinstance(values[field], str):
            raise ValueError(f"{field} must be a string or null.")
    return replace(settings, **values)


def _public_settings(settings):
    return {
        "media_path": str(settings.media_path),
        "thumbnail_path": str(settings.thumbnail_path),
        "import_staging_path": str(settings.resolved_import_staging_path),
        "export_path": str(settings.resolved_export_path),
        "max_items_per_author": settings.max_items_per_author,
        "max_image_export_size_bytes": settings.max_image_export_size_bytes,
        "max_video_export_size_bytes": settings.max_video_export_size_bytes,
        "export_format_rules": dict(settings.export_format_rules),
        "block_previously_deleted": settings.block_previously_deleted,
        "crop_vision_url": settings.crop_vision_url,
        "crop_vision_model": settings.crop_vision_model,
        "crop_vision_format": settings.crop_vision_format,
        "crop_vision_key_configured": bool(settings.crop_vision_key),
        "crop_min_area_percent": settings.crop_min_area_percent,
        "crop_padding_percent": settings.crop_padding_percent,
        "crop_background_tolerance": settings.crop_background_tolerance,
        "crop_selected_analysis": settings.crop_selected_analysis,
        "background_model": settings.background_model,
        "background_model_name": preferred_model_name(
            settings.background_model, settings.background_device
        ),
        "background_device": settings.background_device,
        "background_device_name": preferred_device_name(settings.background_device),
        "huggingface_token_configured": bool(settings.huggingface_token),
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
