from dataclasses import dataclass
from dataclasses import replace
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from jiffle.features.background_editor.workflow import (
    DEFAULT_BACKGROUND_DEVICE,
    DEFAULT_BACKGROUND_MODEL,
    background_device_value,
    background_model_value,
)


DEFAULT_EXPORT_FORMAT_RULES = (("gif", "mp4"), ("webm", "mp4"))

COLLECTION_EXPORT_MODES = ("download", "folder", "both")
COLLECTION_EXPORT_MODE_DEFAULT = "download"

STORAGE_PATH_ENV_VARS = {
    "media_path": "JIFFLE_MEDIA_PATH",
    "thumbnail_path": "JIFFLE_THUMBNAIL_PATH",
    "import_staging_path": "JIFFLE_IMPORT_STAGING_PATH",
    "export_path": "JIFFLE_EXPORT_PATH",
}


def _is_compatible_saved_path(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    if os.name != "nt" and ("\\" in text or re.match(r"^[A-Za-z]:", text)):
        return False
    return True


def default_storage_paths(fallback_root: Path | None = None) -> dict[str, Path]:
    if fallback_root is None:
        project_root = Path(__file__).resolve().parents[2]
        data_root = Path(
            os.environ.get("DATA_DIR")
            or os.environ.get("JIFFLE_DATA_ROOT", project_root / "jiffle-data")
        ).resolve()
        managed_data = bool(os.environ.get("DATA_DIR") or os.environ.get("JIFFLE_DATA_ROOT"))
        base_root = data_root if managed_data else project_root
    else:
        base_root = fallback_root.resolve()
    defaults = {
        "media_path": base_root / "media",
        "thumbnail_path": base_root / "thumbnails",
        "import_staging_path": base_root / "import-staging",
        "export_path": base_root / "collections",
    }
    return {
        field: Path(os.environ.get(env_var) or default_path).resolve()
        for field, (env_var, default_path) in (
            (key, (STORAGE_PATH_ENV_VARS[key], defaults[key])) for key in defaults
        )
    }


def collection_export_mode_value(value: object) -> str:
    """Return the canonical collection export delivery mode."""
    normalized = str(value if value is not None else "").strip().lower()
    if normalized not in COLLECTION_EXPORT_MODES:
        raise ValueError("collection_export_mode must be download, folder, or both.")
    return normalized


@dataclass(frozen=True)
class Settings:
    database_path: Path
    media_path: Path
    thumbnail_path: Path
    configuration_file_path: Path | None = None
    import_staging_path: Path | None = None
    export_path: Path | None = None
    collection_export_mode: str = COLLECTION_EXPORT_MODE_DEFAULT
    initialize_database: bool = True
    run_jobs_inline: bool = False
    max_items_per_author: int = 5
    max_image_export_size_bytes: int = 50 * 1024 * 1024
    max_video_export_size_bytes: int = 50 * 1024 * 1024
    export_format_rules: tuple[tuple[str, str], ...] = DEFAULT_EXPORT_FORMAT_RULES
    block_previously_deleted: bool = False
    crop_review_enabled: bool = False
    trim_review_enabled: bool = False
    # When enabled, media still waiting in the trim review queue are excluded
    # from automatic collection selection and are never exported.  Manual
    # additions stay possible in the library, but export skips them so an
    # unreviewed original cannot leak into a delivered collection.
    trim_block_unreviewed_export: bool = False
    crop_vision_url: str | None = None
    crop_vision_key: str | None = None
    crop_vision_model: str | None = None
    crop_vision_format: str = "openai"
    crop_min_area_percent: float = 10.0
    crop_padding_percent: float = 2.0
    crop_background_tolerance: int = 14
    crop_selected_analysis: str = "local"
    background_model: str = DEFAULT_BACKGROUND_MODEL
    background_device: str = DEFAULT_BACKGROUND_DEVICE
    huggingface_token: str | None = None
    danbooru_login: str | None = None
    danbooru_api_key: str | None = None
    e621_login: str | None = None
    e621_api_key: str | None = None
    gelbooru_user_id: str | None = None
    gelbooru_api_key: str | None = None
    rule34_user_id: str | None = None
    rule34_api_key: str | None = None
    furaffinity_cookie_a: str | None = None
    furaffinity_cookie_b: str | None = None

    @property
    def resolved_import_staging_path(self) -> Path:
        return self.import_staging_path or self.database_path.parent / "import-staging"

    @property
    def resolved_export_path(self) -> Path:
        return self.export_path or self.database_path.parent / "exports"

    @property
    def configuration_path(self) -> Path:
        return self.configuration_file_path or self.database_path.parent / "settings.json"

    @classmethod
    def from_environment(cls) -> "Settings":
        project_root = Path(__file__).resolve().parents[2]
        data_root = Path(os.environ.get("DATA_DIR") or os.environ.get("JIFFLE_DATA_ROOT", project_root / "jiffle-data")).resolve()
        managed_data = bool(os.environ.get("DATA_DIR") or os.environ.get("JIFFLE_DATA_ROOT"))
        configuration_path = Path(
            os.environ.get("JIFFLE_CONFIG_PATH", (data_root if managed_data else project_root) / "settings.json")
        ).resolve()
        settings = cls(
            database_path=data_root / "jiffle-v2.db",
            media_path=Path(os.environ.get("JIFFLE_MEDIA_PATH", (data_root if managed_data else project_root) / "media")).resolve(),
            thumbnail_path=Path(
                os.environ.get("JIFFLE_THUMBNAIL_PATH", (data_root if managed_data else project_root) / "thumbnails")
            ).resolve(),
            configuration_file_path=configuration_path,
            import_staging_path=Path(os.environ.get("JIFFLE_IMPORT_STAGING_PATH", (data_root if managed_data else project_root) / "import-staging")).resolve(),
            export_path=Path(
                os.environ.get("JIFFLE_EXPORT_PATH", (data_root if managed_data else project_root) / "collections")
            ).resolve(),
            collection_export_mode=collection_export_mode_value(
                os.environ.get("JIFFLE_COLLECTION_EXPORT_MODE", COLLECTION_EXPORT_MODE_DEFAULT)
            ),
            crop_vision_url=os.environ.get("JIFFLE_CROP_VISION_URL"),
            crop_vision_key=os.environ.get("JIFFLE_CROP_VISION_KEY"),
            crop_vision_model=os.environ.get("JIFFLE_CROP_VISION_MODEL"),
            crop_vision_format=os.environ.get("JIFFLE_CROP_VISION_FORMAT", "openai"),
            huggingface_token=os.environ.get("JIFFLE_HUGGINGFACE_TOKEN"),
        )
        legacy_configuration_path = data_root / "settings.json"
        source_path = (
            settings.configuration_path
            if settings.configuration_path.is_file()
            else legacy_configuration_path
        )
        if source_path.is_file():
            payload = json.loads(source_path.read_text(encoding="utf-8"))
            legacy_ai_keys = {
                "ai_api_url", "ai_api_key", "ai_api_model", "ai_api_format",
                "ai_tagging_prompt", "ai_api_prompt",
            }
            allowed = {
                "media_path", "thumbnail_path", "import_staging_path", "export_path",
                "collection_export_mode",
                "max_items_per_author", "max_image_export_size_bytes",
                "max_video_export_size_bytes", "export_format_rules",
                "block_previously_deleted",
                "crop_review_enabled",
                "trim_review_enabled",
                "trim_block_unreviewed_export",
                "crop_vision_url", "crop_vision_key", "crop_vision_model", "crop_vision_format",
                "crop_min_area_percent", "crop_padding_percent", "crop_background_tolerance", "crop_selected_analysis",
                "background_model", "background_device",
                "huggingface_token",
                "danbooru_login", "danbooru_api_key", "e621_login", "e621_api_key",
                "gelbooru_user_id", "gelbooru_api_key",
                "rule34_user_id", "rule34_api_key",
                "furaffinity_cookie_a", "furaffinity_cookie_b",
            }
            values = {key: value for key, value in payload.items() if key in allowed}
            if "collection_export_mode" in values:
                try:
                    values["collection_export_mode"] = collection_export_mode_value(
                        values["collection_export_mode"]
                    )
                except ValueError:
                    # An unknown value written by an older build falls back to
                    # the default instead of blocking startup.
                    values.pop("collection_export_mode")
            if "background_model" in values:
                # Accept model ids and older aliases written by previous
                # versions while keeping the persisted setting canonical.
                values["background_model"] = background_model_value(
                    values["background_model"]
                )
            if "background_device" in values:
                values["background_device"] = background_device_value(
                    values["background_device"]
                )
            if "export_format_rules" in values:
                rules = values["export_format_rules"]
                if isinstance(rules, dict):
                    values["export_format_rules"] = tuple(rules.items())
                elif isinstance(rules, list):
                    values["export_format_rules"] = tuple(tuple(rule) for rule in rules)
            for key, env_var in STORAGE_PATH_ENV_VARS.items():
                if key not in values:
                    continue
                if os.environ.get(env_var) or not _is_compatible_saved_path(values[key]):
                    values.pop(key, None)
                else:
                    values[key] = Path(values[key]).resolve()
            settings = replace(settings, **values)
            if legacy_ai_keys.intersection(payload):
                persist_settings(settings)
        return settings


def persist_settings(settings: Settings) -> None:
    path = settings.configuration_path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}-{uuid4().hex}.tmp")
    payload = {
        "media_path": str(settings.media_path),
        "thumbnail_path": str(settings.thumbnail_path),
        "import_staging_path": str(settings.resolved_import_staging_path),
        "export_path": str(settings.resolved_export_path),
        "collection_export_mode": settings.collection_export_mode,
        "max_items_per_author": settings.max_items_per_author,
        "max_image_export_size_bytes": settings.max_image_export_size_bytes,
        "max_video_export_size_bytes": settings.max_video_export_size_bytes,
        "export_format_rules": dict(settings.export_format_rules),
        "block_previously_deleted": settings.block_previously_deleted,
        "crop_review_enabled": settings.crop_review_enabled,
        "trim_review_enabled": settings.trim_review_enabled,
        "trim_block_unreviewed_export": settings.trim_block_unreviewed_export,
        "crop_vision_url": settings.crop_vision_url,
        "crop_vision_key": settings.crop_vision_key,
        "crop_vision_model": settings.crop_vision_model,
        "crop_vision_format": settings.crop_vision_format,
        "crop_min_area_percent": settings.crop_min_area_percent,
        "crop_padding_percent": settings.crop_padding_percent,
        "crop_background_tolerance": settings.crop_background_tolerance,
        "crop_selected_analysis": settings.crop_selected_analysis,
        "background_model": settings.background_model,
        "background_device": settings.background_device,
        "huggingface_token": settings.huggingface_token,
        "danbooru_login": settings.danbooru_login,
        "danbooru_api_key": settings.danbooru_api_key,
        "e621_login": settings.e621_login,
        "e621_api_key": settings.e621_api_key,
        "gelbooru_user_id": settings.gelbooru_user_id,
        "gelbooru_api_key": settings.gelbooru_api_key,
        "rule34_user_id": settings.rule34_user_id,
        "rule34_api_key": settings.rule34_api_key,
        "furaffinity_cookie_a": settings.furaffinity_cookie_a,
        "furaffinity_cookie_b": settings.furaffinity_cookie_b,
    }
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
