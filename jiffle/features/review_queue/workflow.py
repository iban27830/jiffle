import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

import imagehash
from PIL import Image

from jiffle.configuration.settings import Settings
from jiffle.features.imports.local_import import atomic_copy
from jiffle.features.imports.source_adapters.contracts import SourceMedia, SourceProvider
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.features.imports.universal_import import (
    _match_to_source,
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


def accept_review_item(
    connection: sqlite3.Connection,
    settings: Settings,
    review_id: int,
    source: SourceMedia | None = None,
    file_source: SourceMedia | None | object = _UNSET,
) -> int:
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
            media_item_id = int(existing_source[0])
            if file_source:
                connection.execute(
                    "UPDATE media_items SET file_source_url=? WHERE id=?",
                    (file_source.canonical_url, media_item_id),
                )
            _complete_review(
                connection, review_id, row["candidate_id"], media_item_id, source, file_source
            )
            staged.unlink(missing_ok=True)
            _cleanup_source_candidates(connection, settings, review_id)
            return media_item_id

    duplicate = connection.execute(
        "SELECT id FROM media_items WHERE content_hash = ?", (row["content_hash"],)
    ).fetchone()
    if duplicate:
        media_item_id = int(duplicate[0])
        if source:
            _store_source(connection, media_item_id, source)
            _store_tags(connection, media_item_id, source.tags)
        if file_source:
            connection.execute(
                "UPDATE media_items SET file_source_url=? WHERE id=?",
                (file_source.canonical_url, media_item_id),
            )
        _complete_review(connection, review_id, row["candidate_id"], media_item_id, source, file_source)
        staged.unlink(missing_ok=True)
        _cleanup_source_candidates(connection, settings, review_id)
        return media_item_id

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
        if source:
            _store_source(connection, media_item_id, source)
            _store_tags(connection, media_item_id, source.tags)
        _complete_review(connection, review_id, row["candidate_id"], media_item_id, source, file_source)
    except Exception:
        (settings.media_path / stored_path).unlink(missing_ok=True)
        connection.rollback()
        raise
    staged.unlink(missing_ok=True)
    _cleanup_source_candidates(connection, settings, review_id)
    return media_item_id


def accept_source_candidate(
    connection: sqlite3.Connection,
    settings: Settings,
    review_id: int,
    candidate_id: int,
) -> int:
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
    duplicate = connection.execute(
        "SELECT id FROM media_items WHERE content_hash=? AND deleted_at IS NULL",
        (row["content_hash"],),
    ).fetchone()
    stored_path = None
    if duplicate:
        media_item_id = int(duplicate[0])
        if source:
            _store_source(connection, media_item_id, source)
            _store_tags(connection, media_item_id, source.tags)
        if file_source:
            connection.execute(
                "UPDATE media_items SET file_source_url=? WHERE id=?",
                (file_source.canonical_url, media_item_id),
            )
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
            if row["media_type"] == "image":
                try:
                    with Image.open(selected_path) as image:
                        connection.execute(
                            "INSERT INTO media_fingerprints (media_item_id, perceptual_hash) VALUES (?, ?)",
                            (media_item_id, str(imagehash.phash(image))),
                        )
                except (OSError, ValueError):
                    pass
            if source:
                _store_source(connection, media_item_id, source)
                _store_tags(connection, media_item_id, source.tags)
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
    })
    connection.commit()
    _remove_review_staging(connection, settings, review_id, keep=None, primary_path=row["input_path"])
    return media_item_id


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
        media_item_id = accept_review_item(connection, settings, review_id, source, source)
        result = json.dumps({
            "outcome": "accepted", "review_item_id": review_id,
            "media_item_id": media_item_id,
        })
        connection.execute(
            "UPDATE background_jobs SET status='completed', progress=100, result_json=?, "
            "finished_at=CURRENT_TIMESTAMP WHERE id=?", (result, job_id)
        )
        connection.commit()
    except (ReviewFailure, SourceProviderFailure) as error:
        _fail_job(connection, job_id, error.code, error.message)
    except Exception:
        _fail_job(
            connection, job_id, "review.source_resolution_failed",
            "The source could not be applied to this review item.",
        )
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
        if not matches:
            return {
                "review_item_id": review_id,
                "status": "no_source",
                "provider_errors": errors,
                "provider_diagnostics": diagnostics,
            }
        if verified:
            file_match = verified[0][0]
            # The richest tags are not always on the provider whose bytes are
            # downloadable, so rank every exact match for metadata and take the
            # file from the copy that verified.
            metadata_match = _select_metadata_source(matches) or file_match
            source = _match_to_source(metadata_match)
            file_source = _match_to_source(file_match)
            media_item_id = accept_review_item(
                connection, settings, review_id, source, file_source
            )
            return {
                "review_item_id": review_id,
                "status": "accepted",
                "media_item_id": media_item_id,
                "provider": source.provider,
                "file_provider": file_source.provider,
                "source_url": source.canonical_url,
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
        media_item_id = accept_review_item(
            connection, settings, review_id, source, None
        )
        return {
            "review_item_id": review_id,
            "status": "accepted",
            "media_item_id": media_item_id,
            "provider": source.provider,
            "metadata_only": True,
            "source_url": source.canonical_url,
        }
    finally:
        for _match, path in verified:
            path.unlink(missing_ok=True)


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
    return SourceMedia(
        canonical_url=payload["canonical_url"],
        direct_media_url=payload.get("direct_media_url"),
        provider=payload["provider"],
        remote_id=payload["remote_id"],
        author=payload.get("author"),
        domain=payload.get("domain") or urlsplit(payload["canonical_url"]).netloc,
        tags=tuple(payload.get("tags", ())),
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


def _complete_review(connection, review_id, candidate_id, media_item_id, source, file_source=None):
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
    })
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
