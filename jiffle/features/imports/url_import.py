import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from jiffle.configuration.settings import Settings
from jiffle.features.imports.local_import import ImportFailure, atomic_copy, inspect_media
from jiffle.features.imports.history import create_import_history, update_import_history
from jiffle.features.imports.source_adapters.contracts import MediaDownloader, SourceProvider
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.infrastructure.media_revisions import create_original_revision


def create_url_import_job(connection: sqlite3.Connection, submitted_url: str) -> int:
    cursor = connection.execute(
        "INSERT INTO background_jobs (job_type, status) VALUES ('url_import', 'pending')"
    )
    job_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO url_import_candidates (job_id, submitted_url, status) "
        "VALUES (?, ?, 'pending')",
        (job_id, submitted_url),
    )
    create_import_history(connection, job_id, {"url": submitted_url})
    connection.commit()
    return job_id


def run_url_import_job(
    database_path: Path,
    settings: Settings,
    job_id: int,
    submitted_url: str,
    provider: SourceProvider,
    downloader: MediaDownloader,
) -> None:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    temporary: Path | None = None
    try:
        _running(connection, job_id)
        source = provider.fetch(submitted_url)
        blocked_source = connection.execute(
            "SELECT 1 FROM blocked_media_signatures WHERE source_url=?",
            (source.canonical_url,),
        ).fetchone()
        if blocked_source and settings.block_previously_deleted:
            raise ImportFailure(
                "import.previously_deleted",
                "This source was previously deleted and is blocked by settings.",
            )
        existing_source = connection.execute(
            "SELECT source.media_item_id FROM media_sources source "
            "JOIN media_items item ON item.id=source.media_item_id "
            "WHERE source.canonical_url=? AND item.deleted_at IS NULL",
            (source.canonical_url,),
        ).fetchone()
        if existing_source:
            _duplicate(connection, job_id, source, int(existing_source[0]))
            return

        settings.resolved_import_staging_path.mkdir(parents=True, exist_ok=True)
        temporary = settings.resolved_import_staging_path / (
            f"download-{uuid4().hex}{source.file_extension}"
        )
        downloader.download(source.direct_media_url, temporary, source.canonical_url)
        inspection = inspect_media(temporary)
        pending_review = connection.execute(
            "SELECT review.id FROM review_items review "
            "JOIN import_candidates candidate ON candidate.id=review.import_candidate_id "
            "WHERE review.status='pending' AND candidate.content_hash=? LIMIT 1",
            (inspection.content_hash,),
        ).fetchone()
        if pending_review:
            _duplicate_review(
                connection, job_id, source, int(pending_review[0])
            )
            return
        blocked_hash = connection.execute(
            "SELECT 1 FROM blocked_media_signatures WHERE content_hash=?",
            (inspection.content_hash,),
        ).fetchone()
        if blocked_source or blocked_hash:
            if settings.block_previously_deleted:
                raise ImportFailure(
                    "import.previously_deleted",
                    "This media was previously deleted and is blocked by settings.",
                )
            cursor = connection.execute(
                "INSERT INTO import_candidates "
                "(job_id,source_path,original_name,media_type,content_hash,width,height,"
                "file_size,status,stored_path,source_metadata_json) "
                "VALUES (?,?,?,?,?,?,?,?, 'review', ?, ?)",
                (job_id, submitted_url, Path(temporary).name, inspection.media_type,
                 inspection.content_hash, inspection.width, inspection.height,
                 inspection.file_size, temporary.name, _serialize_source(source)),
            )
            review = connection.execute(
                "INSERT INTO review_items (import_candidate_id, reason) VALUES (?, 'previously_deleted')",
                (int(cursor.lastrowid),),
            )
            connection.commit()
            _completed(connection, job_id, "review", None, int(review.lastrowid))
            temporary = None
            return
        existing_hash = connection.execute(
            "SELECT id FROM media_items WHERE content_hash = ?",
            (inspection.content_hash,),
        ).fetchone()
        if existing_hash:
            media_item_id = int(existing_hash[0])
            _attach_source(connection, media_item_id, source)
            _duplicate(connection, job_id, source, media_item_id)
            return

        stored_path = atomic_copy(temporary, settings.media_path, "media")
        try:
            cursor = connection.execute(
                "INSERT INTO media_items "
                "(file_path, media_type, source_url, author, domain, width, height, "
                "file_size, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stored_path, inspection.media_type, source.canonical_url,
                    source.author, source.domain, inspection.width, inspection.height,
                    inspection.file_size, inspection.content_hash,
                ),
            )
            media_item_id = int(cursor.lastrowid)
            create_original_revision(connection, media_item_id)
            _attach_source(connection, media_item_id, source)
            connection.executemany(
                "INSERT INTO media_tags (media_item_id, tag) VALUES (?, ?)",
                ((media_item_id, tag) for tag in sorted(set(source.tags))),
            )
            connection.execute(
                "UPDATE url_import_candidates SET canonical_url = ?, provider = ?, "
                "status = 'accepted', media_item_id = ? WHERE job_id = ?",
                (source.canonical_url, source.provider, media_item_id, job_id),
            )
            _completed(connection, job_id, "accepted", media_item_id)
        except Exception:
            (settings.media_path / stored_path).unlink(missing_ok=True)
            connection.rollback()
            raise
    except (SourceProviderFailure, ImportFailure) as error:
        _failed(connection, job_id, error.code, error.message)
    except Exception:
        _failed(
            connection, job_id, "import.url_import_failed",
            "The URL could not be imported.",
        )
        raise
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
        connection.close()


def _attach_source(connection, media_item_id, source) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO media_sources "
        "(media_item_id, canonical_url, direct_media_url, provider, remote_id, author, domain) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            media_item_id, source.canonical_url, source.direct_media_url,
            source.provider, source.remote_id, source.author, source.domain,
        ),
    )


def _serialize_source(source) -> str:
    return json.dumps({
        "canonical_url": source.canonical_url,
        "direct_media_url": source.direct_media_url,
        "provider": source.provider,
        "remote_id": source.remote_id,
        "author": source.author,
        "domain": source.domain,
        "tags": list(source.tags),
        "file_extension": source.file_extension,
    })


def _running(connection, job_id):
    connection.execute(
        "UPDATE background_jobs SET status='running', progress=10, "
        "started_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,)
    )
    connection.commit()


def _duplicate(connection, job_id, source, media_item_id):
    connection.execute(
        "UPDATE url_import_candidates SET canonical_url=?, provider=?, status='duplicate', "
        "media_item_id=? WHERE job_id=?",
        (source.canonical_url, source.provider, media_item_id, job_id),
    )
    _completed(connection, job_id, "duplicate", media_item_id)


def _duplicate_review(connection, job_id, source, review_item_id):
    connection.execute(
        "UPDATE url_import_candidates SET canonical_url=?, provider=?, status='duplicate' "
        "WHERE job_id=?",
        (source.canonical_url, source.provider, job_id),
    )
    _completed(connection, job_id, "duplicate", None, review_item_id)


def _completed(connection, job_id, outcome, media_item_id, review_item_id=None):
    result = json.dumps({
        "outcome": outcome,
        "media_item_id": media_item_id,
        "review_item_id": review_item_id,
    })
    connection.execute(
        "UPDATE background_jobs SET status='completed', progress=100, result_json=?, "
        "finished_at=CURRENT_TIMESTAMP WHERE id=?", (result, job_id)
    )
    update_import_history(connection, job_id, outcome, json.loads(result))
    connection.commit()


def _failed(connection, job_id, code, message):
    connection.rollback()
    connection.execute(
        "UPDATE background_jobs SET status='failed', error_code=?, error_message=?, "
        "finished_at=CURRENT_TIMESTAMP WHERE id=?", (code, message, job_id)
    )
    connection.execute(
        "UPDATE url_import_candidates SET status='failed' WHERE job_id=?", (job_id,)
    )
    update_import_history(connection, job_id, "failed", {"code": code, "message": message})
    connection.commit()
