"""Universal file/URL import resolution workflow.

The legacy local and URL jobs remain available for old clients.  This module
provides the single resolver used by the current Import screen.
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import shutil
import sqlite3
import time
from urllib.parse import urlsplit
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
from jiffle.infrastructure.media_revisions import create_original_revision


MIN_SIMILAR_CONFIDENCE = 80.0


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
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
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
        "timing": {"duration_ms": 0, "phases_ms": {}},
    }
    started_at = time.perf_counter()
    details["_started_at"] = started_at
    try:
        _running(connection, job_id)
        source_path = Path(submitted_input)
        original_path: Path
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
                _finish(
                    connection,
                    job_id,
                    {"outcome": "duplicate", "candidate_id": candidate_id, "media_item_id": existing_source},
                    details,
                )
                return
            original_path, source, metadata_errors = _resolve_url_metadata(
                submitted_input, providers, details["provider_timings"], details["timing"]["phases_ms"]
            )
            details["provider_errors"] = metadata_errors
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
                except Exception:
                    original_path.unlink(missing_ok=True)
                    original_path = None
                details["timing"]["phases_ms"]["source_download"] = _elapsed_ms(source_download_started)
            if original_path is not None:
                temporary.append(original_path)
                result = _accept_downloaded(
                    connection, settings, job_id, original_path, source, submitted_input,
                    source_url_override=normalized_input,
                )
                details.update({
                    "resolution_method": result.get("resolution_method", "source"),
                    "resolved_source_url": source.canonical_url if source else submitted_input,
                })
                _finish(connection, job_id, result, details)
                return
            digest = source.content_md5 if source else None
            source_hint = source
        else:
            if not source_path.is_file():
                raise ImportFailure("import.file_not_found", "The selected file does not exist.")
            source_hint = None
            digest = _md5(source_path)
            original_path = source_path

        if input_kind == "url" and original_path is None:
            if not digest:
                raise ImportFailure("import.source_media_missing", "The source has no downloadable media or file hash.")
            if connection.execute("SELECT 1 FROM blocked_media_signatures WHERE content_hash=?", (digest,)).fetchone() and settings.block_previously_deleted:
                raise ImportFailure("import.previously_deleted", "This media was previously deleted and is blocked by settings.")
            exact_started = time.perf_counter()
            exact, errors = _search_exact(providers, digest, source_hint, details["provider_timings"])
            details["timing"]["phases_ms"]["exact_search"] = _elapsed_ms(exact_started)
            details["exact_candidates_checked"] = len(exact)
            details["provider_errors"] = list(details.get("provider_errors", [])) + errors
            download_started = time.perf_counter()
            valid_candidates, download_errors = _download_exact_candidates(exact, digest, settings, downloader)
            details["timing"]["phases_ms"]["exact_download"] = _elapsed_ms(download_started)
            details.setdefault("provider_errors", []).extend(download_errors)
            for match, downloaded in valid_candidates:
                try:
                    source = _match_to_source(match)
                    result = _accept_downloaded(
                        connection, settings, job_id, downloaded, source, submitted_input,
                        source_url_override=normalized_input,
                    )
                    details.update({
                        "resolution_method": result.get("resolution_method", "exact"),
                        "resolved_source_url": match.canonical_url,
                    })
                    _finish(connection, job_id, result, details)
                    for _, other in valid_candidates:
                        other.unlink(missing_ok=True)
                    return
                except Exception as error:
                    details.setdefault("provider_errors", []).append({"provider": match.provider, "code": "import.candidate_unavailable", "message": str(error)})
                finally:
                    downloaded.unlink(missing_ok=True)
            raise ImportFailure("import.source_not_found", "No downloadable exact copy was found for this source.")

        inspection = inspect_media(original_path)
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

        if not digest:
            digest = _md5(original_path)
        exact_started = time.perf_counter()
        exact, errors = _search_exact(providers, digest, source_hint, details["provider_timings"])
        details["timing"]["phases_ms"]["exact_search"] = _elapsed_ms(exact_started)
        details["exact_candidates_checked"] = len(exact)
        details["provider_errors"] = list(details.get("provider_errors", [])) + errors
        download_started = time.perf_counter()
        valid_candidates, download_errors = _download_exact_candidates(exact, digest, settings, downloader)
        details["timing"]["phases_ms"]["exact_download"] = _elapsed_ms(download_started)
        details["provider_errors"] = list(details.get("provider_errors", [])) + download_errors
        for match, downloaded in valid_candidates:
            try:
                temporary.append(downloaded)
                source = _match_to_source(match)
                result = _accept_downloaded(connection, settings, job_id, downloaded, source, submitted_input)
                details.update({
                    "resolution_method": result.get("resolution_method", "exact"),
                    "resolved_source_url": match.canonical_url,
                })
                _finish(connection, job_id, result, details)
                temporary.remove(downloaded)
                downloaded.unlink(missing_ok=True)
                for _, other in valid_candidates:
                    other.unlink(missing_ok=True)
                return
            except Exception as error:
                downloaded.unlink(missing_ok=True)
                details.setdefault("provider_errors", []).append({"provider": match.provider, "code": "import.candidate_unavailable", "message": str(error)})
            finally:
                if downloaded in temporary:
                    temporary.remove(downloaded)
        for _, downloaded in valid_candidates:
            downloaded.unlink(missing_ok=True)

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
        if inspection.media_type == "image":
            similar.extend(_reverse_similar(original_path, providers))
        similar = [_coerce_match(match, "perceptual") for match in similar]
        similar = [match for match in _unique_similar(similar) if match.confidence >= MIN_SIMILAR_CONFIDENCE]
        details["similar_candidates_found"] = len(similar)
        for match in similar:
            media_url = match.direct_media_url or match.preview_url
            if not media_url:
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
            except Exception:
                path.unlink(missing_ok=True)
        if candidate_paths:
            cursor = connection.execute(
                "UPDATE import_candidates SET status='review', stored_path=?, media_type=?, "
                "content_hash=?, width=?, height=?, file_size=? WHERE id=?",
                (original_staged, inspection.media_type, inspection.content_hash,
                 inspection.width, inspection.height, inspection.file_size, candidate_id),
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
            "content_hash=?, width=?, height=?, file_size=? WHERE id=?",
            (original_staged, inspection.media_type, inspection.content_hash,
             inspection.width, inspection.height, inspection.file_size, candidate_id),
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
        _failed(connection, job_id, error.code, error.message, details)
    except Exception:
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
            if row and (row["status"] in {"accepted", "duplicate", "failed"} or is_staged_upload and row["stored_path"] != upload_path.name):
                Path(submitted_input).unlink(missing_ok=True)
        connection.close()


def _resolve_url_metadata(url: str, providers, provider_timings=None, phase_timings=None):
    normalized = normalize_source_url(url)
    provider = next((item for item in providers if item.can_handle(normalized)), None)
    if provider is None:
        return None, SourceMedia(normalized, normalized, "direct", "", None,
                                 urlsplit(normalized).netloc, (), _extension(normalized)), []
    errors = []
    started = time.perf_counter()
    try:
        fetch_metadata = getattr(provider, "fetch_metadata", provider.fetch)
        source = _coerce_source(fetch_metadata(normalized), normalized, getattr(provider, "provider_name", "unknown"))
        duration_ms = _elapsed_ms(started)
        _record_provider_timing(provider_timings, provider, duration_ms, "ok")
        if phase_timings is not None:
            phase_timings["metadata"] = duration_ms
        if source.direct_media_url:
            return None, source, errors
        return None, source, errors
    except SourceProviderFailure as error:
        duration_ms = _elapsed_ms(started)
        _record_provider_timing(provider_timings, provider, duration_ms, "error")
        if phase_timings is not None:
            phase_timings["metadata"] = duration_ms
        errors.append({"provider": getattr(provider, "provider_name", "unknown"), "code": error.code, "message": error.message})
        return None, None, errors
    except Exception as error:
        duration_ms = _elapsed_ms(started)
        _record_provider_timing(provider_timings, provider, duration_ms, "error")
        if phase_timings is not None:
            phase_timings["metadata"] = duration_ms
        errors.append({"provider": getattr(provider, "provider_name", "unknown"), "code": "import.provider_unavailable", "message": str(error) or "The source provider is unavailable."})
        return None, None, errors


def _search_exact(providers, digest: str, source_hint: SourceMedia | None, provider_timings=None):
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
            return position, [match for match in matches if match], [], _elapsed_ms(started), "ok"
        except SourceProviderFailure as error:
            return position, [], [{"provider": getattr(provider, "provider_name", "unknown"), "code": error.code, "message": error.message}], _elapsed_ms(started), "error"
        except Exception:
            return position, [], [{"provider": getattr(provider, "provider_name", "unknown"), "code": "import.source_search_failed", "message": "The source search failed."}], _elapsed_ms(started), "error"

    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(searchable))) as executor:
        futures = [executor.submit(lookup, position, provider) for position, (_index, provider) in enumerate(searchable)]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item[0])
    matches: list[SourceMatch] = []
    errors: list[dict[str, str]] = []
    for _index, provider_matches, provider_errors, duration_ms, status in results:
        matches.extend(provider_matches)
        errors.extend(provider_errors)
        if provider_timings is not None:
            provider = searchable[_index][1]
            _record_provider_timing(provider_timings, provider, duration_ms, status)
    unique = {}
    for match in matches:
        key = (match.provider, match.remote_id or match.canonical_url)
        unique.setdefault(key, match)
    return list(unique.values()), errors


def _download_exact_candidates(exact, digest, settings, downloader):
    settings.resolved_import_staging_path.mkdir(parents=True, exist_ok=True)

    def download_one(index, match):
        media_url = match.direct_media_url or match.preview_url
        if not media_url:
            return index, match, None, {"provider": match.provider, "code": "import.candidate_unavailable", "message": "The exact candidate has no media URL."}
        downloaded = settings.resolved_import_staging_path / f"resolve-{uuid4().hex}{_extension(media_url)}"
        try:
            downloader.download(media_url, downloaded, match.canonical_url)
            if _md5(downloaded) != digest:
                downloaded.unlink(missing_ok=True)
                return index, match, None, {"provider": match.provider, "code": "import.candidate_hash_mismatch", "message": "The downloaded candidate did not match the source hash."}
            return index, match, downloaded, None
        except Exception as error:
            downloaded.unlink(missing_ok=True)
            return index, match, None, {"provider": match.provider, "code": "import.candidate_unavailable", "message": str(error) or "The exact candidate was unavailable."}

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


def _record_provider_timing(target, provider, duration_ms, status):
    if target is not None:
        target.append({
            "provider": getattr(provider, "provider_name", "unknown"),
            "duration_ms": duration_ms,
            "status": status,
        })


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
        "source.direct_media_url, source.remote_id, source.author, source.domain, media.file_path, "
        "media.width, media.height FROM media_fingerprints fp "
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
            match_method="perceptual", confidence=round(confidence, 2),
            width=row["width"], height=row["height"],
        ))
    return results


def _reverse_similar(image_path, providers):
    results = []
    reverse_providers = list(providers)
    try:
        from jiffle.features.imports.source_adapters.iqdb import IqdbReverseSearch
        reverse_providers.append(IqdbReverseSearch())
    except Exception:
        pass
    for provider in reverse_providers:
        method = getattr(provider, "search_similar", None)
        if not callable(method):
            continue
        try:
            results.extend(_coerce_match(item, "perceptual") for item in method(image_path) or [])
        except Exception:
            continue
    return [item for item in results if item is not None and item.confidence >= MIN_SIMILAR_CONFIDENCE]


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


def _match_to_source(match: SourceMatch) -> SourceMedia:
    return SourceMedia(
        canonical_url=match.canonical_url, direct_media_url=match.direct_media_url or match.canonical_url,
        provider=match.provider, remote_id=match.remote_id or "", author=match.author,
        domain=match.domain or urlsplit(match.canonical_url).netloc, tags=match.tags,
        file_extension=_extension(match.direct_media_url or match.canonical_url),
        content_md5=match.content_md5,
    )


def _accept_downloaded(
    connection, settings, job_id, path, source, submitted_input,
    source_url_override=None,
):
    inspection = inspect_media(path)
    existing = connection.execute(
        "SELECT id FROM media_items WHERE content_hash=? AND deleted_at IS NULL",
        (inspection.content_hash,),
    ).fetchone()
    candidate_id = _candidate_id(connection, job_id)
    if existing:
        if source:
            _store_source(connection, int(existing[0]), source)
        if source_url_override:
            connection.execute(
                "UPDATE media_items SET source_url=? WHERE id=?",
                (source_url_override, int(existing[0])),
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
            "INSERT INTO media_items (file_path, media_type, source_url, author, domain, width, height, file_size, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (stored, inspection.media_type, source_url_override or (source.canonical_url if source else None),
             source.author if source else None, source.domain if source else None,
             inspection.width, inspection.height, inspection.file_size, inspection.content_hash),
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
        if source:
            _store_source(connection, media_id, source)
            connection.executemany("INSERT OR IGNORE INTO media_tags (media_item_id, tag) VALUES (?, ?)", ((media_id, tag) for tag in source.tags))
            connection.execute(
                "UPDATE import_candidates SET source_metadata_json=? WHERE id=?",
                (json.dumps({"canonical_url": source.canonical_url, "direct_media_url": source.direct_media_url,
                             "provider": source.provider, "remote_id": source.remote_id,
                             "author": source.author, "domain": source.domain, "tags": list(source.tags),
                             "character_tags": list(source.character_tags), "parent_id": source.parent_id,
                             "file_extension": source.file_extension, "content_md5": source.content_md5}), candidate_id),
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
            if source:
                _store_source(connection, int(existing[0]), source)
            if source_url_override:
                connection.execute(
                    "UPDATE media_items SET source_url=? WHERE id=?",
                    (source_url_override, int(existing[0])),
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
    connection.execute(
        "INSERT INTO media_sources (media_item_id, canonical_url, direct_media_url, provider, remote_id, author, domain, parent_id, character_tags_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(media_item_id) DO UPDATE SET canonical_url=excluded.canonical_url, direct_media_url=excluded.direct_media_url, provider=excluded.provider, remote_id=excluded.remote_id, author=excluded.author, domain=excluded.domain, parent_id=excluded.parent_id, character_tags_json=excluded.character_tags_json",
        (media_id, source.canonical_url, source.direct_media_url, source.provider, source.remote_id, source.author, source.domain, source.parent_id, json.dumps(list(source.character_tags))),
    )


def _existing_source(connection, canonical_url):
    row = connection.execute(
        "SELECT item.id FROM media_items item "
        "LEFT JOIN media_sources source ON source.media_item_id=item.id "
        "WHERE item.deleted_at IS NULL "
        "AND (item.source_url=? OR source.canonical_url=?) LIMIT 1",
        (canonical_url, canonical_url),
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


def _running(connection, job_id):
    connection.execute("UPDATE background_jobs SET status='running', progress=10, started_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
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
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm"} else ".jpg"
