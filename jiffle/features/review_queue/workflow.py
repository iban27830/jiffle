import json
import os
from pathlib import Path
import sqlite3

from jiffle.configuration.settings import Settings
from jiffle.features.imports.local_import import atomic_copy
from jiffle.features.imports.source_adapters.contracts import SourceMedia, SourceProvider
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.infrastructure.media_revisions import create_original_revision


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
) -> int:
    row = _pending_review(connection, review_id)
    source = source or _candidate_source(row["source_metadata_json"])
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
            _complete_review(
                connection, review_id, row["candidate_id"], media_item_id, source
            )
            staged.unlink(missing_ok=True)
            return media_item_id

    duplicate = connection.execute(
        "SELECT id FROM media_items WHERE content_hash = ?", (row["content_hash"],)
    ).fetchone()
    if duplicate:
        media_item_id = int(duplicate[0])
        if source:
            _store_source(connection, media_item_id, source)
            _store_tags(connection, media_item_id, source.tags)
        _complete_review(connection, review_id, row["candidate_id"], media_item_id, source)
        staged.unlink(missing_ok=True)
        return media_item_id

    stored_path = atomic_copy(staged, settings.media_path, "media")
    try:
        cursor = connection.execute(
            "INSERT INTO media_items "
            "(file_path, media_type, source_url, author, domain, width, height, "
            "file_size, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stored_path,
                row["media_type"],
                source.canonical_url if source else None,
                source.author if source else None,
                source.domain if source else None,
                row["width"],
                row["height"],
                row["file_size"],
                row["content_hash"],
            ),
        )
        media_item_id = int(cursor.lastrowid)
        create_original_revision(connection, media_item_id)
        if source:
            _store_source(connection, media_item_id, source)
            _store_tags(connection, media_item_id, source.tags)
        _complete_review(connection, review_id, row["candidate_id"], media_item_id, source)
    except Exception:
        (settings.media_path / stored_path).unlink(missing_ok=True)
        connection.rollback()
        raise
    staged.unlink(missing_ok=True)
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
        _history(connection, "review.rejected", review_id, {})
        connection.commit()
    except Exception:
        connection.rollback()
        if rejecting.is_file():
            os.replace(rejecting, staged)
        raise
    rejecting.unlink(missing_ok=True)


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
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute(
            "UPDATE background_jobs SET status='running', progress=20, "
            "started_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,)
        )
        connection.commit()
        source = provider.fetch(source_url)
        media_item_id = accept_review_item(connection, settings, review_id, source)
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
        direct_media_url=payload["direct_media_url"],
        provider=payload["provider"],
        remote_id=payload["remote_id"],
        author=payload.get("author"),
        domain=payload["domain"],
        tags=tuple(payload.get("tags", ())),
        file_extension=payload["file_extension"],
        character_tags=tuple(payload.get("character_tags", ())),
        parent_id=payload.get("parent_id"),
    )


def _staged_path(settings: Settings, stored_path: str) -> Path:
    root = settings.resolved_import_staging_path.resolve()
    candidate = (root / stored_path).resolve()
    if not candidate.is_relative_to(root):
        raise ReviewFailure("review.file_missing", "The staged file is unavailable.")
    return candidate


def _complete_review(connection, review_id, candidate_id, media_item_id, source):
    connection.execute(
        "UPDATE import_candidates SET status='accepted', media_item_id=? WHERE id=?",
        (media_item_id, candidate_id),
    )
    connection.execute(
        "UPDATE review_items SET status='accepted', resolved_at=CURRENT_TIMESTAMP WHERE id=?",
        (review_id,),
    )
    _history(connection, "review.accepted", review_id, {
        "media_item_id": media_item_id,
        "source_url": source.canonical_url if source else None,
    })
    connection.commit()


def _store_source(connection, media_item_id, source):
    connection.execute(
        "INSERT INTO media_sources "
        "(media_item_id, canonical_url, direct_media_url, provider, remote_id, author, domain, parent_id, character_tags_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(media_item_id) DO UPDATE SET "
        "canonical_url=excluded.canonical_url, direct_media_url=excluded.direct_media_url, "
        "provider=excluded.provider, remote_id=excluded.remote_id, "
        "author=excluded.author, domain=excluded.domain, parent_id=excluded.parent_id, "
        "character_tags_json=excluded.character_tags_json",
        (
            media_item_id, source.canonical_url, source.direct_media_url,
            source.provider, source.remote_id, source.author, source.domain,
            source.parent_id, json.dumps(list(source.character_tags)),
        ),
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
        "SELECT source.provider, source.canonical_url FROM media_sources source "
        "JOIN media_items item ON item.id=source.media_item_id "
        "WHERE item.id=? AND item.deleted_at IS NULL",
        (media_item_id,),
    ).fetchone()
    if row is None:
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
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute(
            "UPDATE background_jobs SET status='running', progress=20, started_at=CURRENT_TIMESTAMP WHERE id=?",
            (job_id,),
        )
        connection.commit()
        row = connection.execute(
            "SELECT canonical_url FROM media_sources WHERE media_item_id=?", (media_item_id,)
        ).fetchone()
        if row is None:
            raise ReviewFailure("metadata.source_missing", "This media has no source URL to refresh.")
        source = provider.fetch(row["canonical_url"])
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
