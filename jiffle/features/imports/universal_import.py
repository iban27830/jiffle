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
from threading import Event, Lock
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import imagehash
from PIL import Image

from jiffle.configuration.settings import Settings
from jiffle.features.duplicates.scan import (
    DEFAULT_MATCH_THRESHOLD,
    find_similar_media,
    register_pending_match,
)
from jiffle.features.imports.history import create_import_history, update_import_history
from jiffle.features.imports.local_import import (
    ImportFailure,
    _safe_media_path,
    atomic_copy,
    find_exact_perceptual_duplicate,
    inspect_media,
)
from jiffle.features.imports.source_adapters.contracts import SourceMedia, SourceMatch
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.features.imports.source_adapters.reverse_search import reverse_preview_bytes
from jiffle.features.imports.url_normalization import normalize_source_url
from jiffle.features.shelves.workflow import (
    assign_shelves_to_media,
    parse_shelf_ids,
    resolve_shelf_summaries,
)
from jiffle.infrastructure.database.connection import connect_database
from jiffle.infrastructure.media_revisions import create_original_revision


MIN_SIMILAR_CONFIDENCE = 80.0

# Perceptual lookups contact remote services that can be slow or unreachable.
# They run in parallel under one deadline, like the exact-search phase.
REVERSE_SEARCH_TIMEOUT_SECONDS = 20.0
REVERSE_RESOLVE_TIMEOUT_SECONDS = 15.0
EXACT_DOWNLOAD_TIMEOUT_SECONDS = 30.0
CANDIDATE_DOWNLOAD_TIMEOUT_SECONDS = 35.0
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

_CODE_SEARCH_SIGNATURE: str | None = None
_CODE_SIGNATURE_LOCK = Lock()

_SEARCH_SETTINGS_FIELDS = (
    "danbooru_login",
    "danbooru_api_key",
    "e621_login",
    "e621_api_key",
    "gelbooru_user_id",
    "gelbooru_api_key",
    "rule34_user_id",
    "rule34_api_key",
    "furaffinity_cookie_a",
    "furaffinity_cookie_b",
    "saucenao_api_key",
)


def _code_search_signature() -> str:
    """Return a fast digest of the source-lookup code files, computed once per process."""
    global _CODE_SEARCH_SIGNATURE
    if _CODE_SEARCH_SIGNATURE is not None:
        return _CODE_SEARCH_SIGNATURE
    with _CODE_SIGNATURE_LOCK:
        if _CODE_SEARCH_SIGNATURE is not None:
            return _CODE_SEARCH_SIGNATURE
        imports_dir = Path(__file__).resolve().parent
        adapters_dir = imports_dir / "source_adapters"
        review_workflow = imports_dir.parent / "review_queue" / "workflow.py"
        files: list[Path] = []
        if adapters_dir.is_dir():
            files.extend(sorted(adapters_dir.glob("*.py")))
        for extra in (
            imports_dir / "universal_import.py",
            imports_dir / "url_normalization.py",
            review_workflow,
        ):
            if extra.is_file():
                files.append(extra)
        hasher = hashlib.blake2b(digest_size=8)
        for path in files:
            try:
                hasher.update(path.name.encode("utf-8"))
                hasher.update(b"\0")
                hasher.update(path.read_bytes())
                hasher.update(b"\0")
            except OSError:
                continue
        _CODE_SEARCH_SIGNATURE = hasher.hexdigest()
        return _CODE_SEARCH_SIGNATURE


def _provider_signature_token(provider: object) -> dict[str, object]:
    name = (
        getattr(provider, "provider_name", None)
        or provider.__class__.__name__.lower()
    )
    configured = getattr(provider, "is_configured", True)
    if callable(configured):
        try:
            configured = bool(configured())
        except Exception:
            configured = False
    token: dict[str, object] = {
        "name": str(name),
        "class": f"{provider.__class__.__module__}.{provider.__class__.__qualname__}",
        "configured": bool(configured),
        "supports_reverse": bool(getattr(provider, "supports_reverse_search", True)),
        "reverse_available": bool(_reverse_provider_available(provider)),
    }
    for attr in ("login", "api_key", "user_id", "cookie_a", "cookie_b", "hash_algorithm"):
        val = getattr(provider, attr, None)
        if val not in (None, ""):
            token[attr] = str(val)
    for test_attr in ("exact", "similar"):
        if hasattr(provider, test_attr):
            token[test_attr] = repr(getattr(provider, test_attr))
    return token


def _settings_search_signature(
    settings: Settings | None = None,
    providers=(),
) -> str:
    payload = {
        "settings": {
            field: (getattr(settings, field, None) or "")
            for field in _SEARCH_SETTINGS_FIELDS
        }
        if settings is not None
        else {},
        "providers": [
            _provider_signature_token(provider)
            for provider in (providers or ())
            if provider is not None
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.blake2b(encoded, digest_size=8).hexdigest()


def current_search_signature(
    settings: Settings | None = None,
    providers=(),
) -> str:
    """Return a compact ``<code_hash>:<settings_hash>`` signature for source searches."""
    return f"{_code_search_signature()}:{_settings_search_signature(settings, providers)}"


def sync_search_signature(
    settings: Settings | None = None,
    providers=(),
) -> tuple[str, bool]:
    """Compare the current search signature against the persisted marker file.

    Returns ``(signature, changed)``.
    """
    current = current_search_signature(settings, providers)
    if settings is None or getattr(settings, "database_path", None) is None:
        return current, False
    if not getattr(settings, "initialize_database", True):
        return current, False
    marker = Path(settings.database_path).parent / ".search-signature"
    previous = None
    try:
        if marker.is_file():
            previous = marker.read_text(encoding="utf-8").strip()
    except OSError:
        previous = None
    changed = previous != current
    if changed:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(current, encoding="utf-8")
        except OSError:
            pass
    return current, changed


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


def _mark_provider_failure(name: str) -> None:
    _mark_provider_cooldown(name)


def _clear_provider_cooldown(name: str) -> None:
    with _provider_cooldowns_lock:
        _provider_cooldowns.pop(name, None)


def _provider_needs_configuration(provider) -> bool:
    """True when a provider declares that it has no usable credentials yet."""
    configured = getattr(provider, "is_configured", True)
    if callable(configured):
        configured = configured()
    return not configured


def _provider_search_digest(provider, md5_digest: str | None, sha256_digest: str | None) -> str | None:
    """Pick the fingerprint a provider's exact search expects.

    Every booru indexes posts by MD5 and keeps the default.  Pawchive stores
    files by SHA-256, so it declares ``hash_algorithm = "sha256"`` and is
    answered with the SHA-256 the import already computed for the file.
    """
    algorithm = str(getattr(provider, "hash_algorithm", "md5") or "md5").strip().lower()
    if algorithm == "sha256":
        return sha256_digest
    return md5_digest


def _reverse_provider_available(provider) -> bool:
    """Whether a provider's perceptual search can run in the current setup.

    Some reverse-search endpoints require an account even though their ordinary
    metadata lookup is anonymous (e621 and Danbooru).  They must be reported as
    not configured instead of silently returning no results.
    """
    if getattr(provider, "reverse_search_requires_auth", False):
        return bool(getattr(provider, "login", None)) and bool(
            getattr(provider, "api_key", None)
        )
    return not _provider_needs_configuration(provider)


# Failures that leave nothing useful to validate manually.  For these the job
# stays failed (the uploaded bytes are still kept on disk by the import worker).
_RETAIN_EXCLUDED_CODES = {
    "import.previously_deleted",
    "import.unsupported_media_type",
    "import.invalid_media",
    "import.file_not_found",
    "import.video_support_unavailable",
}

# Rule34 and Gelbooru record the uploaded file's MD5 in ``hash``/``md5`` and
# then re-encode or remux video posts to MP4 on their CDN, so the served video
# bytes never match ``content_md5``.
_TRANSCODED_VIDEO_PROVIDERS = {"rule34", "gelbooru"}


def _allows_transcoded_source_media(
    provider: str | None, media_url: str | None, path: Path
) -> bool:
    """True when a provider re-encodes videos after recording the upload MD5."""
    if str(provider or "").strip().lower() not in _TRANSCODED_VIDEO_PROVIDERS:
        return False
    ext = _extension(media_url or path.name)
    if ext not in {".mp4", ".webm"} and path.suffix.lower() not in {".mp4", ".webm"}:
        return False
    try:
        return inspect_media(path).media_type == "video"
    except Exception:
        return False


def _normalize_tag_token(value: object) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def filter_imported_tags(
    connection: sqlite3.Connection,
    tags,
    character_tags=(),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Normalize tags, map aliases, drop blocked tags, and deduplicate."""
    blocked: set[str] = set()
    aliases: dict[str, str] = {}
    try:
        blocked = {
            _normalize_tag_token(row[0])
            for row in connection.execute(
                "SELECT LOWER(tag) FROM tag_rules WHERE disposition='blocked'"
            ).fetchall()
            if _normalize_tag_token(row[0])
        }
    except sqlite3.OperationalError:
        blocked = set()
    try:
        for row in connection.execute(
            "SELECT LOWER(alias) AS alias, LOWER(canonical_tag) AS canonical_tag FROM tag_aliases"
        ).fetchall():
            alias_key = _normalize_tag_token(row[0])
            canonical_val = _normalize_tag_token(row[1])
            if alias_key and canonical_val:
                aliases[alias_key] = canonical_val
    except sqlite3.OperationalError:
        aliases = {}

    def _process(seq) -> tuple[str, ...]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in seq or ():
            tag = _normalize_tag_token(raw)
            if not tag or tag in blocked:
                continue
            mapped = aliases.get(tag, tag)
            if not mapped or mapped in blocked or mapped in seen:
                continue
            seen.add(mapped)
            result.append(mapped)
        return tuple(result)

    return _process(tags), _process(character_tags)


def filter_source_tags(
    connection: sqlite3.Connection, source: SourceMedia | None
) -> SourceMedia | None:
    if source is None:
        return None
    tags, character_tags = filter_imported_tags(
        connection, source.tags, source.character_tags
    )
    return replace(source, tags=tags, character_tags=character_tags)


def _clean_original_name(name: str | None) -> str:
    raw = Path(str(name or "").strip()).name
    cleaned = re.sub(
        r"^(?:upload|candidate|media|resolve)-[0-9a-fA-F]{32}-",
        "",
        raw,
    )
    return cleaned or raw or "import"


def _filename_hash_hints(
    original_name: str | None, fallback_name: str | None = None
) -> tuple[str | None, str | None]:
    text = _clean_original_name(original_name or fallback_name or "")
    text = re.sub(
        r"^(?:upload|candidate|media|resolve)-[0-9a-fA-F]{32}\b[_.-]*",
        "",
        text,
    )
    stem = Path(text).stem
    if not stem:
        return None, None
    sha_match = re.search(r"(?<![0-9a-fA-F])([0-9a-fA-F]{64})(?![0-9a-fA-F])", stem)
    sha256_hint = sha_match.group(1).lower() if sha_match else None
    md5_match = re.search(r"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])", stem)
    md5_hint = md5_match.group(1).lower() if md5_match else None
    return md5_hint, sha256_hint


def create_universal_import_job(
    connection: sqlite3.Connection,
    submitted_input: str,
    input_kind: str,
    original_name: str | None = None,
    shelf_ids: object = (),
    commit: bool = True,
) -> int:
    cursor = connection.execute(
        "INSERT INTO background_jobs (job_type, status) VALUES ('import_resolve', 'pending')"
    )
    job_id = int(cursor.lastrowid)
    display_name = (
        _clean_original_name(original_name or Path(submitted_input).name)
        if input_kind == "file"
        else (original_name or Path(submitted_input).name or "import")
    )
    parsed_shelves = parse_shelf_ids(shelf_ids)
    connection.execute(
        "INSERT INTO import_candidates (job_id, source_path, original_name, status, shelf_ids_json) "
        "VALUES (?, ?, ?, 'pending', ?)",
        (job_id, submitted_input, display_name, json.dumps(list(parsed_shelves))),
    )
    history_payload: dict[str, object] = {
        "submitted_input": submitted_input,
        "input_kind": input_kind,
        "resolution_method": "pending",
        "exact_candidates_checked": 0,
        "similar_candidates_found": 0,
        "search_status": "no_result",
        "search_state": "no_result",
        "search_outcome": "no_result",
        "provider_diagnostics": [],
    }
    if parsed_shelves:
        shelves_summary = resolve_shelf_summaries(connection, parsed_shelves)
        if shelves_summary:
            history_payload["shelves"] = shelves_summary
    create_import_history(connection, job_id, history_payload)
    if commit:
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
        "search_signature": current_search_signature(settings, providers),
        "timing": {"duration_ms": 0, "phases_ms": {}},
    }
    started_at = time.perf_counter()
    details["_started_at"] = started_at
    try:
        _running(connection, job_id)
        source_path = Path(submitted_input)
        original_path: Path | None = None
        metadata_source: SourceMedia | None = None
        file_source: SourceMedia | None = None
        blocked_source = None
        is_direct_url = False
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
            is_direct_url = source is not None and source.provider == "direct"
            details["provider_errors"] = metadata_errors
            if metadata_errors:
                _set_search_status(details, _status_from_errors(metadata_errors))
            if source is not None:
                blocked_source = blocked_source or connection.execute(
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
                    if (
                        source.content_md5
                        and _md5(original_path) != source.content_md5
                        and not _allows_transcoded_source_media(
                            source.provider, source.direct_media_url, original_path
                        )
                    ):
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
            if original_path is not None and not is_direct_url:
                temporary.append(original_path)
                inspection = inspect_media(original_path)
                digest = _md5(original_path)
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
                source_md5 = (
                    (source.content_md5 if source else None)
                    or digest
                    or inspection.content_hash
                )
                blocked_hash = connection.execute(
                    "SELECT 1 FROM blocked_media_signatures WHERE content_hash IN (?, ?, ?)",
                    (inspection.content_hash, digest or inspection.content_hash, source_md5),
                ).fetchone()
                if blocked_source or blocked_hash:
                    if settings.block_previously_deleted:
                        raise ImportFailure(
                            "import.previously_deleted",
                            "This media was previously deleted and is blocked by settings.",
                        )
                    _set_search_status(details, "resolved")
                    _stage_previously_deleted_review(
                        connection, settings, job_id, candidate_id, original_path,
                        inspection, source, details,
                    )
                    return
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
            if original_path is not None and is_direct_url:
                temporary.append(original_path)
                digest = _md5(original_path)
                source_hint = None
                metadata_source = None
                file_source = source
            else:
                digest = source.content_md5 if source else None
                if not digest and is_direct_url:
                    hint_md5, _ = _filename_hash_hints(
                        Path(urlsplit(submitted_input).path).name
                    )
                    digest = hint_md5
                source_hint = source if not is_direct_url else None
                metadata_source = source_hint
        else:
            if not source_path.is_file():
                raise ImportFailure("import.file_not_found", "The selected file does not exist.")
            source_hint = None
            metadata_source = None
            digest = _md5(source_path)
            original_path = source_path

        if input_kind == "url" and original_path is None:
            if is_direct_url and not digest:
                failure = _first_download_failure(details.get("provider_errors"))
                if failure:
                    raise ImportFailure(*failure)
                raise ImportFailure("import.download_unavailable", "The media source was unavailable.")
            if not digest:
                raise ImportFailure("import.source_media_missing", "The source has no downloadable media or file hash.")
            blocked_digest = connection.execute(
                "SELECT 1 FROM blocked_media_signatures WHERE content_hash=?", (digest,)
            ).fetchone()
            if (blocked_source or blocked_digest) and settings.block_previously_deleted:
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
            try:
                for match, downloaded in valid_candidates:
                    try:
                        file_source = _match_to_source(match)
                        cand_inspection = inspect_media(downloaded)
                        pending_review = connection.execute(
                            "SELECT review.id FROM review_items review "
                            "JOIN import_candidates candidate ON candidate.id=review.import_candidate_id "
                            "WHERE review.status='pending' AND candidate.content_hash=? LIMIT 1",
                            (cand_inspection.content_hash,),
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
                        blocked_cand = (
                            blocked_source
                            or blocked_digest
                            or connection.execute(
                                "SELECT 1 FROM blocked_media_signatures WHERE content_hash IN (?, ?) OR source_url=?",
                                (
                                    cand_inspection.content_hash,
                                    digest or cand_inspection.content_hash,
                                    match.canonical_url,
                                ),
                            ).fetchone()
                        )
                        if blocked_cand:
                            if settings.block_previously_deleted:
                                raise ImportFailure(
                                    "import.previously_deleted",
                                    "This media was previously deleted and is blocked by settings.",
                                )
                            details["resolved_source_url"] = match.canonical_url
                            _set_search_status(details, "resolved")
                            _stage_previously_deleted_review(
                                connection,
                                settings,
                                job_id,
                                candidate_id,
                                downloaded,
                                cand_inspection,
                                metadata_source or file_source,
                                details,
                            )
                            return
                        result = _accept_downloaded(
                            connection, settings, job_id, downloaded, file_source, submitted_input,
                            source_url_override=metadata_source.canonical_url if metadata_source else normalized_input,
                            metadata_source=metadata_source or file_source,
                            file_source=file_source,
                        )
                    except ImportFailure as error:
                        if error.code == "import.previously_deleted":
                            raise
                        code, message = _download_failure(error, "The exact candidate was unavailable.")
                        details.setdefault("provider_errors", []).append({"provider": match.provider, "code": code, "message": message, "remote_id": match.remote_id})
                        _record_provider_diagnostic(details["provider_diagnostics"], "exact_download", match.provider, "unavailable", 0, code, message, remote_id=match.remote_id)
                        continue
                    except Exception as error:
                        code, message = _download_failure(error, "The exact candidate was unavailable.")
                        details.setdefault("provider_errors", []).append({"provider": match.provider, "code": code, "message": message, "remote_id": match.remote_id})
                        _record_provider_diagnostic(details["provider_diagnostics"], "exact_download", match.provider, "unavailable", 0, code, message, remote_id=match.remote_id)
                        continue
                    finally:
                        downloaded.unlink(missing_ok=True)
                    details.update({
                        "resolution_method": result.get("resolution_method", "exact"),
                        "resolved_source_url": match.canonical_url,
                    })
                    _set_search_status(details, "resolved")
                    _finish(connection, job_id, result, details)
                    return
            finally:
                for _, other in valid_candidates:
                    other.unlink(missing_ok=True)
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
            "SELECT 1 FROM blocked_media_signatures WHERE content_hash IN (?, ?)",
            (inspection.content_hash, digest or inspection.content_hash),
        ).fetchone()
        if (blocked_source or blocked_hash) and settings.block_previously_deleted:
            raise ImportFailure("import.previously_deleted", "This media was previously deleted and is blocked by settings.")

        # A local file that is already in the library is answered from the
        # local fingerprints before any provider is contacted: an unreachable
        # provider must not make a known file look like it is still importing.
        if not (blocked_source or blocked_hash) and (input_kind != "url" or is_direct_url):
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
        exact_verify_digest = digest
        exact, errors = _search_exact(
            providers, digest, source_hint, details["provider_timings"], details["provider_diagnostics"],
            on_progress=lambda names: _progress(
                connection, job_id, 50, "Waiting for sources: " + ", ".join(sorted(names))
            ),
            sha256_digest=inspection.content_hash,
        )
        if not exact and (input_kind == "file" or is_direct_url):
            if input_kind == "file":
                cand_row = connection.execute(
                    "SELECT original_name FROM import_candidates WHERE id=?",
                    (candidate_id,),
                ).fetchone()
                orig_name = cand_row["original_name"] if cand_row else source_path.name
                fallback_name = source_path.name
            else:
                orig_name = Path(urlsplit(submitted_input).path).name
                fallback_name = original_path.name if original_path is not None else ""
            hint_md5, hint_sha256 = _filename_hash_hints(orig_name, fallback_name)
            query_md5 = hint_md5 if (hint_md5 and hint_md5 != digest) else None
            query_sha256 = (
                hint_sha256
                if (hint_sha256 and hint_sha256 != inspection.content_hash)
                else None
            )
            if query_md5 or query_sha256:
                hint_exact, hint_errors = _search_exact(
                    providers,
                    query_md5 or "",
                    source_hint,
                    details["provider_timings"],
                    details["provider_diagnostics"],
                    sha256_digest=query_sha256,
                )
                errors = errors + hint_errors
                if hint_exact:
                    exact = hint_exact
                    if query_md5:
                        exact_verify_digest = query_md5
        details["timing"]["phases_ms"]["exact_search"] = _elapsed_ms(exact_started)
        details["exact_candidates_checked"] = len(exact)
        details["provider_errors"] = list(details.get("provider_errors", [])) + errors
        if metadata_source is None and exact:
            best_metadata = _select_metadata_source(exact)
            if best_metadata is not None:
                metadata_source = _match_to_source(best_metadata)
        _set_search_status(details, "matched" if exact else _status_from_errors(errors))
        if is_direct_url and metadata_source is not None and exact_verify_digest == digest:
            blocked_meta = (
                blocked_source
                or blocked_hash
                or connection.execute(
                    "SELECT 1 FROM blocked_media_signatures WHERE source_url=?",
                    (metadata_source.canonical_url,),
                ).fetchone()
            )
            if blocked_meta:
                if settings.block_previously_deleted:
                    raise ImportFailure(
                        "import.previously_deleted",
                        "This source was previously deleted and is blocked by settings.",
                    )
                _set_search_status(details, "resolved")
                _stage_previously_deleted_review(
                    connection,
                    settings,
                    job_id,
                    candidate_id,
                    original_path,
                    inspection,
                    metadata_source,
                    details,
                )
                return
            result = _accept_downloaded(
                connection,
                settings,
                job_id,
                original_path,
                metadata_source,
                submitted_input,
                source_url_override=metadata_source.canonical_url,
                metadata_source=metadata_source,
                file_source=file_source or metadata_source,
            )
            details.update({
                "resolution_method": result.get("resolution_method", "exact"),
                "resolved_source_url": metadata_source.canonical_url,
            })
            _set_search_status(details, "resolved")
            _finish(connection, job_id, result, details)
            return
        _progress(connection, job_id, 70 if exact else 65, "Checking source files")
        download_started = time.perf_counter()
        valid_candidates, download_errors = _download_exact_candidates(
            exact, exact_verify_digest, settings, downloader, details["provider_diagnostics"]
        )
        details["timing"]["phases_ms"]["exact_download"] = _elapsed_ms(download_started)
        details["provider_errors"] = list(details.get("provider_errors", [])) + download_errors
        try:
            for match, downloaded in valid_candidates:
                try:
                    temporary.append(downloaded)
                    file_source = _match_to_source(match)
                    resolved_meta = metadata_source or file_source
                    accepted_path = (
                        original_path
                        if (
                            input_kind == "file"
                            and original_path is not None
                            and exact_verify_digest == digest
                            and _md5(downloaded) != digest
                        )
                        else downloaded
                    )
                    cand_inspection = inspect_media(accepted_path)
                    blocked_resolved = (
                        blocked_source
                        or blocked_hash
                        or connection.execute(
                            "SELECT 1 FROM blocked_media_signatures WHERE source_url IN (?, ?) OR content_hash=?",
                            (resolved_meta.canonical_url, file_source.canonical_url, cand_inspection.content_hash),
                        ).fetchone()
                    )
                    if blocked_resolved:
                        if settings.block_previously_deleted:
                            raise ImportFailure(
                                "import.previously_deleted",
                                "This source was previously deleted and is blocked by settings.",
                            )
                        details["resolved_source_url"] = match.canonical_url
                        _set_search_status(details, "resolved")
                        _stage_previously_deleted_review(
                            connection,
                            settings,
                            job_id,
                            candidate_id,
                            accepted_path,
                            cand_inspection,
                            resolved_meta,
                            details,
                        )
                        return
                    result = _accept_downloaded(
                        connection, settings, job_id, accepted_path, file_source, submitted_input,
                        metadata_source=resolved_meta,
                        file_source=file_source,
                    )
                except ImportFailure as error:
                    if error.code == "import.previously_deleted":
                        raise
                    downloaded.unlink(missing_ok=True)
                    code, message = _download_failure(error, "The exact candidate was unavailable.")
                    details.setdefault("provider_errors", []).append({"provider": match.provider, "code": code, "message": message, "remote_id": match.remote_id})
                    _record_provider_diagnostic(details["provider_diagnostics"], "exact_download", match.provider, "unavailable", 0, code, message, remote_id=match.remote_id)
                    continue
                except Exception as error:
                    downloaded.unlink(missing_ok=True)
                    code, message = _download_failure(error, "The exact candidate was unavailable.")
                    details.setdefault("provider_errors", []).append({"provider": match.provider, "code": code, "message": message, "remote_id": match.remote_id})
                    _record_provider_diagnostic(details["provider_diagnostics"], "exact_download", match.provider, "unavailable", 0, code, message, remote_id=match.remote_id)
                    continue
                finally:
                    if downloaded in temporary:
                        temporary.remove(downloaded)
                details.update({
                    "resolution_method": result.get("resolution_method", "exact"),
                    "resolved_source_url": match.canonical_url,
                })
                _set_search_status(details, "resolved")
                _finish(connection, job_id, result, details)
                return
        finally:
            for _, downloaded in valid_candidates:
                downloaded.unlink(missing_ok=True)

        if exact and (download_errors or details.get("search_status") == "matched"):
            _set_search_status(details, "candidate_download_failed")
        elif details.get("search_status") not in {"network_error", "authorization_error"}:
            _set_search_status(details, _status_from_errors(errors))

        if blocked_source or blocked_hash:
            _stage_previously_deleted_review(
                connection,
                settings,
                job_id,
                candidate_id,
                original_path,
                inspection,
                metadata_source or file_source,
                details,
            )
            return

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
        staged_committed = False
        staged_source_metadata = metadata_source or file_source
        try:
            similar = _local_similar(connection, settings, original_path, inspection)
            _record_provider_diagnostic(
                details["provider_diagnostics"], "perceptual_search", "local",
                "matched" if similar else "no_result", len(similar)
            )
            # Videos are searched through one representative frame, so a re-encoded
            # or sample-quality copy can still match the post it was taken from even
            # though its bytes differ from the original.
            _progress(connection, job_id, 75, "Running reverse image search")
            for raw_match in _reverse_similar(
                original_path, providers, details["provider_diagnostics"], settings=settings
            ):
                if isinstance(raw_match, SourceMatch):
                    similar.append(raw_match)
                else:
                    coerced = _coerce_match(raw_match, "perceptual")
                    if coerced is not None and coerced.confidence >= _reverse_min_confidence_by_name(
                        coerced.provider, providers
                    ):
                        similar.append(coerced)
            similar = _unique_similar(similar)
            details["similar_candidates_found"] = len(similar)
            if similar:
                _progress(connection, job_id, 85, "Downloading source candidates")
            candidate_paths.extend(
                _download_similar_candidates(
                    similar, settings, downloader, details["provider_diagnostics"]
                )
            )
            if candidate_paths:
                candidate_paths.sort(key=lambda item: item[0].confidence, reverse=True)
                connection.execute(
                    "UPDATE import_candidates SET status='review', stored_path=?, media_type=?, "
                    "content_hash=?, width=?, height=?, file_size=?, source_metadata_json=? WHERE id=?",
                    (original_staged, inspection.media_type, inspection.content_hash,
                     inspection.width, inspection.height, inspection.file_size,
                     _serialize_source(staged_source_metadata) if staged_source_metadata else None, candidate_id),
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
                staged_committed = True
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
                 _serialize_source(staged_source_metadata) if staged_source_metadata else None, candidate_id),
            )
            review_cursor = connection.execute(
                "INSERT INTO review_items (import_candidate_id, reason) VALUES (?, 'source_required')",
                (candidate_id,),
            )
            connection.commit()
            staged_committed = True
            details["resolution_method"] = "source_required"
            _finish(connection, job_id, {"outcome": "review", "candidate_id": candidate_id,
                                         "review_item_id": int(review_cursor.lastrowid)}, details)
        except Exception:
            if not staged_committed:
                (settings.resolved_import_staging_path / original_staged).unlink(missing_ok=True)
                for _match, path, _inspection in candidate_paths:
                    path.unlink(missing_ok=True)
                connection.rollback()
            raise
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
        try:
            job_row = connection.execute(
                "SELECT status, result_json FROM background_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if job_row is not None and job_row["status"] in {"pending", "running"}:
                _failed(
                    connection,
                    job_id,
                    "import.resolve_failed",
                    "The input could not be resolved.",
                    details,
                )
            elif job_row is not None:
                pending_hist = connection.execute(
                    "SELECT id FROM operation_history "
                    "WHERE entity_type='background_job' AND entity_id=? AND event_type='import.pending'",
                    (job_id,),
                ).fetchone()
                if pending_hist is not None:
                    outcome = "failed"
                    if job_row["status"] == "completed" and job_row["result_json"]:
                        try:
                            outcome = json.loads(job_row["result_json"]).get("outcome") or "accepted"
                        except Exception:
                            outcome = "accepted"
                    update_import_history(connection, job_id, outcome, details)
                    connection.commit()
        except Exception:
            pass
        if input_kind == "file":
            row = connection.execute(
                "SELECT candidate.status, candidate.stored_path, job.error_code "
                "FROM import_candidates candidate JOIN background_jobs job ON job.id=candidate.job_id "
                "WHERE candidate.job_id=?",
                (job_id,),
            ).fetchone()
            upload_path = Path(submitted_input)
            is_staged_upload = upload_path.parent.resolve() == settings.resolved_import_staging_path.resolve() and upload_path.name.startswith("upload-")
            stored_path = row["stored_path"] if row is not None else None
            has_durable_replacement = bool(stored_path) and stored_path != upload_path.name
            is_rejected_upload = (
                row is not None
                and row["status"] == "failed"
                and not has_durable_replacement
                and row["error_code"] in _RETAIN_EXCLUDED_CODES
            )
            # Keep the uploaded bytes until they are safely stored in the library
            # (accepted/duplicate) or a durable staged copy exists for review.
            # Failed imports keep the upload on disk so nothing the user dropped
            # into Jiffle is ever lost when the source is unavailable.
            if row and is_staged_upload and (
                row["status"] in {"accepted", "duplicate"}
                or has_durable_replacement
                or is_rejected_upload
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
    sha256_digest: str | None = None,
    skip_providers=None,
):
    skip_set = {str(item).strip().lower() for item in (skip_providers or ()) if item}
    ordered = list(providers)
    if source_hint:
        ordered.sort(key=lambda p: 0 if getattr(p, "provider_name", "") == source_hint.provider else 1)
    searchable = []
    for index, provider in enumerate(ordered):
        if not callable(getattr(provider, "search_by_md5", None)):
            continue
        provider_digest = _provider_search_digest(provider, digest, sha256_digest)
        # A provider that wants a fingerprint we do not have for this import
        # (a SHA-256 provider reached only with a source MD5) is skipped
        # silently instead of being reported as an error.
        if provider_digest:
            searchable.append((index, provider, provider_digest))

    def lookup(position, provider, provider_digest):
        started = time.perf_counter()
        try:
            raw_matches = getattr(provider, "search_by_md5")(provider_digest) or []
            matches = [_coerce_match(raw, "exact") for raw in raw_matches]
            matches = [match for match in matches if match]
            return position, matches, [], _elapsed_ms(started), "ok"
        except SourceProviderFailure as error:
            return position, [], [{"provider": getattr(provider, "provider_name", "unknown"), "code": error.code, "message": _safe_error_message(error.message, "The source search failed.")}], _elapsed_ms(started), "error"
        except Exception as error:
            return position, [], [{"provider": getattr(provider, "provider_name", "unknown"), "code": "import.source_search_failed", "message": _safe_error_message(error, "The source search failed.")}], _elapsed_ms(started), "error"

    results = []
    active = []
    for position, (_index, provider, _digest) in enumerate(searchable):
        name = getattr(provider, "provider_name", "unknown")
        if str(name).strip().lower() in skip_set:
            results.append((position, [], [], 0, "already_checked"))
        elif _provider_on_cooldown(name):
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
            active.append((position, provider, _digest))

    executor = ThreadPoolExecutor(max_workers=max(1, len(active))) if active else None
    if executor is not None:
        futures = {
            executor.submit(lookup, position, provider, provider_digest): (position, provider)
            for position, provider, provider_digest in active
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
        executor.shutdown(wait=False, cancel_futures=True)
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
            if status == "already_checked":
                _record_provider_diagnostic(
                    diagnostics,
                    "exact_search",
                    name,
                    "no_result",
                    0,
                    duration_ms=0,
                    already_checked=True,
                )
                continue
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
    abandoned = Event()

    def download_one(index, match):
        media_url = match.direct_media_url or match.preview_url
        if not media_url:
            error = {"provider": match.provider, "code": "import.candidate_unavailable", "message": "The exact candidate has no media URL.", "remote_id": match.remote_id}
            return index, match, None, error, False, "unavailable"
        downloaded = settings.resolved_import_staging_path / f"resolve-{uuid4().hex}{_extension(media_url)}"
        try:
            downloader.download(media_url, downloaded, match.canonical_url)
            if abandoned.is_set():
                downloaded.unlink(missing_ok=True)
                return index, match, None, None, False, "abandoned"
            exact_hash_match = _md5(downloaded) == digest
            if not exact_hash_match and not _allows_transcoded_source_media(
                match.provider, media_url, downloaded
            ):
                downloaded.unlink(missing_ok=True)
                error = {"provider": match.provider, "code": "import.candidate_hash_mismatch", "message": "The downloaded candidate did not match the source hash.", "remote_id": match.remote_id}
                return index, match, None, error, False, "unavailable"
            if abandoned.is_set():
                downloaded.unlink(missing_ok=True)
                return index, match, None, None, False, "abandoned"
            return index, match, downloaded, None, exact_hash_match, "matched"
        except Exception as error:
            downloaded.unlink(missing_ok=True)
            code, message = _download_failure(error, "The exact candidate was unavailable.")
            item = {"provider": match.provider, "code": code, "message": message, "remote_id": match.remote_id}
            return index, match, None, item, False, "unavailable"

    valid = []
    errors = []
    candidates = [(index, match) for index, match in enumerate(exact)]
    if not candidates:
        return valid, errors
    executor = ThreadPoolExecutor(
        max_workers=min(4, len(candidates)), thread_name_prefix="jiffle-exact"
    )
    futures = {
        executor.submit(download_one, index, match): (index, match)
        for index, match in candidates
    }
    pending = set(futures)
    results = []
    deadline = time.monotonic() + EXACT_DOWNLOAD_TIMEOUT_SECONDS
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        done, pending = wait(pending, timeout=min(remaining, 0.5))
        for future in done:
            results.append(future.result())
        if any(item[2] is not None and item[4] for item in results) and pending:
            deadline = min(deadline, time.monotonic() + 2.0)
    abandoned.set()
    for future in pending:
        future.cancel()
        index, match = futures[future]
        error = {
            "provider": match.provider,
            "code": "import.download_timeout",
            "message": "The exact candidate download timed out.",
            "remote_id": match.remote_id,
        }
        results.append((index, match, None, error, False, "unavailable"))
    executor.shutdown(wait=False, cancel_futures=True)
    for index, match, downloaded, error, _exact_hash_match, diag_status in sorted(
        results, key=lambda item: item[0]
    ):
        if diagnostics is not None:
            if diag_status == "matched":
                _record_provider_diagnostic(
                    diagnostics, "exact_download", match.provider, "matched", 1,
                    remote_id=match.remote_id,
                )
            elif error is not None:
                _record_provider_diagnostic(
                    diagnostics, "exact_download", match.provider, "unavailable", 0,
                    error["code"], error["message"], remote_id=match.remote_id,
                )
    for index, match, downloaded, error, _exact_hash_match, _diag_status in sorted(
        results, key=lambda item: (0 if item[4] else 1, item[0])
    ):
        if downloaded is not None:
            valid.append((match, downloaded))
        elif error:
            errors.append(error)
    return valid, errors


def _download_similar_candidates(similar, settings, downloader, diagnostics=None):
    """Download perceptual source candidates in parallel under a bounded deadline."""
    settings.resolved_import_staging_path.mkdir(parents=True, exist_ok=True)
    downloadable = []
    for index, match in enumerate(similar):
        media_url = match.direct_media_url
        if not media_url:
            unresolved_message = (
                f"A similar source was found ({match.canonical_url}), "
                "but its media file could not be loaded automatically."
                if match.canonical_url
                else "A similar source was found, but its post could not be loaded, so "
                "only a search preview is available. It is not offered as a file."
            )
            if diagnostics is not None:
                _record_provider_diagnostic(
                    diagnostics, "perceptual_search", match.provider,
                    "unavailable", 0, "import.source_link_unresolved",
                    unresolved_message,
                    remote_id=match.remote_id,
                    source_url=match.canonical_url,
                    author=match.author,
                    confidence=match.confidence,
                )
            continue
        downloadable.append((index, match, media_url))

    if not downloadable:
        return []

    abandoned = Event()

    def download_one(index, match, media_url):
        path = settings.resolved_import_staging_path / f"candidate-{uuid4().hex}{_extension(media_url)}"
        try:
            local_candidate = _safe_media_path(settings.media_path, str(media_url))
            if local_candidate is not None and local_candidate.is_file():
                shutil.copy2(local_candidate, path)
            else:
                downloader.download(media_url, path, match.canonical_url)
            if abandoned.is_set():
                path.unlink(missing_ok=True)
                return index, match, None, None, None
            candidate_inspection = inspect_media(path)
            if abandoned.is_set():
                path.unlink(missing_ok=True)
                return index, match, None, None, None
            return index, match, path, candidate_inspection, None
        except Exception as error:
            path.unlink(missing_ok=True)
            return index, match, None, None, error

    executor = ThreadPoolExecutor(
        max_workers=min(4, len(downloadable)), thread_name_prefix="jiffle-sim-dl"
    )
    futures = {
        executor.submit(download_one, index, match, media_url): (index, match)
        for index, match, media_url in downloadable
    }
    pending = set(futures)
    raw_outcomes = []
    deadline = time.monotonic() + CANDIDATE_DOWNLOAD_TIMEOUT_SECONDS
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        done, pending = wait(pending, timeout=min(remaining, 0.5))
        for future in done:
            raw_outcomes.append(future.result())
    abandoned.set()
    for future in pending:
        future.cancel()
        index, match = futures[future]
        raw_outcomes.append((
            index,
            match,
            None,
            None,
            TimeoutError("The similar candidate download timed out."),
        ))
    executor.shutdown(wait=False, cancel_futures=True)

    candidate_paths = []
    for _index, match, path, candidate_inspection, error in sorted(
        raw_outcomes, key=lambda item: item[0]
    ):
        if path is not None and candidate_inspection is not None:
            candidate_paths.append((match, path, candidate_inspection))
        elif error is not None and diagnostics is not None:
            _record_provider_diagnostic(
                diagnostics,
                "perceptual_search",
                match.provider,
                "unavailable",
                0,
                "import.candidate_unavailable",
                _safe_error_message(error, "The similar candidate was unavailable."),
                remote_id=match.remote_id,
            )
    return candidate_paths


def resolve_exact_downloads(
    path,
    providers,
    downloader,
    settings,
    diagnostics=None,
    original_name: str | None = None,
    skip_providers=None,
):
    """Find exact copies of a local file and download the bytes that verify.

    Returns ``(matches, verified, errors)``.  ``matches`` are every exact source
    the providers reported for the file hash, ``verified`` is a list of
    ``(SourceMatch, Path)`` pairs whose downloaded bytes matched the hash, and
    ``errors`` summarises provider and download failures.  Callers own the
    returned paths and must remove them.
    """
    skip_set = {str(item).strip().lower() for item in (skip_providers or ()) if item}
    digest = _md5(path)
    sha256_digest = _sha256(path)
    verify_digest = digest
    matches, search_errors = _search_exact(
        providers,
        digest,
        None,
        None,
        diagnostics,
        sha256_digest=sha256_digest,
        skip_providers=skip_set,
    )
    if not matches:
        hint_md5, hint_sha256 = _filename_hash_hints(original_name, Path(path).name)
        query_md5 = hint_md5 if (hint_md5 and hint_md5 != digest) else None
        query_sha256 = (
            hint_sha256 if (hint_sha256 and hint_sha256 != sha256_digest) else None
        )
        if query_md5 or query_sha256:
            remaining_providers = [
                provider
                for provider in (providers or ())
                if str(getattr(provider, "provider_name", "unknown")).strip().lower() not in skip_set
            ]
            hint_matches, hint_errors = _search_exact(
                remaining_providers,
                query_md5 or "",
                None,
                None,
                diagnostics,
                sha256_digest=query_sha256,
            )
            search_errors = search_errors + hint_errors
            if hint_matches:
                matches = hint_matches
                if query_md5:
                    verify_digest = query_md5
    verified, download_errors = _download_exact_candidates(
        matches, verify_digest, settings, downloader, diagnostics
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
    raw_count: int | None = None,
    above_threshold: int | None = None,
    min_confidence: float | None = None,
    source_url: str | None = None,
    author: str | None = None,
    confidence: float | None = None,
    already_checked: bool = False,
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
    if already_checked:
        item["already_checked"] = True
    if code:
        item["code"] = str(code)
        item["error_code"] = str(code)
    if message:
        safe_message = _safe_error_message(message, "The provider did not complete this step.")
        if source_url:
            safe_message = safe_message.replace("<redacted-url>", str(source_url))
        item["message"] = safe_message
        item["error_message"] = safe_message
    if duration_ms is not None:
        item["duration_ms"] = max(0, int(duration_ms))
    if remote_id not in (None, ""):
        item["remote_id"] = str(remote_id)
    if raw_count is not None:
        item["raw_count"] = max(0, int(raw_count))
    if above_threshold is not None:
        item["above_threshold"] = max(0, int(above_threshold))
    if min_confidence is not None:
        item["min_confidence"] = round(float(min_confidence), 2)
    if source_url:
        item["source_url"] = str(source_url)
    if author:
        item["author"] = str(author)
    if confidence is not None:
        item["confidence"] = round(float(confidence), 2)
    target.append(item)


def _safe_error_message(error, fallback: str) -> str:
    """Keep diagnostics useful without leaking URLs, credentials, or exception data."""
    message = str(error or "").strip()
    if not message:
        return fallback
    # Provider failures already expose a curated message.  For arbitrary
    # exceptions retain only a short first line so request details cannot leak.
    message = re.sub(r"https?://[^\s)]+", "<redacted-url>", message.splitlines()[0])
    message = re.sub(
        r"(?i)(api_key|key|token|password|cookie_[ab])=[^&\s'\")]+",
        r"\1=<redacted>",
        message,
    )
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
        wanted_int = int(str(wanted), 16)
        hash_bits = wanted.hash.size
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
            distance = (wanted_int ^ int(str(row["perceptual_hash"]), 16)).bit_count()
            confidence = 100.0 * (hash_bits - distance) / hash_bits
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


def _reverse_similar(
    image_path,
    providers,
    diagnostics=None,
    settings: Settings | None = None,
    skip_providers=None,
):
    """Run every available reverse search and return confirmable candidates.

    Providers are queried at the same time under one deadline so a slow service
    cannot park the import.  A result that points at a supported post is loaded
    through the matching provider first, so the candidate carries real tags and
    the original file instead of only a similarity thumbnail.
    """
    skip_set = {str(item).strip().lower() for item in (skip_providers or ()) if item}
    if reverse_preview_bytes(image_path) is None:
        # A file that is neither an image nor a decodable video has nothing to
        # upload.  Report that once instead of letting every provider claim a
        # search that never ran and found nothing.
        if diagnostics is not None:
            _record_provider_diagnostic(
                diagnostics, "perceptual_search", "preview", "not_supported", 0,
                "import.reverse_search_no_preview",
                "This file has no readable image or video frame to reverse-search.",
            )
        return []
    reverse_providers = []
    seen_reverse_names: set[str] = set()
    for provider in list(providers) + [
        _iqdb_reverse_search(),
        _saucenao_reverse_search(settings),
        _fluffle_reverse_search(),
        _fuzzysearch_reverse_search(settings),
        _yandex_reverse_search(),
    ]:
        if provider is None or not callable(getattr(provider, "search_similar", None)):
            continue
        name = (
            getattr(provider, "provider_name", None)
            or provider.__class__.__name__.lower()
        )
        if name in seen_reverse_names:
            continue
        seen_reverse_names.add(name)
        # A provider that answers exact MD5 lookups may still have no reverse
        # search at all.  Report that honestly once instead of listing it as a
        # service that was queried and found nothing.
        if not getattr(provider, "supports_reverse_search", True):
            if diagnostics is not None:
                _record_provider_diagnostic(
                    diagnostics, "perceptual_search", name, "not_supported", 0,
                    "import.reverse_search_unsupported",
                    f"{name} has no reverse-image search.",
                )
            continue
        if str(name).strip().lower() in skip_set:
            if diagnostics is not None:
                _record_provider_diagnostic(
                    diagnostics,
                    "perceptual_search",
                    name,
                    "no_result",
                    0,
                    duration_ms=0,
                    already_checked=True,
                )
            continue
        if not _reverse_provider_available(provider):
            if diagnostics is not None:
                # Surface the skipped source so the user knows that signing in
                # could widen the search instead of seeing a silent no-result.
                _record_provider_diagnostic(
                    diagnostics, "perceptual_search", name, "not_configured", 0,
                    "import.provider_not_configured",
                    f"{name} was not checked because its account is not configured.",
                )
            continue
        if _provider_on_cooldown(name):
            if diagnostics is not None:
                _record_provider_diagnostic(
                    diagnostics, "perceptual_search", name, "skipped", 0,
                    "import.provider_cooldown",
                    f"{name} did not answer recently and was skipped for a few minutes.",
                )
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
        _mark_provider_failure(name)
        raw_results.append({
            "provider": name,
            "matches": [],
            "duration_ms": int(REVERSE_SEARCH_TIMEOUT_SECONDS * 1000),
            "min_confidence": _reverse_min_confidence(provider),
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
    executor.shutdown(wait=False, cancel_futures=True)

    url_cache: dict[str, object] = {}
    url_cache_lock = Lock()
    provider_entries: list[tuple[dict[str, object], list[object], list[object], float]] = []
    resolve_tasks: list[tuple[int, int, object]] = []

    for p_idx, result in enumerate(raw_results):
        provider_name = str(result["provider"])
        error = result.get("error")
        if error:
            error_code = str(error.get("code") or "")
            if error_code in {
                "import.rate_limited",
                "import.provider_unavailable",
                "import.e621_rate_limited",
                "import.e621_unavailable",
                "import.e621_timeout",
                "import.provider_timeout",
            }:
                _mark_provider_failure(provider_name)
        else:
            _clear_provider_cooldown(provider_name)
        raw_matches = list(result.get("matches") or [])
        # Each service reports similarity on its own scale, so keep the
        # provider-specific cut-off that the lookup used.
        min_confidence = float(result.get("min_confidence") or MIN_SIMILAR_CONFIDENCE)
        accepted = [
            raw for raw in raw_matches
            if (_reverse_confidence(raw) or 0) >= min_confidence
        ]
        capped_accepted = sorted(
            accepted,
            key=lambda item: _reverse_confidence(item) or 0.0,
            reverse=True,
        )[:MAX_REVERSE_CANDIDATES]
        provider_entries.append((result, raw_matches, accepted, min_confidence))
        for m_idx, raw in enumerate(capped_accepted):
            resolve_tasks.append((p_idx, m_idx, raw))

    resolved_by_provider: dict[int, list[tuple[int, object]]] = {
        idx: [] for idx in range(len(provider_entries))
    }
    if resolve_tasks:
        resolve_executor = ThreadPoolExecutor(
            max_workers=min(6, len(resolve_tasks)),
            thread_name_prefix="jiffle-rev-resolve",
        )
        resolve_futures = {
            resolve_executor.submit(
                _resolve_reverse_candidate,
                raw,
                providers,
                url_cache,
                url_cache_lock,
            ): (p_idx, m_idx, raw)
            for p_idx, m_idx, raw in resolve_tasks
        }
        resolve_pending = set(resolve_futures)
        resolve_deadline = time.monotonic() + REVERSE_RESOLVE_TIMEOUT_SECONDS
        while resolve_pending:
            remaining = resolve_deadline - time.monotonic()
            if remaining <= 0:
                break
            done, resolve_pending = wait(resolve_pending, timeout=min(remaining, 0.5))
            for future in done:
                p_idx, m_idx, raw = resolve_futures[future]
                try:
                    resolved_by_provider[p_idx].append((m_idx, future.result()))
                except Exception:
                    resolved_by_provider[p_idx].append((m_idx, raw))
        for future in resolve_pending:
            future.cancel()
            p_idx, m_idx, raw = resolve_futures[future]
            resolved_by_provider[p_idx].append((m_idx, raw))
        resolve_executor.shutdown(wait=False, cancel_futures=True)

    collected: list[object] = []
    for p_idx, (result, raw_matches, accepted, min_confidence) in enumerate(provider_entries):
        provider_name = str(result["provider"])
        error = result.get("error")
        resolved = [
            item
            for _m_idx, item in sorted(
                resolved_by_provider.get(p_idx, ()), key=lambda pair: pair[0]
            )
        ]
        kept = [
            match
            for match in (_coerce_match(item, "perceptual") for item in resolved)
            if match is not None
        ]
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
                    "matched" if raw_matches else "no_result", len(kept),
                    duration_ms=result["duration_ms"],
                    raw_count=len(raw_matches),
                    above_threshold=len(accepted),
                    min_confidence=min_confidence,
                )
        collected.extend(kept)
    return _unique_similar(collected)[:MAX_REVERSE_CANDIDATES]


def _iqdb_reverse_search():
    try:
        from jiffle.features.imports.source_adapters.iqdb import IqdbReverseSearch
        return IqdbReverseSearch()
    except Exception:
        return None


def _saucenao_reverse_search(settings: Settings | None = None):
    try:
        from jiffle.features.imports.source_adapters.saucenao import SaucenaoReverseSearch
        api_key = getattr(settings, "saucenao_api_key", None) if settings is not None else None
        return SaucenaoReverseSearch(api_key=api_key)
    except Exception:
        return None


def _fluffle_reverse_search():
    try:
        from jiffle.features.imports.source_adapters.fluffle import FluffleReverseSearchProvider
        return FluffleReverseSearchProvider()
    except Exception:
        return None


def _fuzzysearch_reverse_search(_settings: Settings | None = None):
    try:
        from jiffle.features.imports.source_adapters.fuzzysearch import FuzzySearchReverseSearchProvider
        return FuzzySearchReverseSearchProvider()
    except Exception:
        return None


def _yandex_reverse_search():
    try:
        from jiffle.features.imports.source_adapters.yandex import YandexReverseSearchProvider
        return YandexReverseSearchProvider()
    except Exception:
        return None


def _reverse_min_confidence_by_name(provider_name: str, providers) -> float:
    for provider in providers or ():
        if getattr(provider, "provider_name", None) == provider_name:
            return _reverse_min_confidence(provider)
    if str(provider_name or "").lower() == "e621":
        return 50.0
    return MIN_SIMILAR_CONFIDENCE


def _run_reverse_search(provider, image_path) -> dict[str, object]:
    name = getattr(provider, "provider_name", None) or provider.__class__.__name__.lower()
    min_confidence = _reverse_min_confidence(provider)
    started = time.perf_counter()
    try:
        matches = list(getattr(provider, "search_similar")(image_path) or [])
        return {
            "provider": name, "matches": matches, "error": None,
            "duration_ms": _elapsed_ms(started), "min_confidence": min_confidence,
        }
    except SourceProviderFailure as error:
        return {
            "provider": name, "matches": [], "duration_ms": _elapsed_ms(started),
            "min_confidence": min_confidence,
            "error": {
                "status": _status_for_error(error.code),
                "code": error.code,
                "message": _safe_error_message(error.message, "The reverse search failed."),
            },
        }
    except Exception as error:
        return {
            "provider": name, "matches": [], "duration_ms": _elapsed_ms(started),
            "min_confidence": min_confidence,
            "error": {
                "status": "network_error",
                "code": "import.source_search_failed",
                "message": _safe_error_message(error, "The reverse search failed."),
            },
        }


def _reverse_min_confidence(provider) -> float:
    """Return the similarity cut-off a reverse-search provider reports on.

    Most services report a percentage where a good match is close to 100, so
    the shared cut-off applies.  e621's IQDB backend reports even an exact match
    around 60, so it declares a lower threshold of its own.
    """
    try:
        value = float(getattr(provider, "min_similar_confidence", None))
    except (TypeError, ValueError):
        return MIN_SIMILAR_CONFIDENCE
    return value if value > 0 else MIN_SIMILAR_CONFIDENCE


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


_PREVIEW_MD5_RE = re.compile(
    r"(?:^|/|_|-)([0-9a-fA-F]{32})\.(?:jpg|jpeg|png|gif|webp|webm|mp4)$",
    re.IGNORECASE,
)


def _valid_md5_hex(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if len(text) == 32 and all(character in "0123456789abcdef" for character in text):
        return text
    return None


def _reverse_md5(url: str) -> str | None:
    """Read a 32-character MD5 from a booru search link, when present."""
    query = parse_qs(urlsplit(url).query)
    return _valid_md5_hex((query.get("md5") or [""])[0])


def _extract_md5_from_reverse_raw(raw, resolved_candidate=None) -> str | None:
    if resolved_candidate is not None:
        digest = _valid_md5_hex(getattr(resolved_candidate, "content_md5", None))
        if digest:
            return digest
    if isinstance(raw, dict):
        for key in ("content_md5", "md5"):
            digest = _valid_md5_hex(raw.get(key))
            if digest:
                return digest
    else:
        digest = _valid_md5_hex(getattr(raw, "content_md5", None))
        if digest:
            return digest
    for url in _reverse_links(raw):
        digest = _reverse_md5(url)
        if digest:
            return digest
        path_match = _PREVIEW_MD5_RE.search(urlsplit(url).path or url)
        if path_match:
            return path_match.group(1).lower()
    preview_url = (
        raw.get("preview_url")
        if isinstance(raw, dict)
        else getattr(raw, "preview_url", None)
    )
    if preview_url:
        path_match = _PREVIEW_MD5_RE.search(
            urlsplit(str(preview_url)).path or str(preview_url)
        )
        if path_match:
            return path_match.group(1).lower()
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
        provider_name = getattr(provider, "provider_name", "unknown")
        if _provider_on_cooldown(provider_name) or (
            _provider_needs_configuration(provider)
            and not getattr(provider, "can_fetch_without_configuration", False)
        ):
            return None
        digest = _reverse_md5(url)
        if digest:
            search = getattr(provider, "search_by_md5", None)
            if not callable(search):
                continue
            try:
                fallback_match = None
                for raw in search(digest) or []:
                    match = _coerce_match(raw, "perceptual")
                    if match is not None and (match.direct_media_url or match.preview_url):
                        return match
                    if match is not None and fallback_match is None:
                        fallback_match = match
                if fallback_match is not None:
                    return fallback_match
            except Exception:
                continue
            continue
        fetch = getattr(provider, "fetch", None)
        fetch_metadata = getattr(provider, "fetch_metadata", None)
        if not callable(fetch) and not callable(fetch_metadata):
            continue
        source = None
        fetch_failed_missing = False
        if callable(fetch):
            try:
                source = _coerce_source(
                    fetch(url), url, getattr(provider, "provider_name", "unknown")
                )
            except SourceProviderFailure as error:
                if error.code == "import.source_media_missing":
                    fetch_failed_missing = True
                elif error.code in {
                    "import.rate_limited",
                    "import.e621_rate_limited",
                    "import.e621_timeout",
                    "import.provider_timeout",
                }:
                    _mark_provider_failure(provider_name)
            except Exception:
                source = None
        if (
            source is None
            and callable(fetch_metadata)
            and (not callable(fetch) or fetch_failed_missing)
        ):
            try:
                source = _coerce_source(
                    fetch_metadata(url), url, getattr(provider, "provider_name", "unknown")
                )
            except Exception:
                source = None
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


def _resolve_reverse_candidate(raw, providers, url_cache=None, url_cache_lock=None):
    if isinstance(raw, SourceMatch) and raw.direct_media_url:
        return raw
    confidence = _reverse_confidence(raw)
    preview = raw.get("preview_url") if isinstance(raw, dict) else getattr(raw, "preview_url", None)
    raw_author = raw.get("author") if isinstance(raw, dict) else getattr(raw, "author", None)
    raw_tags = tuple(raw.get("tags") or ()) if isinstance(raw, dict) else tuple(getattr(raw, "tags", ()) or ())
    raw_direct = raw.get("direct_media_url") if isinstance(raw, dict) else getattr(raw, "direct_media_url", None)
    fallback = None
    for url in _reverse_links(raw):
        cached_hit = False
        resolved = None
        if url_cache is not None and url_cache_lock is not None:
            with url_cache_lock:
                if url in url_cache:
                    resolved = url_cache[url]
                    cached_hit = True
        if not cached_hit:
            resolved = _reverse_candidate_from_url(url, providers)
            if url_cache is not None and url_cache_lock is not None:
                with url_cache_lock:
                    url_cache[url] = resolved
        if resolved is None:
            continue
        candidate = replace(
            resolved,
            author=resolved.author or raw_author,
            tags=resolved.tags or raw_tags,
            direct_media_url=resolved.direct_media_url or raw_direct,
            match_method="perceptual",
            confidence=confidence if confidence is not None else resolved.confidence,
            preview_url=resolved.preview_url or preview,
        )
        # The matched post page is what makes a candidate confirmable; a result
        # that only carries a search-service thumbnail is not a real source.
        # Keep looking through the remaining links for a loadable post.
        if candidate.direct_media_url:
            if fallback is not None:
                best_meta = _select_metadata_source([fallback, candidate]) or candidate
                return replace(
                    best_meta,
                    direct_media_url=candidate.direct_media_url,
                    content_md5=best_meta.content_md5 or candidate.content_md5,
                    match_method="perceptual",
                    confidence=candidate.confidence,
                    preview_url=best_meta.preview_url or candidate.preview_url or preview,
                    width=best_meta.width or candidate.width,
                    height=best_meta.height or candidate.height,
                )
            return candidate
        if fallback is None:
            fallback = candidate

    digest = _extract_md5_from_reverse_raw(raw, fallback)
    if digest:
        exact_matches, _ = _search_exact(providers or (), digest, None)
        md5_matches = [
            replace(item, match_method="perceptual") for item in exact_matches
        ]
        file_match = next((item for item in md5_matches if item.direct_media_url), None)
        if file_match is not None:
            pool = [item for item in ([fallback] if fallback is not None else []) + md5_matches if item is not None]
            best_meta = _select_metadata_source(pool) or file_match
            return replace(
                best_meta,
                direct_media_url=file_match.direct_media_url,
                content_md5=best_meta.content_md5 or file_match.content_md5 or digest,
                match_method="perceptual",
                confidence=confidence if confidence is not None else file_match.confidence,
                preview_url=best_meta.preview_url or file_match.preview_url or preview,
                width=best_meta.width or file_match.width,
                height=best_meta.height or file_match.height,
            )
        if fallback is None and md5_matches:
            best_meta = _select_metadata_source(md5_matches)
            if best_meta is not None:
                fallback = replace(
                    best_meta,
                    match_method="perceptual",
                    confidence=confidence if confidence is not None else best_meta.confidence,
                    preview_url=best_meta.preview_url or preview,
                )
    return fallback if fallback is not None else raw


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
        existing = unique.get(key)
        if existing is None or (
            bool(match.direct_media_url),
            match.confidence,
        ) > (
            bool(existing.direct_media_url),
            existing.confidence,
        ):
            unique[key] = match
    return sorted(
        unique.values(),
        key=lambda item: (bool(item.direct_media_url), item.confidence),
        reverse=True,
    )


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


def _stage_previously_deleted_review(
    connection: sqlite3.Connection,
    settings: Settings,
    job_id: int,
    candidate_id: int,
    path: Path,
    inspection,
    metadata_source: SourceMedia | None,
    details: dict[str, object],
) -> None:
    filtered_source = filter_source_tags(connection, metadata_source)
    staged_name = atomic_copy(path, settings.resolved_import_staging_path, "candidate")
    try:
        connection.execute(
            "UPDATE import_candidates SET status='review', stored_path=?, media_type=?, "
            "content_hash=?, width=?, height=?, file_size=?, source_metadata_json=? WHERE id=?",
            (
                staged_name,
                inspection.media_type,
                inspection.content_hash,
                inspection.width,
                inspection.height,
                inspection.file_size,
                _serialize_source(filtered_source) if filtered_source else None,
                candidate_id,
            ),
        )
        review_cursor = connection.execute(
            "INSERT INTO review_items (import_candidate_id, reason) VALUES (?, 'previously_deleted')",
            (candidate_id,),
        )
        review_id = int(review_cursor.lastrowid)
        connection.commit()
    except Exception:
        (settings.resolved_import_staging_path / staged_name).unlink(missing_ok=True)
        connection.rollback()
        raise
    details["resolution_method"] = "previously_deleted"
    if filtered_source and filtered_source.canonical_url:
        details["resolved_source_url"] = filtered_source.canonical_url
    _finish(
        connection,
        job_id,
        {
            "outcome": "review",
            "review_reason": "previously_deleted",
            "candidate_id": candidate_id,
            "review_item_id": review_id,
        },
        details,
    )


def _merge_duplicate_metadata(
    connection: sqlite3.Connection,
    media_id: int,
    metadata_source: SourceMedia | None,
    file_source: SourceMedia | None,
    source_url: str | None,
) -> None:
    metadata_source = filter_source_tags(connection, metadata_source)
    if metadata_source and metadata_source.tags:
        connection.executemany(
            "INSERT OR IGNORE INTO media_tags (media_item_id, tag) VALUES (?, ?)",
            ((media_id, tag) for tag in metadata_source.tags),
        )
    if metadata_source and metadata_source.provider != "direct":
        existing_source = connection.execute(
            "SELECT canonical_url, direct_media_url, author, domain, parent_id, character_tags_json "
            "FROM media_sources WHERE media_item_id=?",
            (media_id,),
        ).fetchone()
        if existing_source is None:
            try:
                connection.execute(
                    "INSERT INTO media_sources "
                    "(media_item_id, canonical_url, direct_media_url, provider, remote_id, author, domain, parent_id, character_tags_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        media_id,
                        metadata_source.canonical_url,
                        metadata_source.direct_media_url,
                        metadata_source.provider,
                        metadata_source.remote_id,
                        metadata_source.author,
                        metadata_source.domain,
                        metadata_source.parent_id,
                        json.dumps(list(metadata_source.character_tags)),
                    ),
                )
            except sqlite3.IntegrityError:
                owner = connection.execute(
                    "SELECT media_item_id FROM media_sources WHERE canonical_url=?",
                    (metadata_source.canonical_url,),
                ).fetchone()
                if owner is not None and int(owner[0]) != int(media_id):
                    deleted = connection.execute(
                        "SELECT deleted_at FROM media_items WHERE id=?", (owner[0],)
                    ).fetchone()
                    if deleted is not None and deleted[0] is not None:
                        connection.execute(
                            "UPDATE media_sources SET media_item_id=?, direct_media_url=?, provider=?, "
                            "remote_id=?, author=?, domain=?, parent_id=?, character_tags_json=? "
                            "WHERE media_item_id=?",
                            (
                                media_id,
                                metadata_source.direct_media_url,
                                metadata_source.provider,
                                metadata_source.remote_id,
                                metadata_source.author,
                                metadata_source.domain,
                                metadata_source.parent_id,
                                json.dumps(list(metadata_source.character_tags)),
                                owner[0],
                            ),
                        )
        else:
            existing_chars = list(_decode_json_tags(existing_source["character_tags_json"]))
            for char_tag in metadata_source.character_tags:
                if char_tag not in existing_chars:
                    existing_chars.append(char_tag)
            connection.execute(
                "UPDATE media_sources SET "
                "direct_media_url=COALESCE(NULLIF(TRIM(direct_media_url), ''), ?), "
                "author=COALESCE(NULLIF(TRIM(author), ''), ?), "
                "domain=COALESCE(NULLIF(TRIM(domain), ''), ?), "
                "parent_id=COALESCE(NULLIF(TRIM(parent_id), ''), ?), "
                "character_tags_json=? "
                "WHERE media_item_id=?",
                (
                    metadata_source.direct_media_url,
                    metadata_source.author,
                    metadata_source.domain,
                    metadata_source.parent_id,
                    json.dumps(existing_chars),
                    media_id,
                ),
            )
    item = connection.execute(
        "SELECT source_url, file_source_url, author, domain, parent_id, character_tags_json "
        "FROM media_items WHERE id=?",
        (media_id,),
    ).fetchone()
    if item is None:
        return
    item_chars = list(_decode_json_tags(item["character_tags_json"]))
    if metadata_source:
        for char_tag in metadata_source.character_tags:
            if char_tag not in item_chars:
                item_chars.append(char_tag)
    effective_source_url = (
        source_url
        if (metadata_source is None or metadata_source.provider != "direct")
        else None
    )
    connection.execute(
        "UPDATE media_items SET "
        "source_url=COALESCE(NULLIF(TRIM(source_url), ''), ?), "
        "file_source_url=COALESCE(NULLIF(TRIM(file_source_url), ''), ?), "
        "author=COALESCE(NULLIF(TRIM(author), ''), ?), "
        "domain=COALESCE(NULLIF(TRIM(domain), ''), ?), "
        "parent_id=COALESCE(NULLIF(TRIM(parent_id), ''), ?), "
        "character_tags_json=? "
        "WHERE id=?",
        (
            effective_source_url,
            file_source.canonical_url if file_source else None,
            metadata_source.author if metadata_source else None,
            metadata_source.domain if (metadata_source and metadata_source.provider != "direct") else None,
            metadata_source.parent_id if metadata_source else None,
            json.dumps(item_chars),
            media_id,
        ),
    )


def _accept_downloaded(
    connection, settings, job_id, path, source, submitted_input,
    source_url_override=None, metadata_source=None, file_source=None,
):
    metadata_source = filter_source_tags(connection, metadata_source or source)
    file_source = filter_source_tags(connection, file_source or source)
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
        existing_id = int(existing[0])
        _merge_duplicate_metadata(
            connection, existing_id, metadata_source, file_source, source_url
        )
        if metadata_source:
            connection.execute(
                "UPDATE import_candidates SET source_metadata_json=? WHERE id=?",
                (_serialize_source(metadata_source), candidate_id),
            )
        _set_candidate_result(connection, candidate_id, "duplicate", existing_id)
        return {"outcome": "duplicate", "candidate_id": candidate_id, "media_item_id": existing_id}

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
        create_original_revision(connection, media_id, settings)
        if inspection.media_type == "image":
            try:
                with Image.open(path) as image:
                    fingerprint = str(imagehash.phash(image))
                connection.execute(
                    "INSERT INTO media_fingerprints (media_item_id, perceptual_hash) VALUES (?, ?)",
                    (media_id, fingerprint),
                )
                for other_media_id, confidence in find_similar_media(
                    connection, fingerprint, DEFAULT_MATCH_THRESHOLD, exclude_media_id=media_id
                ):
                    register_pending_match(connection, media_id, other_media_id, confidence)
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
            existing_id = int(existing[0])
            (settings.media_path / stored).unlink(missing_ok=True)
            _merge_duplicate_metadata(
                connection, existing_id, metadata_source, file_source, source_url
            )
            if metadata_source:
                connection.execute(
                    "UPDATE import_candidates SET source_metadata_json=? WHERE id=?",
                    (_serialize_source(metadata_source), candidate_id),
                )
            _set_candidate_result(connection, candidate_id, "duplicate", existing_id)
            return {"outcome": "duplicate", "candidate_id": candidate_id, "media_item_id": existing_id}
        (settings.media_path / stored).unlink(missing_ok=True)
        raise
    except Exception:
        (settings.media_path / stored).unlink(missing_ok=True)
        connection.rollback()
        raise


def _store_source(connection, media_id, source):
    source = filter_source_tags(connection, source)
    values = (media_id, source.canonical_url, source.direct_media_url, source.provider,
              source.remote_id, source.author, source.domain, source.parent_id,
              json.dumps(list(source.character_tags)))
    sql = (
        "INSERT INTO media_sources (media_item_id, canonical_url, direct_media_url, provider, remote_id, author, domain, parent_id, character_tags_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(media_item_id) DO UPDATE SET canonical_url=excluded.canonical_url, direct_media_url=excluded.direct_media_url, provider=excluded.provider, remote_id=excluded.remote_id, author=excluded.author, domain=excluded.domain, parent_id=excluded.parent_id, character_tags_json=excluded.character_tags_json"
    )
    try:
        connection.execute(sql, values)
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
        connection.execute(
            "DELETE FROM media_sources WHERE media_item_id=?",
            (owner[0],),
        )
        connection.execute(sql, values)
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
    cand_row = connection.execute(
        "SELECT shelf_ids_json FROM import_candidates WHERE job_id=?", (job_id,)
    ).fetchone()
    raw_shelf_ids = cand_row["shelf_ids_json"] if cand_row is not None else "[]"
    media_id = payload.get("media_item_id")
    if isinstance(media_id, int) and media_id > 0:
        applied_shelves = assign_shelves_to_media(connection, media_id, raw_shelf_ids)
    else:
        applied_shelves = resolve_shelf_summaries(connection, raw_shelf_ids)
        review_id = payload.get("review_item_id")
        if isinstance(review_id, int) and review_id > 0 and payload.get("outcome") == "duplicate":
            incoming_shelves = parse_shelf_ids(raw_shelf_ids)
            if incoming_shelves:
                target_row = connection.execute(
                    "SELECT candidate.id, candidate.shelf_ids_json "
                    "FROM review_items review JOIN import_candidates candidate "
                    "ON candidate.id=review.import_candidate_id WHERE review.id=?",
                    (review_id,),
                ).fetchone()
                if target_row is not None:
                    existing_shelves = parse_shelf_ids(target_row["shelf_ids_json"])
                    merged_shelves = tuple(dict.fromkeys((*existing_shelves, *incoming_shelves)))
                    if merged_shelves != existing_shelves:
                        connection.execute(
                            "UPDATE import_candidates SET shelf_ids_json=? WHERE id=?",
                            (json.dumps(list(merged_shelves)), int(target_row["id"])),
                        )
    if applied_shelves:
        payload["shelves"] = applied_shelves
        details["shelves"] = applied_shelves
    if details.get("resolution_method") and "resolution_method" not in payload:
        payload["resolution_method"] = details["resolution_method"]
    payload["resolution"] = details
    connection.execute("UPDATE background_jobs SET status='completed', progress=100, result_json=?, finished_at=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(payload), job_id))
    update_import_history(connection, job_id, result.get("outcome", "failed"), {**details, **payload})
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


def _sha256(path):
    digest = hashlib.sha256()
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
    parsed = urlsplit(str(url))
    path = parsed.path or ""
    suffix = Path(path).suffix.lower()
    if not suffix and "@" in path:
        tail = path.rsplit("@", 1)[-1].lower()
        if tail in {"jpg", "jpeg", "png", "gif", "webp", "mp4", "webm"}:
            suffix = ".jpg" if tail == "jpeg" else f".{tail}"
    if not suffix and parsed.query:
        fmt = (parse_qs(parsed.query).get("format") or [""])[0].strip().lower().lstrip(".")
        if fmt in {"jpg", "jpeg", "png", "gif", "webp", "mp4", "webm"}:
            suffix = ".jpg" if fmt == "jpeg" else f".{fmt}"
    return suffix or ".jpg"
