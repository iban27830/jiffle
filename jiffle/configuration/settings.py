from dataclasses import dataclass
from dataclasses import replace
import json
import os
from pathlib import Path


DEFAULT_EXPORT_FORMAT_RULES = (("gif", "mp4"), ("webm", "mp4"))


@dataclass(frozen=True)
class Settings:
    database_path: Path
    media_path: Path
    thumbnail_path: Path
    configuration_file_path: Path | None = None
    import_staging_path: Path | None = None
    export_path: Path | None = None
    initialize_database: bool = True
    run_jobs_inline: bool = False
    max_items_per_author: int = 5
    max_image_export_size_bytes: int = 50 * 1024 * 1024
    max_video_export_size_bytes: int = 50 * 1024 * 1024
    export_format_rules: tuple[tuple[str, str], ...] = DEFAULT_EXPORT_FORMAT_RULES
    block_previously_deleted: bool = False
    crop_vision_url: str | None = None
    crop_vision_key: str | None = None
    crop_vision_model: str | None = None
    crop_vision_format: str = "openai"
    crop_min_area_percent: float = 10.0
    crop_padding_percent: float = 2.0
    crop_background_tolerance: int = 14
    crop_selected_analysis: str = "local"
    danbooru_login: str | None = None
    danbooru_api_key: str | None = None
    e621_login: str | None = None
    e621_api_key: str | None = None
    gelbooru_user_id: str | None = None
    gelbooru_api_key: str | None = None
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
        data_root = Path(os.environ.get("JIFFLE_DATA_ROOT", project_root / "jiffle-data")).resolve()
        configuration_path = Path(
            os.environ.get("JIFFLE_CONFIG_PATH", project_root / "settings.json")
        ).resolve()
        settings = cls(
            database_path=data_root / "jiffle-v2.db",
            media_path=Path(os.environ.get("JIFFLE_MEDIA_PATH", project_root / "media")).resolve(),
            thumbnail_path=Path(
                os.environ.get("JIFFLE_THUMBNAIL_PATH", project_root / "thumbnails")
            ).resolve(),
            configuration_file_path=configuration_path,
            import_staging_path=project_root / "import-staging",
            export_path=Path(
                os.environ.get("JIFFLE_EXPORT_PATH", project_root / "collections")
            ).resolve(),
            crop_vision_url=os.environ.get("JIFFLE_CROP_VISION_URL"),
            crop_vision_key=os.environ.get("JIFFLE_CROP_VISION_KEY"),
            crop_vision_model=os.environ.get("JIFFLE_CROP_VISION_MODEL"),
            crop_vision_format=os.environ.get("JIFFLE_CROP_VISION_FORMAT", "openai"),
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
                "max_items_per_author", "max_image_export_size_bytes",
                "max_video_export_size_bytes", "export_format_rules",
                "block_previously_deleted",
                "crop_vision_url", "crop_vision_key", "crop_vision_model", "crop_vision_format",
                "crop_min_area_percent", "crop_padding_percent", "crop_background_tolerance", "crop_selected_analysis",
                "danbooru_login", "danbooru_api_key", "e621_login", "e621_api_key",
                "gelbooru_user_id", "gelbooru_api_key", "furaffinity_cookie_a",
                "furaffinity_cookie_b",
            }
            values = {key: value for key, value in payload.items() if key in allowed}
            if "export_format_rules" in values:
                rules = values["export_format_rules"]
                if isinstance(rules, dict):
                    values["export_format_rules"] = tuple(rules.items())
                elif isinstance(rules, list):
                    values["export_format_rules"] = tuple(tuple(rule) for rule in rules)
            for key in ("media_path", "thumbnail_path", "import_staging_path", "export_path"):
                if key in values and values[key]:
                    values[key] = Path(values[key]).resolve()
            settings = replace(settings, **values)
            if legacy_ai_keys.intersection(payload):
                persist_settings(settings)
        return settings


def persist_settings(settings: Settings) -> None:
    path = settings.configuration_path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    payload = {
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
        "crop_vision_key": settings.crop_vision_key,
        "crop_vision_model": settings.crop_vision_model,
        "crop_vision_format": settings.crop_vision_format,
        "crop_min_area_percent": settings.crop_min_area_percent,
        "crop_padding_percent": settings.crop_padding_percent,
        "crop_background_tolerance": settings.crop_background_tolerance,
        "crop_selected_analysis": settings.crop_selected_analysis,
        "danbooru_login": settings.danbooru_login,
        "danbooru_api_key": settings.danbooru_api_key,
        "e621_login": settings.e621_login,
        "e621_api_key": settings.e621_api_key,
        "gelbooru_user_id": settings.gelbooru_user_id,
        "gelbooru_api_key": settings.gelbooru_api_key,
        "furaffinity_cookie_a": settings.furaffinity_cookie_a,
        "furaffinity_cookie_b": settings.furaffinity_cookie_b,
    }
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
