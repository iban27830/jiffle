"""Universal file/URL import resolution workflow.

The legacy local and URL jobs remain available for old clients.  This module
provides the single resolver used by the current Import screen.
"""

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from dataclasses import replace
from pathlib import Path
import shutil
import sqlite3
from threading import Lock
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import imagehash
from PIL import Image

from jiffle.configuration.settings import Settings
from jiffle.features.imports.history import create_import_history, update_import_history
from jiffle.features.imports.local_import import (
    ImportFailure,
    atomic_copy,
    find_exact_perceptual_duplicate,
    inspect_media,
)
from jiffle.features.imports.source_adapters.contracts import SourceMedia, SourceMatch
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.features.imports.url_normalization import normalize_source_url
from jiffle.infrastructure.database.connection import connect_database
from jiffle.infrastructure.media_revisions import create_original_revision


MIN_SIMILAR_CONFIDENCE = 80.0

# Perceptual lookups contact remote services that can be slow or unreachable.
# They run in parallel under one deadline, like the exact-search phase.
REVERSE_SEARCH_TIMEOUT_SECONDS = 20.0
MAX_REVERSE_CANDIDATES = 5

# A provider that is blocked or black-holed must not park an import: search
# calls run in parallel and any provider still running after this deadline is
# abandoned for this import (its bytes are simply not used).
PROVIDER_SEARCH_TIMEOUT_SECONDS = 10.0

# After a search times out the provider is skipped for a while so a batch of
# imports does not pay the same deadline again and again. It is retried
# automatically once the cooldown expires.
PROVIDER_COOLDOWN_SECONDS = 300.0

_provider_cooldowns: dict[str, float] = {}
_provider_cooldowns_lock = Lock()


def _provider_on_cooldown(name: str) -> bool:
    with _provider_cooldowns_lock:
        until = _provider_cooldowns.get(name)
        if until is None:
            return False
        if until <= time.monotonic():
            _provider_cooldowns.pop(name, None)
            return False
        return True


def _mark_provider_cooldown(name: str) -> None:
    with _provider_cooldowns_lock:
        _provider_cooldowns[name] = time.monotonic() + PROVIDER_COOLDOWN_SECONDS


def _clear_provider_cooldown(name: str) -> None:
    with _provider_cooldowns_lock:
        _provider_cooldowns.pop(name, None)


def _provider_needs_configuration(provider) -> bool:
    """True when a provider declares that it has no usable credentials yet."""
    configured = getattr(provider, "is_configured", True)
    if callable(configured):
        configured = configured()
    return not configured


# Failures that leave nothing useful to validate manually.  For these the job
# stays failed (the uploaded bytes are still kept on disk by the import worker).
_RETAIN_EXCLUDED_CODES = {
    "import.previously_deleted",
    "import.unsupported_media_type",
    "import.invalid_media",
    "import.file_not_found",
    "import.video_support_unavailable",
}


def create_universal_import_job(
    connection: sqlite3.Connection, submitted_input: str, input_kind: str
) -> int:
    cursor = connection.execute(
        "INSERT INTO background_jobs (job_type, status) VALUES ('import_resolve', 'pending')"
    )
    job_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO import_candidates (job_id, source_path, original_name, status) "
        "VALUES (?, ?, ?, 'pending')",
        (job_id, submitted_input, Path(submitted_input).name or "import",),
    )
    create_import_history(connection, job_id, {
        "submitted_input": submitted_input,
        "input_kind": input_kind,
        "resolution_method": "pending",
        "exact_candidates_checked": 0,
        "similar_candidates_found": 0,
        "search_status": "no_result",
        "search_state": "no_result",
        "search_outcome": "no_result",
        "provider_diagnostics": [],
    })
    connection.commit()
    return job_id


def run_universal_import_job(
    database_path: Path,
    settings: Settings,
    job_id: int,
    submitted_input: str,
    input_kind: str,
    providers: tuple[object, ...],
    downloader: object,
) -> None:
    connection = connect_database(database_path)
    temporary: list[Path] = []
    details: dict[str, object] = {
        "submitted_input": submitted_input,
        "input_kind": input_kind,
        "resolution_method": "none",
        "exact_candidates_checked": 0,
        "similar_candidates_found": 0,
        "resolved_source_url": None,
        "provider_errors": [],
        "provider_timings": [],
        "provider_diagnostics": [],
        "search_status": "no_result",
        "search_state": "no_result",
        "search_outcome": "no_result",
        "source_download_failed": False,
        "timing": {"duration_ms": 0, "phases_ms": {}},
    }
    started_at = time.perf_counter()
    details["_started_at"] = started_at
    try:
        _running(connection, job_id)
        source_path = Path(submitted_input)
        original_path: Path
        metadata_source: SourceMedia | None = None
        file_source: SourceMedia | None = None
        if input_kind == "url":
            normalized_input = normalize_source_url(submitted_input)
            candidate_id = _candidate_id(connection, job_id)
            blocked_source = connection.execute(
                "SELECT 1 FROM blocked_media_signatures WHERE source_url=?",
                (normalized_input,),
            ).fetchone()
            if blocked_source and settings.block_previously_deleted:
                raise ImportFailure("import.previously_deleted", "This source was previously deleted and is blocked by settings.")
            existing_source = _existing_source(connection, normalized_input)
            if existing_source:
                _set_candidate_result(connection, candidate_id, "duplicate", existing_source)
                details.update({"resolution_method": "source", "resolved_source_url": normalized_input})
                _set_search_status(details, "resolved")
                _finish(
                    connection,
                    job_id,
                    {"outcome": "duplicate", "candidate_id": candidate_id, "media_item_id": existing_source},
                    details,
                )
                return
            original_path, source, metadata_errors = _resolve_url_metadata(
                submitted_input, providers, details["provider_timings"], details["timing"]["phases_ms"],
                details["provider_diagnostics"],
            )
            details["provider_errors"] = metadata_errors
            if metadata_errors:
                _set_search_status(details, _status_from_errors(metadata_errors))
            if source is not None:
                blocked_source = connection.execute(
                    "SELECT 1 FROM blocked_media_signatures WHERE source_url=?", (source.canonical_url,)
                ).fetchone()
                if blocked_source and settings.block_previously_deleted:
                    raise ImportFailure("import.previously_deleted", "This source was previously deleted and is blocked by settings.")
                existing_source = _existing_source(connection, source.canonical_url)
                if existing_source:
                    _set_candidate_result(connection, candidate_id, "duplicate", existing_source)
                    details.update({"resolution_method": "source", "resolved_source_url": source.canonical_url})
                    _set_search_status(details, "resolved")
                    _finish(connection, job_id, {"outcome": "duplicate", "candidate_id": candidate_id, "media_item_id": existing_source}, details)
                    return
            if source is not None and source.direct_media_url:
                settings.resolved_import_staging_path.mkdir(parents=True, exist_ok=True)
                original_path = settings.resolved_import_staging_path / f"download-{uuid4().hex}{_extension(source.direct_media_url)}"
                source_download_started = time.perf_counter()
                try:
                    downloader.download(source.direct_media_url, original_path, source.canonical_url)
                    if source.content_md5 and _md5(original_path) != source.content_md5:
                        original_path.unlink(missing_ok=True)
                        original_path = None
                        details["source_download_failed"] = True
                        details.setdefault("provider_errors", []).append({
                            "provider": source.provider,
                            "code": "import.candidate_hash_mismatch",
                            "message": "The downloaded source did not match the source hash.",
                            "remote_id": source.remote_id,
                        })
                        _record_provider_diagnostic(
                            details["provider_diagnostics"], "exact_download", source.provider, "unavailable", 0,
                            "import.candidate_hash_mismatch",
                            "The downloaded source did not match the source hash.",
                            remote_id=source.remote_id,
                        )
                    else:
                        _record_provider_diagnostic(
                            details["provider_diagnostics"], "exact_download", source.provider, "matched", 1,
                            remote_id=source.remote_id,
                        )
                except Exception as error:
                    original_path.unlink(missing_ok=True)
                    original_path = None
                    details["source_download_failed"] = True
                    error_code, error_message = _download_failure(error, "The source file was unavailable.")
                    details.setdefault("provider_errors", []).append({
                        "provider": source.provider,
                        "code": error_code,
                        "message": error_message,
                        "remote_id": source.remote_id,
                    })
                    _record_provider_diagnostic(
                        details["provider_diagnostics"], "exact_download", source.provider, "unavailable", 0,
                        error_code,
                        error_message,
                        remote_id=source.remote_id,
                    )
                details["timing"]["phases_ms"]["source_download"] = _elapsed_ms(source_download_started)
            if original_path is not None:
                temporary.append(original_path)
                metadata_source = source
                file_source = source
                result = _accept_downloaded(
                    connection, settings, job_id, original_path, source, submitted_input,
                    source_url_override=source.canonical_url if source else normalized_input,
                    metadata_source=metadata_source, file_source=file_source,
                )
                details.update({
                    "resolution_method": result.get("resolution_method", "source"),
                    "resolved_source_url": source.canonical_url if source else submitted_input,
                })
                _set_search_status(details, "resolved")
                _finish(connection, job_id, result, details)
                return
            digest = source.content_md5 if source else None
            source_hint = source
            metadata_source = source_hint
        else:
            if not source_path.is_file():
                raise ImportFailure("import.file_not_found", "The selected file does not exist.")
            source_hint = None
            metadata_source = None
            digest = _md5(source_path)
            original_path = source_path

        if input_kind == "url" and original_path is None:
            if not digest:
                raise ImportFailure("import.source_media_missing", "The source has no downloadable media or file hash.")
            if connection.execute("SELECT 1 FROM blocked_media_signatures WHERE content_hash=?", (digest,)).fetchone() and settings.block_previously_deleted:
                raise ImportFailure("import.previously_deleted", "This media was previously deleted and is blocked by settings.")
            exact_started = time.perf_counter()
            exact, errors = _search_exact(
                providers, digest, source_hint, details["provider_timings"], details["provider_diagnostics"]
            )
            details["timing"]["phases_ms"]["exact_search"] = _elapsed_ms(exact_started)
            details["exact_candidates_checked"] = len(exact)
            details["provider_errors"] = list(details.get("provider_errors", [])) + errors
            _set_search_status(
                details,
                "matched" if exact else (
                    "candidate_download_failed" if details.get("source_download_failed")
                    else _status_from_errors(errors)
                ),
            )
            download_started = time.perf_counter()
            valid_candidates, download_errors = _download_exact_candidates(
                exact, digest, settings, downloader, details["provider_diagnostics"]
            )
            details["timing"]["phases_ms"]["exact_download"] = _elapsed_ms(download_started)
            details.setdefault("provider_errors", []).extend(download_errors)
            for match, downloaded in valid_candidates:
                try:
                    file_source = _match_to_source(match)
                    result = _accept_downloaded(
                        connection, settings, job_id, downloaded, file_source, submitted_input,
                        source_url_override=metadata_source.canonical_url if metadata_source else normalized_input,
                        metadata_source=metadata_source or file_source,
                        file_source=file_source,
                    )
                    details.update({
                        "resolution_method": result.get("resolution_method", "exact"),
                        "resolved_source_url": match.canonical_url,
                    })
                    _set_search_status(details, "resolved")
                    _finish(connection, job_id, result, details)
                    for _, other in valid_candidates:
                        other.unlink(missing_ok=True)
                    return
                except Exception as error:
                    code, message = _download_failure(error, "The exact candidate was unavailable.")
                    details.setdefault("provider_errors", []).append({"provider": match.provider, "code": code, "message": message, "remote_id": match.remote_id})
                    _record_provider_diagnostic(details["provider_diagnostics"], "exact_download", match.provider, "unavailable", 0, code, message, remote_id=match.remote_id)
                finally:
                    downloaded.unlink(missing_ok=True)
            if exact and (download_errors or details.get("search_status") == "matched"):
                _set_search_status(details, "candidate_download_failed")
            elif details.get("search_status") not in {"network_error", "authorization_error"}:
                _set_search_status(details, _status_from_errors(errors))
            failure = _first_download_failure(details.get("provider_errors"))
            if failure:
                raise ImportFailure(*failure)
            raise ImportFailure("import.source_not_found", "No downloadable exact copy was found for this source.")

        inspection = inspect_media(original_path)
        _progress(connection, job_id, 25, "Reading the file and checking the library")
        candidate_id = _candidate_id(connection, job_id)
        _update_candidate(connection, candidate_id, inspection, None, "pending")
        pending_review = connection.execute(
            "SELECT review.id FROM review_items review "
            "JOIN import_candidates candidate ON candidate.id=review.import_candidate_id "
            "WHERE review.status='pending' AND candidate.content_hash=? LIMIT 1",
            (inspection.content_hash,),
        ).fetchone()
        if pending_review:
            _set_candidate_result(connection, candidate_id, "duplicate", None)
            details["resolution_method"] = "pending_review"
            _finish(
                connection,
                job_id,
                {
                    "outcome": "duplicate",
                    "candidate_id": candidate_id,
                    "media_item_id": None,
                    "review_item_id": int(pending_review[0]),
                },
                details,
            )
            return
        duplicate = connection.execute(
            "SELECT id FROM media_items WHERE content_hash=? AND deleted_at IS NULL",
            (inspection.content_hash,),
        ).fetchone()
        if duplicate:
            _set_candidate_result(connection, candidate_id, "duplicate", int(duplicate[0]))
            details["resolution_method"] = "local_sha256"
            _finish(connection, job_id, {"outcome": "duplicate", "candidate_id": candidate_id,
                                        "media_item_id": int(duplicate[0])}, details)
            return

        blocked_hash = connection.execute(
            "SELECT 1 FROM blocked_media_signatures WHERE content_hash=?", (digest or inspection.content_hash,)
        ).fetchone()
        if blocked_hash and settings.block_previously_deleted:
            raise ImportFailure("import.previously_deleted", "This media was previously deleted and is blocked by settings.")

        # A local file that is already in the library is answered from the
        # local fingerprints before any provider is contacted: an unreachable
        # provider must not make a known file look like it is still importing.
        if input_kind != "url":
            perceptual_duplicate = find_exact_perceptual_duplicate(
                connection, settings, original_path, inspection
            )
            if perceptual_duplicate is not None:
                _set_candidate_result(connection, candidate_id, "duplicate", perceptual_duplicate)
                details["resolution_method"] = "local_perceptual_duplicate"
                _finish(
                    connection,
                    job_id,
                    {
                        "outcome": "duplicate",
                        "candidate_id": candidate_id,
                        "media_item_id": perceptual_duplicate,
                        "resolution_method": "local_perceptual_duplicate",
                    },
                    details,
                )
                return

        if not digest:
            digest = _md5(original_path)
        _progress(connection, job_id, 45, "Searching supported sources")
        exact_started = time.perf_counter()
        exact, errors = _search_exact(
            providers, digest, source_hint, details["provider_timings"], details["provider_diagnostics"],
            on_progress=lambda names: _progress(
                connection, job_id, 50, "Waiting for sources: " + ", ".join(sorted(names))
            ),
        )
        details["timing"]["phases_ms"]["exact_search"] = _elapsed_ms(exact_started)
        details["exact_candidates_checked"] = len(exact)
        details["provider_errors"] = list(details.get("provider_errors", [])) + errors
        if metadata_source is None and exact:
            best_metadata = _select_metadata_source(exact)
            if best_metadata is not None:
                metadata_source = _match_to_source(best_metadata)
        _set_search_status(details, "matched" if exact else _status_from_errors(errors))
        _progress(connection, job_id, 70 if exact else 65, "Checking source files")
        download_started = time.perf_counter()
        valid_candidates, download_errors = _download_exact_candidates(
            exact, digest, settings, downloader, details["provider_diagnostics"]
        )
        details["timing"]["phases_ms"]["exact_download"] = _elapsed_ms(download_started)
        details["provider_errors"] = list(details.get("provider_errors", [])) + download_errors
        for match, downloaded in valid_candidates:
            try:
                temporary.append(downloaded)
                file_source = _match_to_source(match)
                result = _accept_downloaded(
                    connection, settings, job_id, downloaded, file_source, submitted_input,
                    metadata_source=metadata_source or file_source,
                    file_source=file_source,
                )
                details.update({
                    "resolution_method": result.get("resolution_method", "exact"),
                    "resolved_source_url": match.canonical_url,
                })
                _set_search_status(details, "resolved")
                _finish(connection, job_id, result, details)
                temporary.remove(downloaded)
                downloaded.unlink(missing_ok=True)
                for _, other in valid_candidates:
                    other.unlink(missing_ok=True)
                return
            except Exception as error:
                downloaded.unlink(missing_ok=True)
                code, message = _download_failure(error, "The exact candidate was unavailable.")
                details.setdefault("provider_errors", []).append({"provider": match.provider, "code": code, "message": message, "remote_id": match.remote_id})
                _record_provider_diagnostic(details["provider_diagnostics"], "exact_download", match.provider, "unavailable", 0, code, message, remote_id=match.remote_id)
            finally:
                if downloaded in temporary:
                    temporary.remove(downloaded)
        for _, downloaded in valid_candidates:
            downloaded.unlink(missing_ok=True)

        if exact and (download_errors or details.get("search_status") == "matched"):
            _set_search_status(details, "candidate_download_failed")
        elif details.get("search_status") not in {"network_error", "authorization_error"}:
            _set_search_status(details, _status_from_errors(errors))

        perceptual_duplicate = find_exact_perceptual_duplicate(
            connection, settings, original_path, inspection
        )
        if perceptual_duplicate is not None:
            _set_candidate_result(connection, candidate_id, "duplicate", perceptual_duplicate)
            details["resolution_method"] = "local_perceptual_duplicate"
            _finish(
                connection,
                job_id,
                {
                    "outcome": "duplicate",
                    "candidate_id": candidate_id,
                    "media_item_id": perceptual_duplicate,
                    "resolution_method": "local_perceptual_duplicate",
                },
                details,
            )
            return

        # Keep the original in staging before trying approximate candidates.
        original_staged = atomic_copy(original_path, settings.resolved_import_staging_path, "candidate")
        candidate_paths: list[tuple[SourceMatch, Path, object]] = []
        similar = _local_similar(connection, settings, original_path, inspection)
        _record_provider_diagnostic(
            details["provider_diagnostics"], "perceptual_search", "local",
            "matched" if similar else "no_result", len(similar)
        )
        if inspection.media_type == "image":
            similar.extend(_reverse_similar(original_path, providers, details["provider_diagnostics"]))
        similar = [_coerce_match(match, "perceptual") for match in similar]
        similar = [match for match in _unique_similar(similar) if match.confidence >= MIN_SIMILAR_CONFIDENCE]
        details["similar_candidates_found"] = len(similar)
        for match in similar:
            media_url = match.direct_media_url or match.preview_url
            if not media_url:
                _record_provider_diagnostic(
                    details["provider_diagnostics"], "perceptual_search", match.provider,
                    "unavailable", 0, "import.candidate_unavailable",
                    "The similar candidate has no media URL.", remote_id=match.remote_id,
                )
                continue
            path = settings.resolved_import_staging_path / f"candidate-{uuid4().hex}{_extension(media_url)}"
            try:
                local_candidate = Path(str(media_url))
                if local_candidate.is_file():
                    shutil.copy2(local_candidate, path)
                else:
                    downloader.download(media_url, path, match.canonical_url)
                candidate_inspection = inspect_media(path)
                candidate_paths.append((match, path, candidate_inspection))
            except Exception as error:
                path.unlink(missing_ok=True)
                _record_provider_diagnostic(
                    details["provider_diagnostics"], "perceptual_search", match.provider, "unavailable", 0,
                    "import.candidate_unavailable", _safe_error_message(error, "The similar candidate was unavailable."),
                    remote_id=match.remote_id,
                )
        if candidate_paths:
            cursor = connection.execute(
                "UPDATE import_candidates SET status='review', stored_path=?, media_type=?, "
                "content_hash=?, width=?, height=?, file_size=?, source_metadata_json=? WHERE id=?",
                (original_staged, inspection.media_type, inspection.content_hash,
                 inspection.width, inspection.height, inspection.file_size,
                 _serialize_source(metadata_source) if metadata_source else None, candidate_id),
            )
            review_cursor = connection.execute(
                "INSERT INTO review_items (import_candidate_id, reason) VALUES (?, 'source_candidates')",
                (candidate_id,),
            )
            review_id = int(review_cursor.lastrowid)
            for rank, (match, path, candidate_inspection) in enumerate(candidate_paths):
                connection.execute(
                    "INSERT INTO import_source_candidates "
                    "(review_item_id, rank, match_method, confidence, provider, source_metadata_json, "
                    "stored_path, media_type, content_hash, width, height, file_size) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (review_id, rank, match.match_method, match.confidence, match.provider,
                     json.dumps(match.as_dict()), path.name, candidate_inspection.media_type,
                     candidate_inspection.content_hash, candidate_inspection.width,
                     candidate_inspection.height, candidate_inspection.file_size),
                )
            connection.commit()
            details["resolution_method"] = "perceptual"
            _finish(connection, job_id, {"outcome": "review", "candidate_id": candidate_id,
                                         "review_item_id": review_id,
                                         "source_candidates": len(candidate_paths)}, details)
            return

        # No usable source: retain the uploaded file for the existing manual Source action.
        connection.execute(
            "UPDATE import_candidates SET status='review', stored_path=?, media_type=?, "
            "content_hash=?, width=?, height=?, file_size=?, source_metadata_json=? WHERE id=?",
            (original_staged, inspection.media_type, inspection.content_hash,
             inspection.width, inspection.height, inspection.file_size,
             _serialize_source(metadata_source) if metadata_source else None, candidate_id),
        )
        review_cursor = connection.execute(
            "INSERT INTO review_items (import_candidate_id, reason) VALUES (?, 'source_required')",
            (candidate_id,),
        )
        connection.commit()
        details["resolution_method"] = "source_required"
        _finish(connection, job_id, {"outcome": "review", "candidate_id": candidate_id,
                                     "review_item_id": int(review_cursor.lastrowid)}, details)
    except (ImportFailure, SourceProviderFailure) as error:
        if _retain_failed_upload(connection, settings, job_id, submitted_input, input_kind, error.code, details):
            details["resolution_error"] = {
                "code": error.code,
                "message": _safe_error_message(error.message, "The source could not be resolved."),
            }
            details["resolution_method"] = "source_required"
            _set_search_status(details, _status_for_error(error.code))
            _finish(connection, job_id, {
                "outcome": "review",
                "candidate_id": _candidate_id(connection, job_id),
                "review_item_id": details.get("review_item_id"),
            }, details)
        else:
            _failed(connection, job_id, error.code, error.message, details)
    except Exception:
        if _retain_failed_upload(connection, settings, job_id, submitted_input, input_kind, "import.resolve_failed", details):
            details["resolution_error"] = {
                "code": "import.resolve_failed",
                "message": "The input could not be resolved.",
            }
            details["resolution_method"] = "source_required"
            _finish(connection, job_id, {
                "outcome": "review",
                "candidate_id": _candidate_id(connection, job_id),
                "review_item_id": details.get("review_item_id"),
            }, details)
        else:
            _failed(connection, job_id, "import.resolve_failed", "The input could not be resolved.", details)
            raise
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)
        if input_kind == "file":
            row = connection.execute(
                "SELECT status, stored_path FROM import_candidates WHERE job_id=?", (job_id,)
            ).fetchone()
            upload_path = Path(submitted_input)
            is_staged_upload = upload_path.parent.resolve() == settings.resolved_import_staging_path.resolve() and upload_path.name.startswith("upload-")
            stored_path = row["stored_path"] if row is not None else None
            has_durable_replacement = bool(stored_path) and stored_path != upload_path.name
            # Keep the uploaded bytes until they are safely stored in the library
            # (accepted/duplicate) or a durable staged copy exists for review.
            # Failed imports keep the upload on disk so nothing the user dropped
            # into Jiffle is ever lost when the source is unavailable.
            if row and is_staged_upload and (
                row["status"] in {"accepted", "duplicate"} or has_durable_replacement
            ):
                upload_path.unlink(missing_ok=True)
        connection.close()


def _resolve_url_metadata(
    url: str,
    providers,
    provider_timings=None,
    phase_timings=None,
    diagnostics=None,
):
    normalized = normalize_source_url(url)
    provider = next((item for item in providers if item.can_handle(normalized)), None)
    if provider is None:
        if diagnostics is not None:
            _record_provider_diagnostic(
                diagnostics, "metadata", "direct", "matched", 1,
            )
        return None, SourceMedia(normalized, normalized, "direct", "", None,
                                 urlsplit(normalized).netloc, (), _extension(normalized)), []
    errors = []
    started = time.perf_counter()
    try:
        fetch_metadata = getattr(provider, "fetch_metadata", provider.fetch)
        source = _coerce_source(fetch_metadata(normalized), normalized, getattr(provider, "provider_name", "unknown"))
        duration_ms = _elapsed_ms(started)
        _record_provider_timing(provider_timings, provider, duration_ms, "ok")
        if diagnostics is not None:
            _record_provider_diagnostic(
                diagnostics, "metadata", getattr(provider, "provider_name", "unknown"),
                "matched" if source else "no_result", 1 if source else 0,
                duration_ms=duration_ms,
                remote_id=getattr(source, "remote_id", None),
            )
        if phase_timings is not None:
            phase_timings["metadata"] = duration_ms
        if source.direct_media_url:
            return None, source, errors
        return None, source, errors
    except SourceProviderFailure as error:
        duration_ms = _elapsed_ms(started)
        _record_provider_timing(provider_timings, provider, duration_ms, "error")
        message = _safe_error_message(error.message, "The source provider is unavailable.")
        if diagnostics is not None:
            _record_provider_diagnostic(
                diagnostics, "metadata", getattr(provider, "provider_name", "unknown"),
                _status_for_error(error.code), 0, error.code, message,
                duration_ms, getattr(error, "remote_id", None),
            )
        if phase_timings is not None:
            phase_timings["metadata"] = duration_ms
        errors.append({"provider": getattr(provider, "provider_name", "unknown"), "code": error.code, "message": message})
        return None, None, errors
    except Exception as error:
        duration_ms = _elapsed_ms(started)
        _record_provider_timing(provider_timings, provider, duration_ms, "error")
        message = _safe_error_message(error, "The source provider is unavailable.")
        if diagnostics is not None:
            _record_provider_diagnostic(
                diagnostics, "metadata", getattr(provider, "provider_name", "unknown"),
                "network_error", 0, "import.provider_unavailable", message,
                duration_ms,
            )
        if phase_timings is not None:
            phase_timings["metadata"] = duration_ms
        errors.append({"provider": getattr(provider, "provider_name", "unknown"), "code": "import.provider_unavailable", "message": message})
        return None, None, errors


def _search_exact(
    providers,
    digest: str,
    source_hint: SourceMedia | None,
    provider_timings=None,
    diagnostics=None,
    on_progress=None,
):
    ordered = list(providers)
    if source_hint:
        ordered.sort(key=lambda p: 0 if getattr(p, "provider_name", "") == source_hint.provider else 1)
    searchable = [(index, provider) for index, provider in enumerate(ordered)
                  if callable(getattr(provider, "search_by_md5", None))]

    def lookup(position, provider):
        started = time.perf_counter()
        try:
            raw_matches = getattr(provider, "search_by_md5")(digest) or []
            matches = [_coerce_match(raw, "exact") for raw in raw_matches]
            matches = [match for match in matches if match]
            return position, matches, [], _elapsed_ms(started), "ok"
        except SourceProviderFailure as error:
            return position, [], [{"provider": getattr(provider, "provider_name", "unknown"), "code": error.code, "message": _safe_error_message(error.message, "The source search failed.")}], _elapsed_ms(started), "error"
        except Exception as error:
            return position, [], [{"provider": getattr(provider, "provider_name", "unknown"), "code": "import.source_search_failed", "message": _safe_error_message(error, "The source search failed.")}], _elapsed_ms(started), "error"

    results = []
    active = []
    for position, (_index, provider) in enumerate(searchable):
        name = getattr(provider, "provider_name", "unknown")
        if _provider_on_cooldown(name):
            results.append((
                position,
                [],
                [{
                    "provider": name,
                    "code": "import.provider_cooldown",
                    "message": f"{name} did not answer recently and was skipped for a few minutes.",
                }],
                0,
                "skipped",
            ))
        elif _provider_needs_configuration(provider):
            # A provider without credentials (for example Rule34) is reported in
            # the diagnostics but is not treated as a failed source, so it cannot
            # change the outcome of an import that simply found no exact copy.
            results.append((position, [], [], 0, "not_configured"))
        else:
            active.append((position, provider))

    executor = ThreadPoolExecutor(max_workers=max(1, len(active))) if active else None
    if executor is not None:
        futures = {
            executor.submit(lookup, position, provider): (position, provider)
            for position, provider in active
        }
        pending = set(futures)
        deadline = time.monotonic() + PROVIDER_SEARCH_TIMEOUT_SECONDS
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=min(remaining, 1.0))
            for future in done:
                results.append(future.result())
            if pending and on_progress is not None:
                on_progress([
                    getattr(futures[future][1], "provider_name", "unknown")
                    for future in pending
                ])
        for future in pending:
            future.cancel()
            position, provider = futures[future]
            name = getattr(provider, "provider_name", "unknown")
            _mark_provider_cooldown(name)
            results.append((
                position,
                [],
                [{
                    "provider": name,
                    "code": "import.provider_timeout",
                    "message": f"{name} did not answer within {PROVIDER_SEARCH_TIMEOUT_SECONDS:.0f} seconds.",
                }],
                int(PROVIDER_SEARCH_TIMEOUT_SECONDS * 1000),
                "timeout",
            ))
        # Abandoned searches keep running in the background; their results are
        # discarded, so the caller must not wait for their threads either.
        executor.shutdown(wait=False)
    results.sort(key=lambda item: item[0])
    matches: list[SourceMatch] = []
    errors: list[dict[str, str]] = []
    for _index, provider_matches, provider_errors, duration_ms, status in results:
        matches.extend(provider_matches)
        errors.extend(provider_errors)
        if provider_timings is not None:
            provider = searchable[_index][1]
            _record_provider_timing(provider_timings, provider, duration_ms, status)
        if diagnostics is not None:
            provider = searchable[_index][1]
            name = getattr(provider, "provider_name", "unknown")
            if status == "ok":
                _clear_provider_cooldown(name)
            error = provider_errors[0] if provider_errors else None
            diagnostic_status = (
                _status_for_error(error["code"])
                if error else ("matched" if provider_matches else "no_result")
            )
            if status in {"timeout", "skipped", "not_configured"}:
                diagnostic_status = status
            _record_provider_diagnostic(
                diagnostics,
                "exact_search",
                name,
                diagnostic_status,
                len(provider_matches),
                error.get("code") if error else None,
                error.get("message") if error else None,
                duration_ms,
            )
    unique = {}
    for match in matches:
        key = (match.provider, match.remote_id or match.canonical_url)
        unique.setdefault(key, match)
    return list(unique.values()), errors


def _download_exact_candidates(exact, digest, settings, downloader, diagnostics=None):
    settings.resolved_import_staging_path.mkdir(parents=True, exist_ok=True)

    def download_one(index, match):
        media_url = match.direct_media_url or match.preview_url
        if not media_url:
            error = {"provider": match.provider, "code": "import.candidate_unavailable", "message": "The exact candidate has no media URL.", "remote_id": match.remote_id}
            if diagnostics is not None:
                _record_provider_diagnostic(diagnostics, "exact_download", match.provider, "unavailable", 0, error["code"], error["message"], remote_id=match.remote_id)
            return index, match, None, error
        downloaded = settings.resolved_import_staging_path / f"resolve-{uuid4().hex}{_extension(media_url)}"
        try:
            downloader.download(media_url, downloaded, match.canonical_url)
            if _md5(downloaded) != digest:
                downloaded.unlink(missing_ok=True)
                error = {"provider": match.provider, "code": "import.candidate_hash_mismatch", "message": "The downloaded candidate did not match the source hash.", "remote_id": match.remote_id}
                if diagnostics is not None:
                    _record_provider_diagnostic(diagnostics, "exact_download", match.provider, "unavailable", 0, error["code"], error["message"], remote_id=match.remote_id)
                return index, match, None, error
            if diagnostics is not None:
                _record_provider_diagnostic(diagnostics, "exact_download", match.provider, "matched", 1, remote_id=match.remote_id)
            return index, match, downloaded, None
        except Exception as error:
            downloaded.unlink(missing_ok=True)
            code, message = _download_failure(error, "The exact candidate was unavailable.")
            item = {"provider": match.provider, "code": code, "message": message, "remote_id": match.remote_id}
            if diagnostics is not None:
                _record_provider_diagnostic(diagnostics, "exact_download", match.provider, "unavailable", 0, code, message, remote_id=match.remote_id)
            return index, match, None, item

    valid = []
    errors = []
    candidates = [(index, match) for index, match in enumerate(exact)]
    if not candidates:
        return valid, errors
    with ThreadPoolExecutor(max_workers=min(4, len(candidates)), thread_name_prefix="jiffle-exact") as executor:
        futures = [executor.submit(download_one, index, match) for index, match in candidates]
        results = [future.result() for future in as_completed(futures)]
    for index, match, downloaded, error in sorted(results, key=lambda item: item[0]):
        if downloaded is not None:
            valid.append((match, downloaded))
        elif error:
            errors.append(error)
    return valid, errors


def resolve_exact_downloads(path, providers, downloader, settings, diagnostics=None):
    """Find exact copies of a local file and download the bytes that verify.

    Returns ``(matches, verified, errors)``.  ``matches`` are every exact source
    the providers reported for the file hash, ``verified`` is a list of
    ``(SourceMatch, Path)`` pairs whose downloaded bytes matched the hash, and
    ``errors`` summarises provider and download failures.  Callers own the
    returned paths and must remove them.
    """
    digest = _md5(path)
    matches, search_errors = _search_exact(providers, digest, None, None, diagnostics)
    verified, download_errors = _download_exact_candidates(
        matches, digest, settings, downloader, diagnostics
    )
    return matches, verified, search_errors + download_errors


def _record_provider_timing(target, provider, duration_ms, status):
    if target is not None:
        target.append({
            "provider": getattr(provider, "provider_name", "unknown"),
            "duration_ms": duration_ms,
            "status": status,
        })


def _record_provider_diagnostic(
    target,
    stage: str,
    provider: str,
    status: str,
    candidate_count: int = 0,
    code: str | None = None,
    message: str | None = None,
    duration_ms: int | None = None,
    remote_id: str | None = None,
) -> None:
    """Append a safe, UI-friendly result for one provider and resolution stage."""
    if target is None:
        return
    item: dict[str, object] = {
        "stage": stage,
        "provider": provider or "unknown",
        "status": status,
        "candidate_count": max(0, int(candidate_count or 0)),
    }
    if code:
        item["code"] = str(code)
        item["error_code"] = str(code)
    if message:
        safe_message = _safe_error_message(message, "The provider did not complete this step.")
        item["message"] = safe_message
        item["error_message"] = safe_message
    if duration_ms is not None:
        item["duration_ms"] = max(0, int(duration_ms))
    if remote_id not in (None, ""):
        item["remote_id"] = str(remote_id)
    target.append(item)


def _safe_error_message(error, fallback: str) -> str:
    """Keep diagnostics useful without leaking URLs, credentials, or exception data."""
    message = str(error or "").strip()
    if not message:
        return fallback
    # Provider failures already expose a curated message.  For arbitrary
    # exceptions retain only a short first line so request details cannot leak.
    message = re.sub(r"https?://[^\s)]+", "<redacted-url>", message.splitlines()[0])
    return message[:240]


def _download_failure(error, fallback: str) -> tuple[str, str]:
    """Translate downloader/provider exceptions into stable user diagnostics."""
    code = getattr(error, "code", None)
    message = getattr(error, "message", None)
    if code:
        return str(code), _safe_error_message(message or error, fallback)
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status in {401, 403, 404}:
        return "import.source_media_unavailable", f"The source media is unavailable (HTTP {status})."
    if status == 408:
        return "import.download_timeout", "The media download timed out."
    if status == 429:
        return "import.download_rate_limited", "The media source rate limit was reached."
    if status in {500, 502, 503, 504}:
        return "import.download_unavailable", f"The media source returned temporary HTTP {status}."
    name = error.__class__.__name__.lower()
    if "timeout" in name:
        return "import.download_timeout", "The media download timed out."
    if "connection" in name:
        return "import.download_unavailable", "The media source was unavailable."
    if isinstance(error, ImportFailure):
        return error.code, _safe_error_message(error.message, fallback)
    safe = _safe_error_message(error, fallback)
    if "html" in safe.lower():
        return "import.source_media_unavailable", "The source returned HTML instead of media."
    return "import.candidate_unavailable", safe


def _first_download_failure(errors) -> tuple[str, str] | None:
    for item in errors or ():
        if isinstance(item, dict) and item.get("code") and item.get("message"):
            return str(item["code"]), str(item["message"])
    return None


def _status_for_error(code: str | None) -> str:
    value = str(code or "").lower()
    if any(token in value for token in ("auth", "credential", "access_denied", "authorization")):
        return "authorization_error"
    if any(token in value for token in ("unavailable", "network", "timeout", "connection", "rate_limited", "source_search_failed", "cooldown")):
        return "network_error"
    return "unavailable"


def _status_from_errors(errors) -> str:
    statuses = {_status_for_error(item.get("code")) for item in (errors or []) if isinstance(item, dict)}
    if "authorization_error" in statuses:
        return "authorization_error"
    if "network_error" in statuses:
        return "network_error"
    return "no_result"


def _set_search_status(details: dict[str, object], status: str) -> None:
    details["search_status"] = status
    # Keep an explicit alias for consumers that call this a search state.
    details["search_state"] = status
    details["search_outcome"] = status


def _local_similar(connection, settings, original_path, inspection):
    if inspection.media_type != "image":
        return []
    try:
        with Image.open(original_path) as image:
            wanted = imagehash.phash(image)
    except (OSError, ValueError):
        return []
    rows = connection.execute(
        "SELECT fp.media_item_id, fp.perceptual_hash, source.provider, source.canonical_url, "
        "source.direct_media_url, source.remote_id, source.author, source.domain, source.parent_id, "
        "source.character_tags_json, media.file_path, media.width, media.height FROM media_fingerprints fp "
        "JOIN media_items media ON media.id=fp.media_item_id "
        "LEFT JOIN media_sources source ON source.media_item_id=media.id "
        "WHERE media.deleted_at IS NULL"
    ).fetchall()
    results = []
    for row in rows:
        try:
            confidence = 100.0 * (wanted.hash.size - (wanted - imagehash.hex_to_hash(row["perceptual_hash"]))) / wanted.hash.size
        except (TypeError, ValueError):
            continue
        if confidence < MIN_SIMILAR_CONFIDENCE or not row["provider"] or not row["canonical_url"]:
            continue
        path = settings.media_path / row["file_path"]
        results.append(SourceMatch(
            provider=row["provider"], canonical_url=row["canonical_url"],
            direct_media_url=str(path) if path.is_file() else row["direct_media_url"],
            remote_id=row["remote_id"], author=row["author"], domain=row["domain"],
            character_tags=_decode_json_tags(row["character_tags_json"]),
            parent_id=row["parent_id"],
            match_method="perceptual", confidence=round(confidence, 2),
            width=row["width"], height=row["height"],
        ))
    return results


def _reverse_similar(image_path, providers, diagnostics=None):
    """Run every available reverse search and return confirmable candidates.

    Providers are queried at the same time under one deadline so a slow service
    cannot park the import.  A result that points at a supported post is loaded
    through the matching provider first, so the candidate carries real tags and
    the original file instead of only a similarity thumbnail.
    """
    reverse_providers = []
    for provider in list(providers) + [_iqdb_reverse_search()]:
        if provider is None or not callable(getattr(provider, "search_similar", None)):
            continue
        if _provider_needs_configuration(provider):
            continue
        reverse_providers.append(provider)
    if not reverse_providers:
        return []

    raw_results: list[dict[str, object]] = []
    executor = ThreadPoolExecutor(
        max_workers=len(reverse_providers), thread_name_prefix="jiffle-reverse"
    )
    futures = {
        executor.submit(_run_reverse_search, provider, image_path): provider
        for provider in reverse_providers
    }
    pending = set(futures)
    deadline = time.monotonic() + REVERSE_SEARCH_TIMEOUT_SECONDS
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        done, pending = wait(pending, timeout=min(remaining, 1.0))
        for future in done:
            raw_results.append(future.result())
    for future in pending:
        future.cancel()
        provider = futures[future]
        name = getattr(provider, "provider_name", None) or provider.__class__.__name__.lower()
        raw_results.append({
            "provider": name,
            "matches": [],
            "duration_ms": int(REVERSE_SEARCH_TIMEOUT_SECONDS * 1000),
            "error": {
                "status": "timeout",
                "code": "import.provider_timeout",
                "message": (
                    f"{name} did not answer within "
                    f"{REVERSE_SEARCH_TIMEOUT_SECONDS:.0f} seconds."
                ),
            },
        })
    # Abandoned searches keep running in the background; their results are
    # discarded, so the caller must not wait for their threads either.
    executor.shutdown(wait=False)

    resolved: list[object] = []
    for result in raw_results:
        provider_name = str(result["provider"])
        error = result.get("error")
        raw_matches = list(result.get("matches") or [])
        if diagnostics is not None:
            if error:
                _record_provider_diagnostic(
                    diagnostics, "perceptual_search", provider_name,
                    error["status"], 0, error["code"], error["message"],
                    result["duration_ms"],
                )
            else:
                _record_provider_diagnostic(
                    diagnostics, "perceptual_search", provider_name,
                    "matched" if raw_matches else "no_result", len(raw_matches),
                    duration_ms=result["duration_ms"],
                )
        for raw in raw_matches:
            confidence = _reverse_confidence(raw)
            if confidence is None or confidence < MIN_SIMILAR_CONFIDENCE:
                continue
            resolved.append(_resolve_reverse_candidate(raw, providers))
    matches = [
        match
        for match in (_coerce_match(item, "perceptual") for item in resolved)
        if match is not None
    ]
    return _unique_similar(matches)[:MAX_REVERSE_CANDIDATES]


def _iqdb_reverse_search():
    try:
        from jiffle.features.imports.source_adapters.iqdb import IqdbReverseSearch
        return IqdbReverseSearch()
    except Exception:
        return None


def _run_reverse_search(provider, image_path) -> dict[str, object]:
    name = getattr(provider, "provider_name", None) or provider.__class__.__name__.lower()
    started = time.perf_counter()
    try:
        matches = list(getattr(provider, "search_similar")(image_path) or [])
        return {
            "provider": name, "matches": matches, "error": None,
            "duration_ms": _elapsed_ms(started),
        }
    except SourceProviderFailure as error:
        return {
            "provider": name, "matches": [], "duration_ms": _elapsed_ms(started),
            "error": {
                "status": _status_for_error(error.code),
                "code": error.code,
                "message": _safe_error_message(error.message, "The reverse search failed."),
            },
        }
    except Exception as error:
        return {
            "provider": name, "matches": [], "duration_ms": _elapsed_ms(started),
            "error": {
                "status": "network_error",
                "code": "import.source_search_failed",
                "message": _safe_error_message(error, "The reverse search failed."),
            },
        }


def _reverse_confidence(raw) -> float | None:
    value = raw.get("confidence") if isinstance(raw, dict) else getattr(raw, "confidence", None)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _reverse_links(raw) -> list[str]:
    values: list[str] = []
    if isinstance(raw, dict):
        links = raw.get("links")
        if isinstance(links, (list, tuple)):
            values.extend(str(link) for link in links if link)
        if raw.get("canonical_url"):
            values.append(str(raw["canonical_url"]))
    else:
        canonical = getattr(raw, "canonical_url", None)
        if canonical:
            values.append(str(canonical))
    return list(dict.fromkeys(values))


def _reverse_md5(url: str) -> str | None:
    """Read a 32-character MD5 from a booru search link, when present."""
    query = parse_qs(urlsplit(url).query)
    value = str((query.get("md5") or [""])[0]).strip().lower()
    if len(value) == 32 and all(character in "0123456789abcdef" for character in value):
        return value
    return None


def _reverse_candidate_from_url(url: str, providers):
    """Load full metadata for a reverse-search link through a matching provider."""
    for provider in providers:
        can_handle = getattr(provider, "can_handle", None)
        if not callable(can_handle):
            continue
        try:
            if not can_handle(url):
                continue
        except Exception:
            continue
        digest = _reverse_md5(url)
        if digest:
            search = getattr(provider, "search_by_md5", None)
            if not callable(search):
                continue
            try:
                for raw in search(digest) or []:
                    match = _coerce_match(raw, "perceptual")
                    if match is not None and (match.direct_media_url or match.preview_url):
                        return match
            except Exception:
                continue
            continue
        fetch = getattr(provider, "fetch", None)
        if not callable(fetch):
            continue
        try:
            source = _coerce_source(
                fetch(url), url, getattr(provider, "provider_name", "unknown")
            )
        except Exception:
            continue
        if isinstance(source, SourceMedia):
            return SourceMatch(
                provider=source.provider,
                canonical_url=source.canonical_url or url,
                direct_media_url=source.direct_media_url,
                remote_id=source.remote_id,
                author=source.author,
                domain=source.domain,
                tags=tuple(source.tags),
                content_md5=source.content_md5,
                match_method="perceptual",
                confidence=100.0,
                character_tags=tuple(source.character_tags),
                parent_id=source.parent_id,
            )
    return None


def _resolve_reverse_candidate(raw, providers):
    if isinstance(raw, SourceMatch) and raw.direct_media_url:
        return raw
    confidence = _reverse_confidence(raw)
    preview = raw.get("preview_url") if isinstance(raw, dict) else getattr(raw, "preview_url", None)
    for url in _reverse_links(raw):
        resolved = _reverse_candidate_from_url(url, providers)
        if resolved is None:
            continue
        return replace(
            resolved,
            match_method="perceptual",
            confidence=confidence if confidence is not None else resolved.confidence,
            preview_url=resolved.preview_url or preview,
        )
    return raw


def _coerce_match(raw, method):
    if isinstance(raw, SourceMatch):
        return raw
    if not isinstance(raw, dict) or not raw.get("canonical_url"):
        return None
    try:
        confidence = float(raw.get("confidence", 100 if method == "exact" else 0))
    except (TypeError, ValueError):
        return None
    return SourceMatch(
        provider=str(raw.get("provider") or "unknown"),
        canonical_url=str(raw["canonical_url"]),
        direct_media_url=raw.get("direct_media_url"), remote_id=str(raw.get("remote_id")) if raw.get("remote_id") is not None else None,
        author=raw.get("author"), domain=raw.get("domain"), tags=tuple(raw.get("tags") or ()),
        content_md5=raw.get("content_md5"), match_method=str(raw.get("match_method") or method),
        confidence=confidence, preview_url=raw.get("preview_url"), width=raw.get("width"), height=raw.get("height"),
        character_tags=tuple(raw.get("character_tags") or raw.get("characters") or ()),
        parent_id=str(raw.get("parent_id")) if raw.get("parent_id") not in (None, "", 0, "0") else None,
    )


def _coerce_source(raw, canonical_url, provider_name):
    if isinstance(raw, SourceMedia):
        return raw
    if not isinstance(raw, dict):
        return raw
    direct_url = raw.get("direct_media_url") or raw.get("file_url")
    return SourceMedia(
        canonical_url=str(raw.get("canonical_url") or canonical_url),
        direct_media_url=direct_url,
        provider=str(raw.get("provider") or provider_name),
        remote_id=str(raw.get("remote_id") or ""),
        author=raw.get("author"),
        domain=str(raw.get("domain") or urlsplit(canonical_url).netloc),
        tags=tuple(raw.get("tags") or ()),
        file_extension=_extension(direct_url or canonical_url),
        character_tags=tuple(raw.get("character_tags") or ()),
        parent_id=raw.get("parent_id"),
        content_md5=raw.get("content_md5") or raw.get("md5"),
    )


def _unique_similar(matches):
    unique = {}
    for match in matches:
        if match is None:
            continue
        key = (match.provider, match.remote_id or match.canonical_url)
        if key not in unique or unique[key].confidence < match.confidence:
            unique[key] = match
    return sorted(unique.values(), key=lambda item: item.confidence, reverse=True)


def _metadata_richness(match: SourceMatch) -> tuple[int, int, int, int]:
    """Rank an exact match by how much source metadata it carries."""
    return (
        len(match.character_tags or ()),
        len(match.tags or ()),
        1 if match.author else 0,
        1 if match.parent_id else 0,
    )


def _select_metadata_source(matches):
    """Pick the richest metadata among exact matches.

    One file can be found on several providers.  The provider holding the
    authoritative tags/author is not necessarily the one whose bytes we end up
    downloading, so choose the richest metadata source and let the download
    provide the file from wherever it is actually available.
    """
    if not matches:
        return None
    return max(matches, key=_metadata_richness)


def _retain_failed_upload(
    connection,
    settings: Settings,
    job_id: int,
    submitted_input: str,
    input_kind: str,
    code: str,
    details: dict,
) -> bool:
    """Keep an inspected upload in staging and queue it for manual validation.

    A failed source resolution must never lose the file the user dropped into
    Jiffle.  When the upload itself is valid we turn the failure into a normal
    ``source_required`` review item so the image can be validated or given a
    source later.  Returns ``True`` when the job should finish as a review.
    """
    if input_kind != "file" or code in _RETAIN_EXCLUDED_CODES:
        return False
    row = connection.execute(
        "SELECT id, media_type, content_hash, width, height, file_size, status "
        "FROM import_candidates WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if row is None or row["media_type"] is None:
        return False
    if row["status"] in {"accepted", "duplicate", "review"}:
        return False
    source_path = Path(submitted_input)
    if not source_path.is_file():
        return False
    candidate_id = int(row["id"])
    relative = None
    try:
        connection.rollback()
        existing = connection.execute(
            "SELECT id FROM review_items WHERE import_candidate_id=?", (candidate_id,)
        ).fetchone()
        if existing is not None:
            return False
        relative = atomic_copy(source_path, settings.resolved_import_staging_path, "candidate")
        connection.execute(
            "UPDATE import_candidates SET status='review', stored_path=?, media_type=?, "
            "content_hash=?, width=?, height=?, file_size=? WHERE id=?",
            (relative, row["media_type"], row["content_hash"], row["width"],
             row["height"], row["file_size"], candidate_id),
        )
        cursor = connection.execute(
            "INSERT INTO review_items (import_candidate_id, reason) VALUES (?, 'source_required')",
            (candidate_id,),
        )
        connection.commit()
    except Exception:
        if relative:
            (settings.resolved_import_staging_path / relative).unlink(missing_ok=True)
        connection.rollback()
        return False
    details["retained_for_review"] = True
    details["review_item_id"] = int(cursor.lastrowid)
    return True


def _match_to_source(match: SourceMatch) -> SourceMedia:
    return SourceMedia(
        canonical_url=match.canonical_url, direct_media_url=match.direct_media_url or match.canonical_url,
        provider=match.provider, remote_id=match.remote_id or "", author=match.author,
        domain=match.domain or urlsplit(match.canonical_url).netloc, tags=match.tags,
        file_extension=_extension(match.direct_media_url or match.canonical_url),
        character_tags=match.character_tags, parent_id=match.parent_id,
        content_md5=match.content_md5,
    )


def _accept_downloaded(
    connection, settings, job_id, path, source, submitted_input,
    source_url_override=None, metadata_source=None, file_source=None,
):
    metadata_source = metadata_source or source
    file_source = file_source or source
    source_url = source_url_override or (
        metadata_source.canonical_url if metadata_source else (
            file_source.canonical_url if file_source else None
        )
    )
    inspection = inspect_media(path)
    existing = connection.execute(
        "SELECT id FROM media_items WHERE content_hash=? AND deleted_at IS NULL",
        (inspection.content_hash,),
    ).fetchone()
    candidate_id = _candidate_id(connection, job_id)
    if existing:
        if metadata_source:
            _store_source(connection, int(existing[0]), metadata_source)
        if source_url:
            connection.execute(
                "UPDATE media_items SET source_url=? WHERE id=?",
                (source_url, int(existing[0])),
            )
        if file_source:
            connection.execute(
                "UPDATE media_items SET file_source_url=? WHERE id=?",
                (file_source.canonical_url, int(existing[0])),
            )
        if metadata_source:
            connection.execute(
                "UPDATE import_candidates SET source_metadata_json=? WHERE id=?",
                (_serialize_source(metadata_source), candidate_id),
            )
        _set_candidate_result(connection, candidate_id, "duplicate", int(existing[0]))
        return {"outcome": "duplicate", "candidate_id": candidate_id, "media_item_id": int(existing[0])}

    perceptual_duplicate = find_exact_perceptual_duplicate(
        connection, settings, path, inspection
    )
    if perceptual_duplicate is not None:
        _set_candidate_result(connection, candidate_id, "duplicate", perceptual_duplicate)
        return {
            "outcome": "duplicate",
            "candidate_id": candidate_id,
            "media_item_id": perceptual_duplicate,
            "resolution_method": "local_perceptual_duplicate",
        }
    stored = atomic_copy(path, settings.media_path, "media")
    try:
        cursor = connection.execute(
            "INSERT INTO media_items (file_path, media_type, source_url, file_source_url, author, domain, width, height, file_size, content_hash, parent_id, character_tags_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (stored, inspection.media_type, source_url,
             file_source.canonical_url if file_source else None,
             metadata_source.author if metadata_source else None,
             metadata_source.domain if metadata_source else None,
             inspection.width, inspection.height, inspection.file_size, inspection.content_hash,
             metadata_source.parent_id if metadata_source else None,
             json.dumps(list(metadata_source.character_tags)) if metadata_source else "[]"),
        )
        media_id = int(cursor.lastrowid)
        create_original_revision(connection, media_id)
        if inspection.media_type == "image":
            try:
                with Image.open(path) as image:
                    fingerprint = str(imagehash.phash(image))
                connection.execute(
                    "INSERT INTO media_fingerprints (media_item_id, perceptual_hash) VALUES (?, ?)",
                    (media_id, fingerprint),
                )
            except (OSError, ValueError):
                pass
        if metadata_source:
            _store_source(connection, media_id, metadata_source)
            connection.executemany("INSERT OR IGNORE INTO media_tags (media_item_id, tag) VALUES (?, ?)", ((media_id, tag) for tag in metadata_source.tags))
            connection.execute(
                "UPDATE import_candidates SET source_metadata_json=? WHERE id=?",
                (_serialize_source(metadata_source), candidate_id),
            )
        _set_candidate_result(connection, candidate_id, "accepted", media_id, stored)
        return {"outcome": "accepted", "candidate_id": candidate_id, "media_item_id": media_id}
    except sqlite3.IntegrityError as error:
        if "media_items.content_hash" not in str(error):
            (settings.media_path / stored).unlink(missing_ok=True)
            connection.rollback()
            raise
        connection.rollback()
        existing = connection.execute(
            "SELECT id FROM media_items WHERE content_hash=? AND deleted_at IS NULL",
            (inspection.content_hash,),
        ).fetchone()
        if existing:
            (settings.media_path / stored).unlink(missing_ok=True)
            if metadata_source:
                _store_source(connection, int(existing[0]), metadata_source)
            if source_url:
                connection.execute(
                    "UPDATE media_items SET source_url=? WHERE id=?",
                    (source_url, int(existing[0])),
                )
            if file_source:
                connection.execute(
                    "UPDATE media_items SET file_source_url=? WHERE id=?",
                    (file_source.canonical_url, int(existing[0])),
                )
            if metadata_source:
                connection.execute(
                    "UPDATE import_candidates SET source_metadata_json=? WHERE id=?",
                    (_serialize_source(metadata_source), candidate_id),
                )
            _set_candidate_result(connection, candidate_id, "duplicate", int(existing[0]))
            return {"outcome": "duplicate", "candidate_id": candidate_id, "media_item_id": int(existing[0])}
        (settings.media_path / stored).unlink(missing_ok=True)
        raise
    except Exception:
        (settings.media_path / stored).unlink(missing_ok=True)
        connection.rollback()
        raise


def _store_source(connection, media_id, source):
    values = (media_id, source.canonical_url, source.direct_media_url, source.provider,
              source.remote_id, source.author, source.domain, source.parent_id,
              json.dumps(list(source.character_tags)))
    try:
        connection.execute(
            "INSERT INTO media_sources (media_item_id, canonical_url, direct_media_url, provider, remote_id, author, domain, parent_id, character_tags_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(media_item_id) DO UPDATE SET canonical_url=excluded.canonical_url, direct_media_url=excluded.direct_media_url, provider=excluded.provider, remote_id=excluded.remote_id, author=excluded.author, domain=excluded.domain, parent_id=excluded.parent_id, character_tags_json=excluded.character_tags_json",
            values,
        )
    except sqlite3.IntegrityError:
        owner = connection.execute(
            "SELECT media_item_id FROM media_sources WHERE canonical_url=?",
            (source.canonical_url,),
        ).fetchone()
        if owner is None or int(owner[0]) == int(media_id):
            raise
        deleted = connection.execute(
            "SELECT deleted_at FROM media_items WHERE id=?", (owner[0],)
        ).fetchone()
        if deleted is None or deleted[0] is None:
            raise
        target = connection.execute(
            "SELECT 1 FROM media_sources WHERE media_item_id=?", (media_id,)
        ).fetchone()
        if target:
            raise
        connection.execute(
            "UPDATE media_sources SET media_item_id=? WHERE media_item_id=?",
            (media_id, owner[0]),
        )
        connection.execute(
            "UPDATE media_sources SET direct_media_url=?, provider=?, remote_id=?, author=?, domain=?, parent_id=?, character_tags_json=? WHERE media_item_id=?",
            (source.direct_media_url, source.provider, source.remote_id, source.author,
             source.domain, source.parent_id, json.dumps(list(source.character_tags)), media_id),
        )
    connection.execute(
        "UPDATE media_items SET source_url=?, author=?, domain=?, parent_id=?, character_tags_json=? WHERE id=?",
        (source.canonical_url, source.author, source.domain, source.parent_id,
         json.dumps(list(source.character_tags)), media_id),
    )


def _serialize_source(source: SourceMedia) -> str:
    return json.dumps({
        "canonical_url": source.canonical_url,
        "direct_media_url": source.direct_media_url,
        "provider": source.provider,
        "remote_id": source.remote_id,
        "author": source.author,
        "domain": source.domain,
        "tags": list(source.tags),
        "character_tags": list(source.character_tags),
        "parent_id": source.parent_id,
        "file_extension": source.file_extension,
        "content_md5": source.content_md5,
    })


def _decode_json_tags(raw) -> tuple[str, ...]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    return tuple(str(value) for value in values if str(value).strip()) if isinstance(values, (list, tuple)) else ()


def _existing_source(connection, canonical_url):
    row = connection.execute(
        "SELECT item.id FROM media_items item "
        "LEFT JOIN media_sources source ON source.media_item_id=item.id "
        "WHERE item.deleted_at IS NULL "
        "AND (item.source_url=? OR item.file_source_url=? OR source.canonical_url=?) LIMIT 1",
        (canonical_url, canonical_url, canonical_url),
    ).fetchone()
    return int(row[0]) if row else None


def _candidate_id(connection, job_id):
    row = connection.execute("SELECT id FROM import_candidates WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        raise RuntimeError("Import candidate is missing")
    return int(row[0])


def _update_candidate(connection, candidate_id, inspection, stored_path, status):
    connection.execute("UPDATE import_candidates SET media_type=?, content_hash=?, width=?, height=?, file_size=?, stored_path=?, status=? WHERE id=?", (inspection.media_type, inspection.content_hash, inspection.width, inspection.height, inspection.file_size, stored_path, status, candidate_id))
    connection.commit()


def _set_candidate_result(connection, candidate_id, status, media_id, stored_path=None):
    connection.execute("UPDATE import_candidates SET status=?, media_item_id=?, stored_path=COALESCE(?, stored_path) WHERE id=?", (status, media_id, stored_path, candidate_id))
    connection.commit()


def _running(connection, job_id, message: str = "Starting the import"):
    connection.execute(
        "UPDATE background_jobs SET status='running', progress=10, status_message=?, "
        "started_at=CURRENT_TIMESTAMP WHERE id=?",
        (message, job_id),
    )
    connection.commit()


def _progress(connection, job_id, percent: int, message: str | None = None) -> None:
    """Publish a coarse stage so a long import never looks like a frozen job."""
    if message is None:
        connection.execute(
            "UPDATE background_jobs SET progress=? WHERE id=?",
            (int(percent), job_id),
        )
    else:
        connection.execute(
            "UPDATE background_jobs SET progress=?, status_message=? WHERE id=?",
            (int(percent), message, job_id),
        )
    connection.commit()


def _finish(connection, job_id, result, details):
    _finalize_timing(details)
    payload = dict(result)
    if details.get("resolution_method") and "resolution_method" not in payload:
        payload["resolution_method"] = details["resolution_method"]
    payload["resolution"] = details
    connection.execute("UPDATE background_jobs SET status='completed', progress=100, result_json=?, finished_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(payload), job_id))
    update_import_history(connection, job_id, result.get("outcome", "failed"), {**details, **result})
    connection.commit()


def _failed(connection, job_id, code, message, details):
    _finalize_timing(details)
    connection.rollback()
    connection.execute(
        "UPDATE background_jobs SET status='failed', error_code=?, error_message=?, result_json=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
        (code, message, json.dumps({"outcome": "failed", "code": code, "message": message, "resolution": details}), job_id),
    )
    connection.execute("UPDATE import_candidates SET status='failed' WHERE job_id=?", (job_id,))
    update_import_history(connection, job_id, "failed", {**details, "code": code, "message": message})
    connection.commit()


def _md5(path):
    digest = hashlib.md5()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _elapsed_ms(started):
    return max(0, int(round((time.perf_counter() - started) * 1000)))


def _finalize_timing(details):
    started = details.pop("_started_at", None)
    if started is not None:
        timing = details.setdefault("timing", {"duration_ms": 0, "phases_ms": {}})
        timing["duration_ms"] = _elapsed_ms(started)


def _extension(url):
    suffix = Path(urlsplit(str(url)).path).suffix.lower()
    return suffix or ".jpg"
