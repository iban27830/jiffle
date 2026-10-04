import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import NamedTuple
from urllib.parse import urlsplit
from uuid import uuid4

import imagehash
from PIL import Image

from jiffle.configuration.settings import Settings
from jiffle.features.duplicates.scan import (
    DEFAULT_MATCH_THRESHOLD,
    find_similar_media,
    register_pending_match,
)
from jiffle.features.imports.history import finalize_review_history
from jiffle.features.imports.local_import import atomic_copy, inspect_media
from jiffle.features.imports.source_adapters.contracts import SourceMedia, SourceProvider
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.features.imports.source_adapters.platform_tags import add_platform_tags
from jiffle.features.imports.universal_import import (
    _extension,
    _match_to_source,
    _record_provider_diagnostic,
    _reverse_similar,
    _safe_error_message,
    _select_metadata_source,
    resolve_exact_downloads,
)
from jiffle.infrastructure.database.connection import connect_database
from jiffle.infrastructure.media_revisions import create_original_revision


_UNSET = object()


class ReviewFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ReviewAcceptance(NamedTuple):
    """What accepting a review item did to the library."""

    media_item_id: int
    outcome: str  # "created" | "merged" | "duplicate_pending"
    duplicate_of: int | None = None


def accept_review_item(
    connection: sqlite3.Connection,
    settings: Settings,
    review_id: int,
    source: SourceMedia | None = None,
    file_source: SourceMedia | None | object = _UNSET,
) -> ReviewAcceptance:
    row = _pending_review(connection, review_id)
    source = source or _candidate_source(row["source_metadata_json"])
    if file_source is _UNSET:
        # By default the metadata source is also where the bytes come from.
        # Passing an explicit ``None`` records a source without a file source,
        # for example when the user's own upload supplied the bytes.
        file_source = source
    staged = _staged_path(settings, row["stored_path"])
    if not staged.is_file():
        raise ReviewFailure("review.file_missing", "The staged file is unavailable.")

    if source:
        existing_source = connection.execute(
            "SELECT media_item_id FROM media_sources WHERE canonical_url = ?",
            (source.canonical_url,),
        ).fetchone()
        if existing_source:
            # The source post is already in the library.  Keep the single item
            # and merge its metadata; the stored file is never replaced.
            media_item_id = int(existing_source[0])
            _merge_source_into_existing(connection, media_item_id, source, file_source)
            _complete_review(
                connection, review_id, row["candidate_id"], media_item_id,
                source, file_source, outcome="merged", duplicate_of=media_item_id,
            )
            staged.unlink(missing_ok=True)
            _cleanup_source_candidates(connection, settings, review_id)
            return ReviewAcceptance(media_item_id, "merged", media_item_id)

    duplicate = connection.execute(
        "SELECT id FROM media_items WHERE content_hash = ? AND deleted_at IS NULL",
        (row["content_hash"],),
    ).fetchone()
    if duplicate:
        media_item_id = int(duplicate[0])
        _merge_source_into_existing(connection, media_item_id, source, file_source)
        _complete_review(
            connection, review_id, row["candidate_id"], media_item_id,
            source, file_source, outcome="merged", duplicate_of=media_item_id,
        )
        staged.unlink(missing_ok=True)
        _cleanup_source_candidates(connection, settings, review_id)
        return ReviewAcceptance(media_item_id, "merged", media_item_id)

    stored_path = atomic_copy(staged, settings.media_path, "media")
    try:
        cursor = connection.execute(
            "INSERT INTO media_items "
            "(file_path, media_type, source_url, file_source_url, author, domain, width, height, "
            "file_size, content_hash, parent_id, character_tags_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stored_path,
                row["media_type"],
                source.canonical_url if source else None,
                file_source.canonical_url if file_source else None,
                source.author if source else None,
                source.domain if source else None,
                row["width"],
                row["height"],
                row["file_size"],
                row["content_hash"],
                source.parent_id if source else None,
                json.dumps(list(source.character_tags)) if source else "[]",
            ),
        )
        media_item_id = int(cursor.lastrowid)
        create_original_revision(connection, media_item_id)
        perceptual_hash = _store_new_fingerprint(
            connection, media_item_id, row["media_type"],
            settings.media_path / stored_path,
        )
        if source:
            _store_source(connection, media_item_id, source)
            _store_tags(connection, media_item_id, source.tags)
        duplicate_of = _register_new_duplicates(connection, media_item_id, perceptual_hash)
        outcome = "duplicate_pending" if duplicate_of is not None else "created"
        _complete_review(
            connection, review_id, row["candidate_id"], media_item_id,
            source, file_source, outcome=outcome, duplicate_of=duplicate_of,
        )
    except Exception:
        (settings.media_path / stored_path).unlink(missing_ok=True)
        connection.rollback()
        raise
    staged.unlink(missing_ok=True)
    _cleanup_source_candidates(connection, settings, review_id)
    return ReviewAcceptance(media_item_id, outcome, duplicate_of)


def accept_source_candidate(
    connection: sqlite3.Connection,
    settings: Settings,
    review_id: int,
    candidate_id: int,
) -> ReviewAcceptance:
    """Accept one staged external source candidate and discard its siblings."""
    row = connection.execute(
        "SELECT review.status, review.import_candidate_id, candidate.content_hash AS input_hash, "
        "candidate.stored_path AS input_path, source.id, source.stored_path, source.media_type, "
        "source.content_hash, source.width, source.height, source.file_size, source.source_metadata_json, "
        "candidate.source_metadata_json AS candidate_source_metadata_json "
        "FROM review_items review JOIN import_candidates candidate ON candidate.id=review.import_candidate_id "
        "JOIN import_source_candidates source ON source.review_item_id=review.id "
        "WHERE review.id=? AND source.id=? AND source.status='pending'",
        (review_id, candidate_id),
    ).fetchone()
    if row is None:
        raise ReviewFailure("review.candidate_not_found", "The source candidate was not found.")
    if row["status"] != "pending":
        raise ReviewFailure("review.already_resolved", "Review item is already resolved.")
    if row["stored_path"] is None:
        raise ReviewFailure("review.file_missing", "The source candidate is unavailable.")
    selected_path = _staged_path(settings, row["stored_path"])
    if not selected_path.is_file():
        raise ReviewFailure("review.file_missing", "The source candidate is unavailable.")
    file_source = _candidate_source(row["source_metadata_json"])
    candidate_metadata = _candidate_source(row["candidate_source_metadata_json"])
    source = candidate_metadata or file_source
    source_existing = connection.execute(
        "SELECT id FROM media_items WHERE content_hash=? AND deleted_at IS NULL",
        (row["content_hash"],),
    ).fetchone()
    input_existing = None
    if row["input_hash"] and row["input_hash"] != row["content_hash"]:
        input_existing = connection.execute(
            "SELECT id FROM media_items WHERE content_hash=? AND deleted_at IS NULL",
            (row["input_hash"],),
        ).fetchone()

    if source_existing or input_existing:
        # The accepted file is already stored byte-for-byte.  Merge the source
        # metadata into that item; its file and revisions are never replaced.
        media_item_id = int((source_existing or input_existing)[0])
        _merge_source_into_existing(connection, media_item_id, source, file_source)
        outcome = "merged"
        duplicate_of = media_item_id
    else:
        stored_path = atomic_copy(selected_path, settings.media_path, "media")
        try:
            cursor = connection.execute(
                "INSERT INTO media_items (file_path, media_type, source_url, file_source_url, author, domain, width, height, file_size, content_hash, parent_id, character_tags_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (stored_path, row["media_type"], source.canonical_url if source else None,
                 file_source.canonical_url if file_source else None,
                 source.author if source else None, source.domain if source else None,
                 row["width"], row["height"], row["file_size"], row["content_hash"],
                 source.parent_id if source else None,
                 json.dumps(list(source.character_tags)) if source else "[]"),
            )
            media_item_id = int(cursor.lastrowid)
            create_original_revision(connection, media_item_id)
            perceptual_hash = _store_new_fingerprint(
                connection, media_item_id, row["media_type"],
                settings.media_path / stored_path,
            )
            if source:
                _store_source(connection, media_item_id, source)
                _store_tags(connection, media_item_id, source.tags)
            duplicate_of = _register_new_duplicates(connection, media_item_id, perceptual_hash)
            outcome = "duplicate_pending" if duplicate_of is not None else "created"
        except Exception:
            (settings.media_path / stored_path).unlink(missing_ok=True)
            connection.rollback()
            raise
    connection.execute(
        "UPDATE import_candidates SET status='accepted', media_item_id=? WHERE id=?",
        (media_item_id, row["import_candidate_id"]),
    )
    connection.execute(
        "UPDATE review_items SET status='accepted', resolved_at=CURRENT_TIMESTAMP WHERE id=?",
        (review_id,),
    )
    connection.execute(
        "UPDATE import_source_candidates SET status=CASE WHEN id=? THEN 'selected' ELSE 'rejected' END, resolved_at=CURRENT_TIMESTAMP WHERE review_item_id=?",
        (candidate_id, review_id),
    )
    _history(connection, "review.accepted", review_id, {
        "media_item_id": media_item_id,
        "source_candidate_id": candidate_id,
        "source_url": source.canonical_url if source else None,
        "file_source_url": file_source.canonical_url if file_source else None,
        "outcome": outcome,
        "duplicate_of": duplicate_of,
    })
    finalize_review_history(connection, review_id, outcome, media_item_id, duplicate_of)
    _delete_review_search_attempts(connection, review_id)
    connection.commit()
    _remove_review_staging(connection, settings, review_id, keep=None, primary_path=row["input_path"])
    return ReviewAcceptance(media_item_id, outcome, duplicate_of)


def reject_review_item(
    connection: sqlite3.Connection, settings: Settings, review_id: int
) -> None:
    row = _pending_review(connection, review_id)
    staged = _staged_path(settings, row["stored_path"])
    rejecting = staged.with_suffix(staged.suffix + ".rejecting")
    if staged.is_file():
        os.replace(staged, rejecting)
    try:
        connection.execute(
            "UPDATE review_items SET status='rejected', resolved_at=CURRENT_TIMESTAMP "
            "WHERE id=?", (review_id,)
        )
        connection.execute(
            "UPDATE import_source_candidates SET status='rejected', resolved_at=CURRENT_TIMESTAMP WHERE review_item_id=?",
            (review_id,),
        )
        _history(connection, "review.rejected", review_id, {})
        finalize_review_history(connection, review_id, "rejected")
        _delete_review_search_attempts(connection, review_id)
        connection.commit()
    except Exception:
        connection.rollback()
        if rejecting.is_file():
            os.replace(rejecting, staged)
        raise
    rejecting.unlink(missing_ok=True)
    for candidate in connection.execute(
        "SELECT stored_path FROM import_source_candidates WHERE review_item_id=?", (review_id,)
    ).fetchall():
        if candidate[0]:
            _staged_path(settings, candidate[0]).unlink(missing_ok=True)


def create_manual_source_job(
    connection: sqlite3.Connection, review_id: int, source_url: str
) -> int:
    _pending_review(connection, review_id)
    cursor = connection.execute(
        "INSERT INTO background_jobs (job_type, status, result_json) "
        "VALUES ('review_source_resolution', 'pending', ?)",
        (json.dumps({"review_item_id": review_id, "source_url": source_url}),),
    )
    connection.commit()
    return int(cursor.lastrowid)


def run_manual_source_job(
    database_path: Path,
    settings: Settings,
    job_id: int,
    review_id: int,
    source_url: str,
    provider: SourceProvider,
) -> None:
    connection = connect_database(database_path)
    try:
        connection.execute(
            "UPDATE background_jobs SET status='running', progress=20, "
            "started_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,)
        )
        connection.commit()
        source = provider.fetch(source_url)
        acceptance = accept_review_item(connection, settings, review_id, source, source)
        result = json.dumps({
            "outcome": "accepted", "review_item_id": review_id,
            "media_item_id": acceptance.media_item_id,
            "review_outcome": acceptance.outcome,
            "duplicate_of": acceptance.duplicate_of,
        })
        connection.execute(
            "UPDATE background_jobs SET status='completed', progress=100, result_json=?, "
            "finished_at=CURRENT_TIMESTAMP WHERE id=?", (result, job_id)
        )
        connection.commit()
    except (ReviewFailure, SourceProviderFailure) as error:
        _fail_job(connection, job_id, error.code, error.message)
        _record_review_search_attempt(connection, review_id, {
            "status": "unavailable", "code": error.code, "message": error.message,
        })
    except Exception:
        _fail_job(
            connection, job_id, "review.source_resolution_failed",
            "The source could not be applied to this review item.",
        )
        _record_review_search_attempt(connection, review_id, {
            "status": "unavailable", "code": "review.source_resolution_failed",
            "message": "The source could not be applied to this review item.",
        })
        raise
    finally:
        connection.close()


def create_review_reimport_job(connection: sqlite3.Connection, review_ids) -> int:
    """Queue a recheck of pending review items against the current providers."""
    unique_ids = list(dict.fromkeys(int(value) for value in review_ids))
    if not unique_ids:
        raise ReviewFailure("review.invalid_request", "At least one review item is required.")
    placeholders = ",".join("?" for _ in unique_ids)
    pending = [
        int(row[0])
        for row in connection.execute(
            f"SELECT id FROM review_items WHERE status='pending' AND id IN ({placeholders})",
            unique_ids,
        ).fetchall()
    ]
    if not pending:
        raise ReviewFailure("review.not_found", "The selected review items are no longer pending.")
    pending.sort(key=unique_ids.index)
    cursor = connection.execute(
        "INSERT INTO background_jobs (job_type, status, result_json) "
        "VALUES ('review_reimport', 'pending', ?)",
        (json.dumps({"review_item_ids": pending}),),
    )
    connection.commit()
    return int(cursor.lastrowid)


def run_review_reimport_job(
    database_path: Path,
    settings: Settings,
    job_id: int,
    review_ids,
    providers: tuple[SourceProvider, ...],
    downloader: object,
) -> None:
    """Re-run source resolution for staged review items.

    Rechecking is useful after the user adds or reconfigures a source provider:
    a photo that had no source may now be found.  When a verified source is
    found the item is accepted with the richest metadata available, keeping the
    metadata source and the file source separate when they differ.
    """
    connection = connect_database(database_path)
    try:
        stored = connection.execute(
            "SELECT result_json FROM background_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if stored is not None and stored["result_json"]:
            try:
                queued = json.loads(stored["result_json"]).get("review_item_ids")
            except (TypeError, ValueError):
                queued = None
            if isinstance(queued, list) and queued:
                review_ids = [int(value) for value in queued]
        connection.execute(
            "UPDATE background_jobs SET status='running', progress=5, "
            "started_at=CURRENT_TIMESTAMP WHERE id=?",
            (job_id,),
        )
        connection.commit()
        results: list[dict[str, object]] = []
        total = max(1, len(review_ids))
        for index, review_id in enumerate(review_ids, start=1):
            try:
                result = _reimport_review_item(
                    connection, settings, int(review_id), providers, downloader
                )
            except ReviewFailure as error:
                result = {
                    "review_item_id": int(review_id),
                    "status": "unavailable",
                    "code": error.code,
                    "message": error.message,
                }
            except Exception:
                connection.rollback()
                result = {
                    "review_item_id": int(review_id),
                    "status": "unavailable",
                    "code": "review.reimport_failed",
                    "message": "The source could not be rechecked.",
                }
            results.append(result)
            _record_review_search_attempt(connection, int(review_id), result)
            connection.execute(
                "UPDATE background_jobs SET progress=?, result_json=? WHERE id=?",
                (
                    5 + int(90 * index / total),
                    json.dumps({"outcome": "running", "items": results}),
                    job_id,
                ),
            )
            connection.commit()
        summary = {
            "outcome": "completed",
            "items": results,
            "accepted": sum(1 for item in results if item["status"] == "accepted"),
            "candidates": sum(1 for item in results if item["status"] == "candidates"),
            "no_source": sum(1 for item in results if item["status"] == "no_source"),
            "unavailable": sum(1 for item in results if item["status"] == "unavailable"),
        }
        connection.execute(
            "UPDATE background_jobs SET status='completed', progress=100, result_json=?, "
            "finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (json.dumps(summary), job_id),
        )
        connection.commit()
    except Exception:
        _fail_job(
            connection, job_id, "review.reimport_failed",
            "The selected items could not be rechecked.",
        )
        raise
    finally:
        connection.close()


def _reimport_review_item(
    connection: sqlite3.Connection,
    settings: Settings,
    review_id: int,
    providers,
    downloader,
) -> dict[str, object]:
    row = _pending_review(connection, review_id)
    staged = _staged_path(settings, row["stored_path"])
    if not staged.is_file():
        raise ReviewFailure("review.file_missing", "The staged file is unavailable.")
    diagnostics: list[dict[str, object]] = []
    matches, verified, errors = resolve_exact_downloads(
        staged, providers, downloader, settings, diagnostics
    )
    try:
        if verified:
            file_match = verified[0][0]
            # The richest tags are not always on the provider whose bytes are
            # downloadable, so rank every exact match for metadata and take the
            # file from the copy that verified.
            metadata_match = _select_metadata_source(matches) or file_match
            source = _match_to_source(metadata_match)
            file_source = _match_to_source(file_match)
            acceptance = accept_review_item(
                connection, settings, review_id, source, file_source
            )
            return {
                "review_item_id": review_id,
                "status": "accepted",
                "media_item_id": acceptance.media_item_id,
                "review_outcome": acceptance.outcome,
                "duplicate_of": acceptance.duplicate_of,
                "provider": source.provider,
                "file_provider": file_source.provider,
                "source_url": source.canonical_url,
            }
        if not matches:
            # No byte-identical copy exists, so look for an approximate match
            # and offer it for confirmation instead of giving up silently.
            similar = _reverse_similar(staged, providers, diagnostics)
            stored_candidates = _stage_reverse_candidates(
                connection, settings, review_id, similar, downloader, diagnostics
            ) if similar else 0
            if stored_candidates:
                return {
                    "review_item_id": review_id,
                    "status": "candidates",
                    "candidate_count": stored_candidates,
                    "provider_diagnostics": diagnostics,
                }
            return {
                "review_item_id": review_id,
                "status": "no_source",
                "provider_errors": errors,
                "provider_diagnostics": diagnostics,
            }
        # The providers know the exact source but no copy is downloadable.
        # The user's own staged file already has the right bytes, so keep it
        # and apply the richest metadata without a file source.
        metadata_match = _select_metadata_source(matches)
        if metadata_match is None:
            return {
                "review_item_id": review_id,
                "status": "no_source",
                "provider_errors": errors,
                "provider_diagnostics": diagnostics,
            }
        source = _match_to_source(metadata_match)
        acceptance = accept_review_item(
            connection, settings, review_id, source, None
        )
        return {
            "review_item_id": review_id,
            "status": "accepted",
            "media_item_id": acceptance.media_item_id,
            "review_outcome": acceptance.outcome,
            "duplicate_of": acceptance.duplicate_of,
            "provider": source.provider,
            "metadata_only": True,
            "source_url": source.canonical_url,
        }
    finally:
        for _match, path in verified:
            path.unlink(missing_ok=True)


def _stage_reverse_candidates(
    connection: sqlite3.Connection,
    settings: Settings,
    review_id: int,
    matches,
    downloader,
    diagnostics: list[dict[str, object]],
) -> int:
    """Store downloadable perceptual matches as confirmable review candidates.

    Returns the number of newly stored candidates.  Matches that are already
    attached to the review item are skipped, and a match whose media cannot be
    downloaded is reported in the diagnostics.
    """
    settings.resolved_import_staging_path.mkdir(parents=True, exist_ok=True)
    existing = set()
    for row in connection.execute(
        "SELECT provider, source_metadata_json FROM import_source_candidates "
        "WHERE review_item_id=?",
        (review_id,),
    ):
        try:
            metadata = json.loads(row["source_metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        existing.add((row["provider"], str(metadata.get("remote_id") or "")))

    stored: list[tuple[object, str, object]] = []
    for match in matches:
        key = (match.provider, str(match.remote_id or ""))
        if key in existing:
            continue
        # Only a real download from the matched post becomes a confirmable
        # candidate.  A reverse-search result whose post could not be loaded is
        # reported instead of staging the service's cropped preview as if it
        # were the source file.
        media_url = match.direct_media_url
        if not media_url:
            _record_provider_diagnostic(
                diagnostics, "perceptual_search", match.provider, "unavailable", 0,
                "import.source_link_unresolved",
                "A similar source was found, but its post could not be loaded, so "
                "only a search preview is available. It is not offered as a file.",
                remote_id=match.remote_id,
            )
            continue
        name = f"candidate-{uuid4().hex}{_extension(media_url)}"
        path = settings.resolved_import_staging_path / name
        try:
            local_candidate = Path(str(media_url))
            if local_candidate.is_file():
                shutil.copy2(local_candidate, path)
            else:
                downloader.download(media_url, path, match.canonical_url)
            inspection = inspect_media(path)
        except Exception as error:
            path.unlink(missing_ok=True)
            _record_provider_diagnostic(
                diagnostics, "perceptual_search", match.provider, "unavailable", 0,
                "import.candidate_unavailable",
                _safe_error_message(error, "The similar candidate was unavailable."),
                remote_id=match.remote_id,
            )
            continue
        stored.append((match, name, inspection))
        existing.add(key)
    if not stored:
        return 0
    connection.execute(
        "UPDATE review_items SET reason='source_candidates' WHERE id=?", (review_id,)
    )
    for rank, (match, name, inspection) in enumerate(stored):
        connection.execute(
            "INSERT INTO import_source_candidates "
            "(review_item_id, rank, match_method, confidence, provider, source_metadata_json, "
            "stored_path, media_type, content_hash, width, height, file_size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                review_id, rank, match.match_method, match.confidence, match.provider,
                json.dumps(match.as_dict()), name, inspection.media_type,
                inspection.content_hash, inspection.width, inspection.height,
                inspection.file_size,
            ),
        )
    connection.commit()
    return len(stored)


def _pending_review(connection: sqlite3.Connection, review_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT review.id, review.status, candidate.id AS candidate_id, "
        "candidate.stored_path, candidate.media_type, candidate.content_hash, "
        "candidate.width, candidate.height, candidate.file_size, "
        "candidate.source_metadata_json "
        "FROM review_items review JOIN import_candidates candidate "
        "ON candidate.id=review.import_candidate_id WHERE review.id=?",
        (review_id,),
    ).fetchone()
    if row is None:
        raise ReviewFailure("review.not_found", "Review item was not found.")
    if row["status"] != "pending":
        raise ReviewFailure("review.already_resolved", "Review item is already resolved.")
    if not row["stored_path"]:
        raise ReviewFailure("review.file_missing", "The staged file is unavailable.")
    return row


def _candidate_source(raw_metadata: str | None) -> SourceMedia | None:
    if not raw_metadata:
        return None
    payload = json.loads(raw_metadata)
    domain = payload.get("domain") or urlsplit(payload["canonical_url"]).netloc
    return SourceMedia(
        canonical_url=payload["canonical_url"],
        direct_media_url=payload.get("direct_media_url"),
        provider=payload["provider"],
        remote_id=payload["remote_id"],
        author=payload.get("author"),
        domain=domain,
        tags=add_platform_tags(payload.get("tags", ()), payload["provider"], domain),
        file_extension=payload.get("file_extension") or (os.path.splitext(urlsplit(payload.get("direct_media_url") or payload["canonical_url"]).path)[1] or ".jpg"),
        character_tags=tuple(payload.get("character_tags", ())),
        parent_id=payload.get("parent_id"),
        content_md5=payload.get("content_md5"),
    )


def _remove_review_staging(connection, settings: Settings, review_id: int, keep: int | None = None, primary_path: str | None = None) -> None:
    rows = connection.execute(
        "SELECT stored_path FROM import_source_candidates WHERE review_item_id=? AND (? IS NULL OR id<>?)",
        (review_id, keep, keep),
    ).fetchall()
    for row in rows:
        if row[0]:
            _staged_path(settings, row[0]).unlink(missing_ok=True)
    if primary_path:
        _staged_path(settings, primary_path).unlink(missing_ok=True)


def _cleanup_source_candidates(connection, settings: Settings, review_id: int) -> None:
    for row in connection.execute(
        "SELECT stored_path FROM import_source_candidates WHERE review_item_id=?", (review_id,)
    ).fetchall():
        if row[0]:
            _staged_path(settings, row[0]).unlink(missing_ok=True)


def _staged_path(settings: Settings, stored_path: str) -> Path:
    root = settings.resolved_import_staging_path.resolve()
    candidate = (root / stored_path).resolve()
    if not candidate.is_relative_to(root):
        raise ReviewFailure("review.file_missing", "The staged file is unavailable.")
    return candidate


def _decode_json_list(raw) -> tuple[str, ...]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return ()
    if not isinstance(values, list):
        return ()
    return tuple(str(value) for value in values if str(value).strip())


def _merge_source_into_existing(
    connection: sqlite3.Connection,
    media_item_id: int,
    source: SourceMedia | None,
    file_source: SourceMedia | None = None,
) -> None:
    """Add source metadata to an existing item without replacing its file.

    Only missing fields are filled and tags/characters are unioned, so merging a
    duplicate never rewrites the user's author, source, file or revisions.  A
    second source URL cannot be recorded because ``media_sources.canonical_url``
    is unique; the existing row is kept in that case.
    """
    if source is not None and source.tags:
        _store_tags(connection, media_item_id, source.tags)
    item = connection.execute(
        "SELECT author, domain, source_url, character_tags_json FROM media_items WHERE id=?",
        (media_item_id,),
    ).fetchone()
    if item is None:
        return
    item_updates: dict[str, object] = {}
    if source is not None:
        if not item["author"] and source.author:
            item_updates["author"] = source.author
        if not item["domain"] and source.domain:
            item_updates["domain"] = source.domain
        if not item["source_url"] and source.canonical_url:
            item_updates["source_url"] = source.canonical_url
    item_characters = set(_decode_json_list(item["character_tags_json"]))
    merged_characters = set(item_characters)
    if source is not None:
        merged_characters |= {str(value) for value in source.character_tags}
    if sorted(merged_characters) != sorted(item_characters):
        item_updates["character_tags_json"] = json.dumps(sorted(merged_characters))
    if item_updates:
        assignments = ", ".join(f"{field}=?" for field in item_updates)
        connection.execute(
            f"UPDATE media_items SET {assignments} WHERE id=?",
            (*item_updates.values(), media_item_id),
        )
    if source is not None:
        _merge_media_sources(connection, media_item_id, source)
    if file_source is not None and getattr(file_source, "canonical_url", None):
        connection.execute(
            "UPDATE media_items SET file_source_url=? "
            "WHERE id=? AND (file_source_url IS NULL OR TRIM(file_source_url)='')",
            (file_source.canonical_url, media_item_id),
        )


def _merge_media_sources(
    connection: sqlite3.Connection, media_item_id: int, source: SourceMedia
) -> None:
    existing = connection.execute(
        "SELECT canonical_url, direct_media_url, author, domain, parent_id, character_tags_json "
        "FROM media_sources WHERE media_item_id=?",
        (media_item_id,),
    ).fetchone()
    if existing is None:
        try:
            connection.execute(
                "INSERT INTO media_sources "
                "(media_item_id, canonical_url, direct_media_url, provider, remote_id, author, domain, parent_id, character_tags_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    media_item_id, source.canonical_url, source.direct_media_url,
                    source.provider, source.remote_id, source.author, source.domain,
                    source.parent_id, json.dumps(list(source.character_tags)),
                ),
            )
        except sqlite3.IntegrityError:
            # The URL already belongs to another (possibly deleted) item; the
            # stored metadata is a bonus, not a reason to fail the acceptance.
            pass
        return
    updates: dict[str, object] = {}
    if not existing["direct_media_url"] and source.direct_media_url:
        updates["direct_media_url"] = source.direct_media_url
    if not existing["author"] and source.author:
        updates["author"] = source.author
    if not existing["domain"] and source.domain:
        updates["domain"] = source.domain
    if not existing["parent_id"] and source.parent_id:
        updates["parent_id"] = source.parent_id
    current_characters = set(_decode_json_list(existing["character_tags_json"]))
    merged = current_characters | {str(value) for value in source.character_tags}
    if sorted(merged) != sorted(current_characters):
        updates["character_tags_json"] = json.dumps(sorted(merged))
    if updates:
        assignments = ", ".join(f"{field}=?" for field in updates)
        connection.execute(
            f"UPDATE media_sources SET {assignments} WHERE media_item_id=?",
            (*updates.values(), media_item_id),
        )


def _store_new_fingerprint(
    connection: sqlite3.Connection,
    media_item_id: int,
    media_type: str,
    path: Path,
) -> str | None:
    """Cache the pHash of a newly stored image and return it as hex."""
    if media_type != "image" or path is None or not path.is_file():
        return None
    try:
        with Image.open(path) as image:
            fingerprint = str(imagehash.phash(image))
    except (OSError, ValueError):
        return None
    connection.execute(
        "INSERT INTO media_fingerprints (media_item_id, perceptual_hash) VALUES (?, ?) "
        "ON CONFLICT(media_item_id) DO UPDATE SET "
        "perceptual_hash=excluded.perceptual_hash, updated_at=CURRENT_TIMESTAMP",
        (media_item_id, fingerprint),
    )
    return fingerprint


def _register_new_duplicates(
    connection: sqlite3.Connection,
    media_item_id: int,
    perceptual_hash: str | None,
) -> int | None:
    """Queue pending Duplicates pairs for a freshly accepted image."""
    if not perceptual_hash:
        return None
    matches = find_similar_media(
        connection, perceptual_hash, DEFAULT_MATCH_THRESHOLD,
        exclude_media_id=media_item_id,
    )
    for other_media_id, confidence in matches:
        register_pending_match(connection, media_item_id, other_media_id, confidence)
    return matches[0][0] if matches else None


def _complete_review(
    connection,
    review_id,
    candidate_id,
    media_item_id,
    source,
    file_source=None,
    *,
    outcome="created",
    duplicate_of=None,
):
    connection.execute(
        "UPDATE import_candidates SET status='accepted', media_item_id=? WHERE id=?",
        (media_item_id, candidate_id),
    )
    connection.execute(
        "UPDATE review_items SET status='accepted', resolved_at=CURRENT_TIMESTAMP WHERE id=?",
        (review_id,),
    )
    connection.execute(
        "UPDATE import_source_candidates SET status='rejected', resolved_at=CURRENT_TIMESTAMP WHERE review_item_id=?",
        (review_id,),
    )
    _history(connection, "review.accepted", review_id, {
        "media_item_id": media_item_id,
        "source_url": source.canonical_url if source else None,
        "file_source_url": file_source.canonical_url if file_source else None,
        "outcome": outcome,
        "duplicate_of": duplicate_of,
    })
    finalize_review_history(connection, review_id, outcome, media_item_id, duplicate_of)
    _delete_review_search_attempts(connection, review_id)
    connection.commit()


def _store_source(connection, media_item_id, source):
    values = (
        media_item_id, source.canonical_url, source.direct_media_url,
        source.provider, source.remote_id, source.author, source.domain,
        source.parent_id, json.dumps(list(source.character_tags)),
    )
    try:
        connection.execute(
            "INSERT INTO media_sources "
            "(media_item_id, canonical_url, direct_media_url, provider, remote_id, author, domain, parent_id, character_tags_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(media_item_id) DO UPDATE SET "
            "canonical_url=excluded.canonical_url, direct_media_url=excluded.direct_media_url, "
            "provider=excluded.provider, remote_id=excluded.remote_id, "
            "author=excluded.author, domain=excluded.domain, parent_id=excluded.parent_id, "
            "character_tags_json=excluded.character_tags_json",
            values,
        )
    except sqlite3.IntegrityError:
        owner = connection.execute(
            "SELECT media_item_id FROM media_sources WHERE canonical_url=?",
            (source.canonical_url,),
        ).fetchone()
        if owner is None or int(owner[0]) == int(media_item_id):
            raise
        deleted = connection.execute(
            "SELECT deleted_at FROM media_items WHERE id=?", (owner[0],)
        ).fetchone()
        if deleted is None or deleted[0] is None:
            raise
        target = connection.execute(
            "SELECT 1 FROM media_sources WHERE media_item_id=?", (media_item_id,)
        ).fetchone()
        if target:
            raise
        connection.execute(
            "UPDATE media_sources SET media_item_id=? WHERE media_item_id=?",
            (media_item_id, owner[0]),
        )
        connection.execute(
            "UPDATE media_sources SET direct_media_url=?, provider=?, remote_id=?, author=?, domain=?, parent_id=?, character_tags_json=? WHERE media_item_id=?",
            (source.direct_media_url, source.provider, source.remote_id, source.author,
             source.domain, source.parent_id, json.dumps(list(source.character_tags)), media_item_id),
        )
    connection.execute(
        "UPDATE media_items SET source_url=?, author=?, domain=? WHERE id=?",
        (source.canonical_url, source.author, source.domain, media_item_id),
    )
    connection.execute(
        "UPDATE media_items SET parent_id=?, character_tags_json=? WHERE id=?",
        (source.parent_id, json.dumps(list(source.character_tags)), media_item_id),
    )


def _store_tags(connection, media_item_id, tags):
    connection.executemany(
        "INSERT OR IGNORE INTO media_tags (media_item_id, tag) VALUES (?, ?)",
        ((media_item_id, tag) for tag in sorted(set(tags))),
    )


def _history(connection, event_type, review_id, details):
    connection.execute(
        "INSERT INTO operation_history "
        "(event_type, entity_type, entity_id, details_json) VALUES (?, 'review_item', ?, ?)",
        (event_type, review_id, json.dumps(details)),
    )


def _record_review_search_attempt(connection, review_id, result) -> None:
    """Log one source search for a still-pending review card.

    The card uses these rows to mark itself as already searched and to show the
    user what happened.  Resolved items keep no log, so the accepted or rejected
    card does not leave rows behind.
    """
    status = str(result.get("status") or "unavailable")
    if status == "accepted":
        return
    pending = connection.execute(
        "SELECT 1 FROM review_items WHERE id=? AND status='pending'", (review_id,)
    ).fetchone()
    if pending is None:
        return
    details = {
        key: result[key]
        for key in (
            "candidate_count", "provider", "file_provider", "source_url",
            "metadata_only", "provider_errors", "provider_diagnostics",
        )
        if result.get(key) is not None
    }
    connection.execute(
        "INSERT INTO review_search_attempts "
        "(review_item_id, outcome, code, message, details_json) VALUES (?, ?, ?, ?, ?)",
        (
            review_id, status, result.get("code"), result.get("message"),
            json.dumps(details),
        ),
    )
    connection.commit()


def _delete_review_search_attempts(connection, review_id) -> None:
    """Discard a card's search log once the card is resolved."""
    connection.execute(
        "DELETE FROM review_search_attempts WHERE review_item_id=?", (review_id,)
    )


def _fail_job(connection, job_id, code, message):
    connection.rollback()
    connection.execute(
        "UPDATE background_jobs SET status='failed', error_code=?, error_message=?, "
        "finished_at=CURRENT_TIMESTAMP WHERE id=?", (code, message, job_id)
    )
    connection.commit()


def create_metadata_refresh_job(connection: sqlite3.Connection, media_item_id: int) -> int:
    row = connection.execute(
        "SELECT item.source_url, COALESCE(source.provider, '') AS provider FROM media_items item "
        "LEFT JOIN media_sources source ON source.media_item_id=item.id "
        "WHERE item.id=? AND item.deleted_at IS NULL",
        (media_item_id,),
    ).fetchone()
    if row is None:
        raise ReviewFailure("metadata.source_missing", "This media has no supported source metadata.")
    if not row["source_url"]:
        raise ReviewFailure("metadata.source_missing", "This media has no supported source metadata.")
    pending = connection.execute(
        "SELECT id FROM metadata_suggestions WHERE media_item_id=? AND status='pending'",
        (media_item_id,),
    ).fetchone()
    if pending:
        raise ReviewFailure("metadata.already_pending", "Metadata refresh is already waiting for review.")
    cursor = connection.execute(
        "INSERT INTO background_jobs (job_type, status, result_json) VALUES ('metadata_refresh', 'pending', ?)",
        (json.dumps({"media_item_id": media_item_id}),),
    )
    job_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO metadata_suggestions (job_id, media_item_id, provider) VALUES (?, ?, ?)",
        (job_id, media_item_id, row["provider"]),
    )
    connection.commit()
    return job_id


def run_metadata_refresh_job(
    database_path: Path,
    job_id: int,
    media_item_id: int,
    provider: SourceProvider,
) -> None:
    connection = connect_database(database_path)
    try:
        connection.execute(
            "UPDATE background_jobs SET status='running', progress=20, started_at=CURRENT_TIMESTAMP WHERE id=?",
            (job_id,),
        )
        connection.commit()
        row = connection.execute(
            "SELECT source_url FROM media_items WHERE id=? AND deleted_at IS NULL", (media_item_id,)
        ).fetchone()
        if row is None:
            raise ReviewFailure("metadata.source_missing", "This media has no source URL to refresh.")
        source = provider.fetch(row["source_url"])
        payload = _source_payload(source)
        connection.execute(
            "UPDATE metadata_suggestions SET source_metadata_json=? WHERE job_id=?",
            (json.dumps(payload), job_id),
        )
        result = json.dumps({"outcome": "review", "media_item_id": media_item_id, "suggestion_id": connection.execute("SELECT id FROM metadata_suggestions WHERE job_id=?", (job_id,)).fetchone()[0]})
        connection.execute(
            "UPDATE background_jobs SET status='completed', progress=100, result_json=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (result, job_id),
        )
        connection.commit()
    except (ReviewFailure, SourceProviderFailure) as error:
        _fail_metadata_job(connection, job_id, error.code, error.message)
    except Exception:
        _fail_metadata_job(
            connection, job_id, "metadata.refresh_failed",
            "Source metadata could not be refreshed.",
        )
        raise
    finally:
        connection.close()


def accept_metadata_suggestion(connection: sqlite3.Connection, suggestion_id: int) -> int:
    row = connection.execute(
        "SELECT id, media_item_id, status, source_metadata_json FROM metadata_suggestions WHERE id=?",
        (suggestion_id,),
    ).fetchone()
    if row is None:
        raise ReviewFailure("metadata.not_found", "Metadata suggestion was not found.")
    if row["status"] != "pending":
        raise ReviewFailure("metadata.already_resolved", "Metadata suggestion is already resolved.")
    try:
        source = _candidate_source(row["source_metadata_json"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ReviewFailure("metadata.invalid", "The fetched metadata is invalid.") from error
    if source is None:
        raise ReviewFailure("metadata.invalid", "The fetched metadata is empty.")
    media = connection.execute("SELECT id FROM media_items WHERE id=? AND deleted_at IS NULL", (row["media_item_id"],)).fetchone()
    if media is None:
        raise ReviewFailure("metadata.media_not_found", "Media item was not found.")
    _store_source(connection, int(row["media_item_id"]), source)
    _store_tags(connection, int(row["media_item_id"]), source.tags)
    connection.execute(
        "UPDATE metadata_suggestions SET status='accepted', resolved_at=CURRENT_TIMESTAMP WHERE id=?",
        (suggestion_id,),
    )
    _history(connection, "metadata.accepted", suggestion_id, {"media_item_id": int(row["media_item_id"])})
    connection.commit()
    return int(row["media_item_id"])


def reject_metadata_suggestion(connection: sqlite3.Connection, suggestion_id: int) -> None:
    row = connection.execute(
        "SELECT id, status FROM metadata_suggestions WHERE id=?", (suggestion_id,)
    ).fetchone()
    if row is None:
        raise ReviewFailure("metadata.not_found", "Metadata suggestion was not found.")
    if row["status"] != "pending":
        raise ReviewFailure("metadata.already_resolved", "Metadata suggestion is already resolved.")
    connection.execute(
        "UPDATE metadata_suggestions SET status='rejected', resolved_at=CURRENT_TIMESTAMP WHERE id=?",
        (suggestion_id,),
    )
    _history(connection, "metadata.rejected", suggestion_id, {})
    connection.commit()


def _source_payload(source: SourceMedia) -> dict[str, object]:
    return {
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
    }


def _fail_metadata_job(connection, job_id, code, message):
    connection.rollback()
    connection.execute("DELETE FROM metadata_suggestions WHERE job_id=?", (job_id,))
    connection.execute(
        "UPDATE background_jobs SET status='failed', error_code=?, error_message=?, "
        "finished_at=CURRENT_TIMESTAMP WHERE id=?",
        (code, message, job_id),
    )
    connection.commit()
