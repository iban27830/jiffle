"""Backfill implicit source tags on media that was imported before the rule.

New imports apply :mod:`platform_tags` while they are resolved, so an e621 or
FurAffinity post (or a general booru whose post ``source`` points at one) is
tagged ``furry`` on the way in.  Media that was imported earlier keeps only the
tags the source listed, which is why older records can be missing the tag.

This job walks those media, asks their provider for the post again, and adds
``furry`` when the source turns out to be furry-only.  Re-reading is necessary
because the booru ``source`` links were never stored locally, and the job is
idempotent: media that already carry the tag are skipped.
"""

import json
import sqlite3
from pathlib import Path

from jiffle.features.imports.source_adapters.platform_tags import (
    FURRY_TAG,
    is_furry_only_source,
)
from jiffle.infrastructure.database.connection import connect_database

# Providers whose post metadata can be re-read for a furry-only ``source`` link.
_SOURCE_PROVIDERS = ("e621", "furaffinity", "rule34", "gelbooru", "danbooru", "tbib")


def create_implicit_tag_job(connection: sqlite3.Connection) -> int:
    cursor = connection.execute(
        "INSERT INTO background_jobs (job_type, status, result_json) "
        "VALUES ('implicit_tag_backfill', 'pending', '{}')"
    )
    connection.commit()
    return int(cursor.lastrowid)


def run_implicit_tag_job(database_path: Path, job_id: int, providers) -> None:
    connection = connect_database(database_path)
    try:
        connection.execute(
            "UPDATE background_jobs SET status='running', progress=5, "
            "started_at=CURRENT_TIMESTAMP WHERE id=?",
            (job_id,),
        )
        connection.commit()
        rows = _candidate_rows(connection)
        by_name = {
            str(getattr(provider, "provider_name", "")).lower(): provider
            for provider in providers
        }
        total = max(1, len(rows))
        tagged = 0
        unchanged = 0
        errors: list[dict[str, object]] = []
        for index, row in enumerate(rows, start=1):
            try:
                added = _tag_media(connection, row, by_name)
            except Exception as error:  # a single provider failure must not stop the sweep
                connection.rollback()
                errors.append({
                    "media_item_id": int(row["media_item_id"]),
                    "provider": row["provider"] or None,
                    "message": str(error)[:200],
                })
                added = False
            if added:
                tagged += 1
            else:
                unchanged += 1
            connection.execute(
                "UPDATE background_jobs SET progress=? WHERE id=?",
                (5 + int(90 * index / total), job_id),
            )
            connection.commit()
        summary = {
            "outcome": "completed", "checked": len(rows), "tagged": tagged,
            "unchanged": unchanged, "errors": errors,
        }
        connection.execute(
            "UPDATE background_jobs SET status='completed', progress=100, result_json=?, "
            "finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (json.dumps(summary), job_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        connection.execute(
            "UPDATE background_jobs SET status='failed', error_code=?, error_message=?, "
            "finished_at=CURRENT_TIMESTAMP WHERE id=?",
            ("implicit_tags.failed", "Implicit tags could not be refreshed.", job_id),
        )
        connection.commit()
        raise
    finally:
        connection.close()


def _candidate_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Media without ``furry`` that could gain it from a known source."""
    placeholders = ",".join("?" for _ in _SOURCE_PROVIDERS)
    return connection.execute(
        "SELECT item.id AS media_item_id, COALESCE(item.domain, '') AS item_domain, "
        "COALESCE(source.provider, '') AS provider, COALESCE(source.domain, '') AS source_domain, "
        "source.canonical_url AS canonical_url "
        "FROM media_items item LEFT JOIN media_sources source ON source.media_item_id=item.id "
        "WHERE item.deleted_at IS NULL AND NOT EXISTS ("
        "  SELECT 1 FROM media_tags tag WHERE tag.media_item_id=item.id AND LOWER(tag.tag)='furry') "
        "AND (LOWER(COALESCE(item.domain, '')) IN ('e621.net', 'e926.net', 'furaffinity.net') "
        f"  OR LOWER(COALESCE(source.provider, '')) IN ({placeholders}) "
        "  OR LOWER(COALESCE(source.domain, '')) IN ('e621.net', 'e926.net', 'furaffinity.net')) "
        "ORDER BY item.id",
        _SOURCE_PROVIDERS,
    ).fetchall()


def _tag_media(connection: sqlite3.Connection, row: sqlite3.Row, providers) -> bool:
    media_id = int(row["media_item_id"])
    if is_furry_only_source(row["provider"], row["source_domain"] or row["item_domain"]):
        # Already known from the stored source; no network round-trip needed.
        return _add_furry(connection, media_id)
    provider = providers.get(str(row["provider"] or "").lower())
    if provider is None or not row["canonical_url"]:
        return False
    source = provider.fetch_metadata(row["canonical_url"])
    if not (is_furry_only_source(source.provider, source.domain)
            or FURRY_TAG in {str(tag).strip().lower() for tag in source.tags}):
        return False
    return _add_furry(connection, media_id)


def _add_furry(connection: sqlite3.Connection, media_id: int) -> bool:
    cursor = connection.execute(
        "INSERT OR IGNORE INTO media_tags (media_item_id, tag) VALUES (?, ?)",
        (media_id, FURRY_TAG),
    )
    return bool(cursor.rowcount)
