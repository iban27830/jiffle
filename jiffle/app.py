from flask import Flask, jsonify, send_from_directory
from werkzeug.exceptions import HTTPException

from jiffle.configuration.settings import Settings
from jiffle.features.health.routes import health_blueprint
from jiffle.features.duplicates.routes import duplicates_blueprint
from jiffle.features.imports.routes import imports_blueprint
from jiffle.features.imports.source_adapters.http_download import RequestsMediaDownloader
from jiffle.features.imports.source_adapters.registry import build_source_providers
from jiffle.features.library.routes import library_blueprint
from jiffle.features.review_queue.routes import review_blueprint
from jiffle.features.collections.routes import collections_blueprint
from jiffle.features.settings_api.routes import settings_blueprint
from jiffle.features.tag_management.routes import tag_management_blueprint
from jiffle.features.crop_editor.routes import crop_blueprint, resume_crop_scans
from jiffle.infrastructure.database.connection import close_database
from jiffle.infrastructure.database.migrations import migrate_database


def create_app(
    settings: Settings | None = None,
    source_providers: tuple[object, ...] | None = None,
    media_downloader: object | None = None,
) -> Flask:
    resolved_settings = settings or Settings.from_environment()
    app = Flask(__name__, static_folder="frontend", static_url_path="/app-assets")
    app.config["JIFFLE_SETTINGS"] = resolved_settings
    app.config["JIFFLE_SOURCE_PROVIDERS"] = (
        source_providers if source_providers is not None else build_source_providers(resolved_settings)
    )
    app.config["JIFFLE_CUSTOM_SOURCE_PROVIDERS"] = source_providers is not None
    app.config["JIFFLE_MEDIA_DOWNLOADER"] = (
        media_downloader if media_downloader is not None else RequestsMediaDownloader()
    )
    app.teardown_appcontext(close_database)
    app.register_blueprint(health_blueprint)
    app.register_blueprint(library_blueprint)
    app.register_blueprint(imports_blueprint)
    app.register_blueprint(review_blueprint)
    app.register_blueprint(duplicates_blueprint)
    app.register_blueprint(collections_blueprint)
    app.register_blueprint(settings_blueprint)
    app.register_blueprint(tag_management_blueprint)
    app.register_blueprint(crop_blueprint)
    register_error_handlers(app)

    @app.get("/")
    def frontend_index():
        return send_from_directory(app.static_folder, "index.html")

    if resolved_settings.initialize_database:
        resolved_settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        with app.app_context():
            migrate_database()
        if not resolved_settings.run_jobs_inline:
            resume_crop_scans(resolved_settings.database_path, resolved_settings)

    return app


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        return jsonify({"error": {
            "code": f"http.{error.code}",
            "message": error.description,
            "details": {},
        }}), error.code

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        app.logger.exception("Unhandled request error", exc_info=error)
        return jsonify({"error": {
            "code": "internal.unexpected_error",
            "message": "An unexpected error occurred.",
            "details": {},
        }}), 500
