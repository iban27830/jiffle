import json
from pathlib import Path
from threading import Thread

from flask import Blueprint, current_app, jsonify, request

from jiffle.configuration.settings import Settings
from jiffle.features.imports.domain import LocalImportCommand
from jiffle.features.imports.local_import import create_local_import_job, run_local_import_job
from jiffle.features.imports.url_import import create_url_import_job, run_url_import_job
from jiffle.features.imports.set_import import (
    ActiveSetImportError,
    active_set_job,
    create_set_import_job,
    request_set_import_cancel,
    run_set_import_job,
)
from jiffle.features.imports.source_adapters.contracts import SourceMedia
from urllib.parse import urlsplit
from jiffle.features.imports.url_normalization import normalize_source_url
from jiffle.infrastructure.database.connection import get_database

imports_blueprint = Blueprint("imports", __name__)

class _DirectMediaProvider:
    def can_handle(self, url):
        return True

    def fetch(self, url):
        suffix = Path(urlsplit(url).path).suffix.lower() or ".jpg"
        return SourceMedia(url, url, "direct", "", None, urlsplit(url).netloc, (), suffix)


@imports_blueprint.post("/api/v1/import-jobs")
def create_import_job():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("import.invalid_request", "A JSON object is required.", 400)
    source_path = payload.get("source_path")
    accept_without_source = payload.get("accept_without_source", False)
    if not isinstance(source_path, str) or not source_path.strip():
        return _error("import.invalid_request", "Source path is required.", 400)
    if not isinstance(accept_without_source, bool):
        return _error(
            "import.invalid_request", "accept_without_source must be boolean.", 400
        )

    command = LocalImportCommand(Path(source_path), accept_without_source)
    job_id = create_local_import_job(get_database(), command)
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    arguments = (settings.database_path, settings, job_id, command)
    if settings.run_jobs_inline:
        run_local_import_job(*arguments)
    else:
        Thread(target=run_local_import_job, args=arguments, daemon=True).start()
    return jsonify({"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}), 202

@imports_blueprint.post("/api/v1/import-uploads")
def create_upload_import_job():
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return _error("import.invalid_request", "A file is required.", 400)
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    settings.resolved_import_staging_path.mkdir(parents=True, exist_ok=True)
    target = settings.resolved_import_staging_path / ("upload-" + Path(uploaded.filename).name)
    uploaded.save(target)
    command = LocalImportCommand(source_path=target, accept_without_source=False)
    job_id = create_local_import_job(get_database(), command)
    arguments = (settings.database_path, settings, job_id, command)
    if settings.run_jobs_inline:
        run_local_import_job(*arguments)
    else:
        Thread(target=run_local_import_job, args=arguments, daemon=True).start()
    return jsonify({"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}), 202


@imports_blueprint.post("/api/v1/url-import-jobs")
def create_url_job():
    payload = request.get_json(silent=True)
    raw_url = payload.get("url") if isinstance(payload, dict) else None
    if not isinstance(raw_url, str):
        return _error("import.invalid_request", "URL is required.", 400)
    try:
        normalized_url = normalize_source_url(raw_url)
    except ValueError as error:
        return _error("import.invalid_source_url", str(error), 400)
    providers = current_app.config["JIFFLE_SOURCE_PROVIDERS"]
    provider = next((item for item in providers if item.can_handle(normalized_url)), None)
    if provider is None:
        provider = _DirectMediaProvider()
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    downloader = current_app.config["JIFFLE_MEDIA_DOWNLOADER"]
    if getattr(provider, "can_handle_set", lambda _url: False)(normalized_url):
        try:
            job_id = create_set_import_job(get_database(), normalized_url, provider)
        except ActiveSetImportError as error:
            return jsonify({
                "error": {
                    "code": "import.set_already_active",
                    "message": "A set import is already running.",
                    "details": {"job_id": error.job_id, "status_url": f"/api/v1/jobs/{error.job_id}"},
                }
            }), 409
        arguments = (
            settings.database_path, settings, job_id, normalized_url, provider, downloader
        )
        if settings.run_jobs_inline:
            run_set_import_job(*arguments)
        else:
            Thread(target=run_set_import_job, args=arguments, daemon=True).start()
        return jsonify({"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}), 202
    job_id = create_url_import_job(get_database(), normalized_url)
    arguments = (
        settings.database_path, settings, job_id, normalized_url, provider, downloader
    )
    if settings.run_jobs_inline:
        run_url_import_job(*arguments)
    else:
        Thread(target=run_url_import_job, args=arguments, daemon=True).start()
    return jsonify({"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}), 202


@imports_blueprint.get("/api/v1/jobs/<int:job_id>")
def get_job(job_id: int):
    row = get_database().execute(
        "SELECT b.*, s.processed AS set_processed, s.total AS set_total, "
        "s.cancel_requested AS set_cancel_requested, s.name AS set_name, s.shortname AS set_shortname, "
        "s.set_id AS set_id, s.provider AS set_provider, s.submitted_url AS set_url "
        "FROM background_jobs b LEFT JOIN source_set_imports s ON s.job_id=b.id WHERE b.id = ?", (job_id,)
    ).fetchone()
    if row is None:
        return _error("jobs.not_found", "Background job was not found.", 404)
    payload = {
        "id": row["id"],
        "type": row["job_type"],
        "status": row["status"],
        "progress": row["progress"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error": ({
            "code": row["error_code"],
            "message": row["error_message"],
            "details": {},
        } if row["error_code"] else None),
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }
    if row["job_type"] == "source_set_import":
        payload.update({
            "processed": row["set_processed"] or 0,
            "total": row["set_total"] or 0,
            "cancel_requested": bool(row["set_cancel_requested"]),
            "set": {
                "id": row["set_id"], "name": row["set_name"],
                "shortname": row["set_shortname"], "provider": row["set_provider"],
                "url": row["set_url"],
            },
        })
    return jsonify(payload)


@imports_blueprint.post("/api/v1/set-import-jobs/<int:job_id>/cancel")
def cancel_set_job(job_id: int):
    if not request_set_import_cancel(get_database(), job_id):
        return _error("jobs.not_found", "Set import was not found.", 404)
    return jsonify({"job_id": job_id, "cancel_requested": True})


@imports_blueprint.get("/api/v1/set-import-jobs/active")
def active_set_import():
    return jsonify({"job": active_set_job(get_database())})


def _error(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message, "details": {}}}), status
