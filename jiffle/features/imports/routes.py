import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import md5, sha256
from pathlib import Path
from threading import Thread

from flask import Blueprint, current_app, jsonify, request

from jiffle.configuration.settings import Settings
from jiffle.features.imports.domain import LocalImportCommand
from jiffle.features.imports.implicit_tags import (
    create_implicit_tag_job,
    run_implicit_tag_job,
)
from jiffle.features.imports.local_import import create_local_import_job, run_local_import_job
from jiffle.features.imports.url_import import create_url_import_job, run_url_import_job
from jiffle.features.imports.universal_import import (
    _filename_hash_hints,
    _provider_needs_configuration,
    create_universal_import_job,
    run_universal_import_job,
)
from jiffle.features.imports.set_import import (
    ActiveSetImportError,
    active_set_job,
    create_set_import_job,
    request_set_import_cancel,
    run_set_import_job,
)
from jiffle.features.imports.source_adapters.contracts import SourceMedia
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from urllib.parse import urlsplit
from uuid import uuid4
from jiffle.features.imports.url_normalization import normalize_source_url
from jiffle.features.shelves.workflow import parse_shelf_ids
from jiffle.infrastructure.database.connection import get_database

imports_blueprint = Blueprint("imports", __name__)

_IMPORT_RESOLVE_EXECUTOR = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="jiffle-import-resolve"
)


def _submit_universal_import_job(settings: Settings, arguments: tuple) -> None:
    if settings.run_jobs_inline:
        run_universal_import_job(*arguments)
    else:
        _IMPORT_RESOLVE_EXECUTOR.submit(run_universal_import_job, *arguments)


@imports_blueprint.post("/api/v1/import-resolve-jobs")
def create_import_resolve_job():
    """Accept one or more file uploads or URLs and resolve them through the import workflow."""
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    uploaded_files = [
        *request.files.getlist("file"),
        *request.files.getlist("files"),
    ]
    inputs: list[tuple[str, str, str | None]] = []
    shelf_ids: tuple[int, ...] = ()
    if uploaded_files:
        valid_files = [item for item in uploaded_files if item and item.filename]
        if not valid_files:
            return _error("import.invalid_request", "A file is required.", 400)
        raw_form_shelves = (
            request.form.getlist("shelf_ids")
            or request.form.get("shelf_ids")
            or request.form.get("shelf_id")
        )
        shelf_ids = parse_shelf_ids(raw_form_shelves)
        settings.resolved_import_staging_path.mkdir(parents=True, exist_ok=True)
        for uploaded in valid_files:
            raw_original_name = Path(uploaded.filename or "upload").name
            target = _staged_upload_target(
                settings.resolved_import_staging_path, uploaded.filename
            )
            uploaded.save(target)
            inputs.append((str(target), "file", raw_original_name))
    else:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("import.invalid_request", "A file or URL is required.", 400)
        raw_payload_shelves = (
            payload.get("shelf_ids") if "shelf_ids" in payload else payload.get("shelf_id")
        )
        shelf_ids = parse_shelf_ids(raw_payload_shelves)
        raw_urls_field = payload.get("urls")
        if isinstance(raw_urls_field, list):
            if not raw_urls_field or any(not isinstance(item, str) for item in raw_urls_field):
                return _error("import.invalid_request", "A file or URL is required.", 400)
            raw_urls = [item.strip() for item in raw_urls_field if item.strip()]
        else:
            raw_url = payload.get("url")
            if not isinstance(raw_url, str) or not raw_url.strip():
                return _error("import.invalid_request", "A file or URL is required.", 400)
            raw_urls = [line.strip() for line in raw_url.splitlines() if line.strip()]
        if not raw_urls:
            return _error("import.invalid_request", "A file or URL is required.", 400)
        for raw_item in raw_urls:
            try:
                normalized = normalize_source_url(raw_item)
            except ValueError as error:
                return _error("import.invalid_source_url", str(error), 400)
            inputs.append((normalized, "url", None))

    connection = get_database()
    providers = current_app.config["JIFFLE_SOURCE_PROVIDERS"]
    downloader = current_app.config["JIFFLE_MEDIA_DOWNLOADER"]
    job_ids: list[int] = []
    pending_universal_args: list[tuple] = []
    for submitted_input, input_kind, original_name in inputs:
        if input_kind == "url":
            provider = next(
                (item for item in providers if item.can_handle(submitted_input)),
                None,
            )
            if provider is not None and getattr(
                provider, "can_handle_set", lambda _url: False
            )(submitted_input):
                if pending_universal_args:
                    connection.commit()
                    for args in pending_universal_args:
                        _submit_universal_import_job(settings, args)
                    pending_universal_args.clear()
                try:
                    job_id = create_set_import_job(
                        connection, submitted_input, provider, shelf_ids=shelf_ids
                    )
                except ActiveSetImportError as error:
                    return jsonify({
                        "error": {
                            "code": "import.set_already_active",
                            "message": "A set import is already running.",
                            "details": {"job_id": error.job_id},
                        }
                    }), 409
                arguments = (
                    settings.database_path,
                    settings,
                    job_id,
                    submitted_input,
                    provider,
                    downloader,
                    providers,
                )
                if settings.run_jobs_inline:
                    run_set_import_job(*arguments)
                else:
                    Thread(target=run_set_import_job, args=arguments, daemon=True).start()
                job_ids.append(job_id)
                continue
        job_id = create_universal_import_job(
            connection,
            submitted_input,
            input_kind,
            original_name=original_name,
            shelf_ids=shelf_ids,
            commit=False,
        )
        arguments = (
            settings.database_path,
            settings,
            job_id,
            submitted_input,
            input_kind,
            providers,
            downloader,
        )
        pending_universal_args.append(arguments)
        job_ids.append(job_id)

    if pending_universal_args:
        connection.commit()
        for arguments in pending_universal_args:
            _submit_universal_import_job(settings, arguments)

    first_job_id = job_ids[0]
    return jsonify({
        "job_id": first_job_id,
        "job_ids": job_ids,
        "status_url": f"/api/v1/jobs/{first_job_id}",
        "status_urls": [f"/api/v1/jobs/{jid}" for jid in job_ids],
    }), 202

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

    shelf_ids = parse_shelf_ids(
        payload.get("shelf_ids") if "shelf_ids" in payload else payload.get("shelf_id")
    )
    command = LocalImportCommand(Path(source_path), accept_without_source, shelf_ids=shelf_ids)
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
    shelf_ids = parse_shelf_ids(
        request.form.getlist("shelf_ids")
        or request.form.get("shelf_ids")
        or request.form.get("shelf_id")
    )
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    settings.resolved_import_staging_path.mkdir(parents=True, exist_ok=True)
    target = _staged_upload_target(
        settings.resolved_import_staging_path, uploaded.filename
    )
    uploaded.save(target)
    command = LocalImportCommand(source_path=target, accept_without_source=False, shelf_ids=shelf_ids)
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
            settings.database_path, settings, job_id, normalized_url, provider, downloader,
            providers,
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


@imports_blueprint.post("/api/v1/source-search")
def search_sources():
    """Find exact copies by file hash without creating an import job.

    Most providers index by MD5; Pawchive stores files by SHA-256.  An uploaded
    file is fingerprinted both ways so each provider receives the hash it uses,
    while a source URL can still only supply an MD5 (a few boorus publish one).
    """
    uploaded = request.files.get("file")
    payload = request.get_json(silent=True) if uploaded is None else None
    source_url = payload.get("url") if isinstance(payload, dict) else None
    digest = None
    sha256_digest = None

    if uploaded is not None:
        if not uploaded.filename:
            return _error("import.invalid_request", "A file is required.", 400)
        md5_hasher = md5()
        sha256_hasher = sha256()
        while True:
            chunk = uploaded.stream.read(64 * 1024)
            if not chunk:
                break
            md5_hasher.update(chunk)
            sha256_hasher.update(chunk)
        digest = md5_hasher.hexdigest()
        sha256_digest = sha256_hasher.hexdigest()
    elif isinstance(source_url, str) and source_url.strip():
        try:
            normalized_url = normalize_source_url(source_url)
        except ValueError as error:
            return _error("import.invalid_source_url", str(error), 400)
        providers = current_app.config["JIFFLE_SOURCE_PROVIDERS"]
        provider = next(
            (item for item in providers if item.can_handle(normalized_url)), None
        )
        metadata_md5 = getattr(provider, "metadata_md5", None)
        if provider is None or not callable(metadata_md5):
            return _error(
                "import.source_search_unsupported",
                "This URL cannot provide an image hash for source search.",
                422,
            )
        try:
            digest = metadata_md5(normalized_url)
        except SourceProviderFailure as error:
            return _error(error.code, error.message, 422)
    else:
        return _error(
            "import.invalid_request", "An image file or supported source URL is required.", 400
        )

    if not digest and not sha256_digest:
        return jsonify({"md5": None, "sha256": None, "matches": [], "errors": []})
    providers = current_app.config["JIFFLE_SOURCE_PROVIDERS"]
    matches: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []

    def _run_lookup_pass(pass_md5: str | None, pass_sha256: str | None) -> list[dict[str, object]]:
        searchable = []
        for provider in providers:
            if not callable(getattr(provider, "search_by_md5", None)):
                continue
            if _provider_needs_configuration(provider):
                continue
            algorithm = str(getattr(provider, "hash_algorithm", "md5") or "md5").strip().lower()
            provider_digest = pass_sha256 if algorithm == "sha256" else pass_md5
            if provider_digest:
                searchable.append((provider, provider_digest))
        if not searchable:
            return []

        def lookup(provider, provider_digest):
            return provider, provider.search_by_md5(provider_digest)

        pass_matches: list[dict[str, object]] = []
        with ThreadPoolExecutor(max_workers=max(1, len(searchable))) as executor:
            futures = {
                executor.submit(lookup, provider, provider_digest): provider
                for provider, provider_digest in searchable
            }
            for future in as_completed(futures):
                provider = futures[future]
                try:
                    _, provider_matches = future.result()
                    pass_matches.extend(provider_matches or [])
                except SourceProviderFailure as error:
                    errors.append({
                        "provider": getattr(provider, "provider_name", "unknown"),
                        "code": error.code,
                        "message": error.message,
                    })
                except Exception:
                    errors.append({
                        "provider": getattr(provider, "provider_name", "unknown"),
                        "code": "import.source_search_failed",
                        "message": "The source search failed.",
                    })
        return pass_matches

    matches.extend(_run_lookup_pass(digest, sha256_digest))
    if not matches and uploaded is not None and uploaded.filename:
        hint_md5, hint_sha256 = _filename_hash_hints(uploaded.filename)
        query_md5 = hint_md5 if (hint_md5 and hint_md5 != digest) else None
        query_sha256 = (
            hint_sha256 if (hint_sha256 and hint_sha256 != sha256_digest) else None
        )
        if query_md5 or query_sha256:
            matches.extend(_run_lookup_pass(query_md5, query_sha256))

    unique: dict[tuple[str, str], dict[str, object]] = {}
    for match in matches:
        provider = str(match.get("provider") or "unknown")
        remote_id = str(match.get("remote_id") or match.get("canonical_url") or "")
        if remote_id:
            unique[(provider, remote_id)] = match
    return jsonify({
        "md5": digest,
        "sha256": sha256_digest,
        "matches": list(unique.values()),
        "errors": errors,
    })


def _serialize_job_row(row) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": row["id"],
        "type": row["job_type"],
        "status": row["status"],
        "progress": row["progress"],
        "message": row["status_message"],
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
    return payload


@imports_blueprint.get("/api/v1/jobs")
def get_jobs_batch():
    raw_ids = request.args.get("ids") or ""
    job_ids: list[int] = []
    for part in raw_ids.split(","):
        part = part.strip()
        if part.isdigit():
            jid = int(part)
            if jid > 0 and jid not in job_ids:
                job_ids.append(jid)
    if not job_ids:
        return jsonify({"items": []})
    job_ids = job_ids[:250]
    placeholders = ",".join("?" for _ in job_ids)
    rows = get_database().execute(
        "SELECT b.*, s.processed AS set_processed, s.total AS set_total, "
        "s.cancel_requested AS set_cancel_requested, s.name AS set_name, s.shortname AS set_shortname, "
        "s.set_id AS set_id, s.provider AS set_provider, s.submitted_url AS set_url "
        f"FROM background_jobs b LEFT JOIN source_set_imports s ON s.job_id=b.id WHERE b.id IN ({placeholders})",
        job_ids,
    ).fetchall()
    by_id = {int(row["id"]): _serialize_job_row(row) for row in rows}
    return jsonify({"items": [by_id[jid] for jid in job_ids if jid in by_id]})


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
    return jsonify(_serialize_job_row(row))


@imports_blueprint.post("/api/v1/set-import-jobs/<int:job_id>/cancel")
def cancel_set_job(job_id: int):
    if not request_set_import_cancel(get_database(), job_id):
        return _error("jobs.not_found", "Set import was not found.", 404)
    return jsonify({"job_id": job_id, "cancel_requested": True})


@imports_blueprint.get("/api/v1/set-import-jobs/active")
def active_set_import():
    return jsonify({"job": active_set_job(get_database())})


@imports_blueprint.post("/api/v1/implicit-tag-jobs")
def create_implicit_tags_job():
    """Re-read source pages for media that may be missing an implicit ``furry`` tag."""
    settings: Settings = current_app.config["JIFFLE_SETTINGS"]
    connection = get_database()
    job_id = create_implicit_tag_job(connection)
    arguments = (
        settings.database_path, job_id, current_app.config["JIFFLE_SOURCE_PROVIDERS"],
    )
    if settings.run_jobs_inline:
        run_implicit_tag_job(*arguments)
    else:
        Thread(target=run_implicit_tag_job, args=arguments, daemon=True).start()
    return jsonify({"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}), 202


def _staged_upload_target(staging_root: Path, filename: str) -> Path:
    raw_name = Path(filename or "upload").name
    suffix = Path(raw_name).suffix.lower()
    safe_suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ""
    stem_source = raw_name[: -len(safe_suffix)] if safe_suffix else raw_name
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem_source).strip("._-")[:120] or "file"
    return staging_root / f"upload-{uuid4().hex}-{safe_stem}{safe_suffix}"


def _error(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message, "details": {}}}), status
