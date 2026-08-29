import json
import os
from pathlib import Path
import sqlite3

from jiffle.configuration.settings import Settings
from jiffle.features.imports.local_import import atomic_copy
from jiffle.features.imports.source_adapters.contracts import SourceMedia, SourceProvider
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure


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
        "candidate.width, candidate.height, candidate.file_size "
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
        "(media_item_id, canonical_url, direct_media_url, provider, remote_id, author, domain) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(media_item_id) DO UPDATE SET "
        "canonical_url=excluded.canonical_url, direct_media_url=excluded.direct_media_url, "
        "provider=excluded.provider, remote_id=excluded.remote_id, "
        "author=excluded.author, domain=excluded.domain",
        (
            media_item_id, source.canonical_url, source.direct_media_url,
            source.provider, source.remote_id, source.author, source.domain,
        ),
    )
    connection.execute(
        "UPDATE media_items SET source_url=?, author=?, domain=? WHERE id=?",
        (source.canonical_url, source.author, source.domain, media_item_id),
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
