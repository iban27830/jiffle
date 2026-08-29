import json
from pathlib import Path
import sqlite3

from jiffle.configuration.settings import Settings
from jiffle.features.ai_tagging.adapters import TaggingProviderFailure
from jiffle.features.ai_tagging.contracts import ImageTaggingProvider
from jiffle.features.ai_tagging.media_sampling import sample_media


def create_tagging_job(connection: sqlite3.Connection, media_item_id: int) -> int:
    row = connection.execute(
        "SELECT 1 FROM media_items WHERE id=? AND deleted_at IS NULL", (media_item_id,)
    ).fetchone()
    if row is None:
        raise LookupError("media not found")
    cursor = connection.execute(
        "INSERT INTO background_jobs (job_type, status) VALUES ('ai_tagging', 'pending')"
    )
    connection.commit()
    return int(cursor.lastrowid)


def run_tagging_job(
    database_path: Path, settings: Settings, job_id: int,
    media_item_id: int, provider: ImageTaggingProvider,
) -> None:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            "UPDATE background_jobs SET status='running', progress=10, "
            "started_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,)
        )
        connection.commit()
        media = connection.execute(
            "SELECT file_path, media_type FROM media_items "
            "WHERE id=? AND deleted_at IS NULL", (media_item_id,)
        ).fetchone()
        if media is None:
            raise ValueError("Media item is missing")
        path = _media_path(settings.media_path, media["file_path"])
        if path is None or not path.is_file():
            raise ValueError("Media file is missing")
        samples = sample_media(path, media["media_type"])
        result = provider.suggest_tags(samples)
        existing_vocabulary = {
            row[0] for row in connection.execute("SELECT DISTINCT tag FROM media_tags")
        }
        preferred = {
            row[0] for row in connection.execute(
                "SELECT tag FROM tag_rules WHERE disposition='preferred'"
            )
        }
        blocked = {
            row[0] for row in connection.execute(
                "SELECT tag FROM tag_rules WHERE disposition='blocked'"
            )
        }
        aliases = {
            row["alias"]: row["canonical_tag"]
            for row in connection.execute("SELECT canonical_tag, alias FROM tag_aliases")
        }
        normalized_tags = {
            aliases.get(tag, tag) for tag in result.tags if aliases.get(tag, tag) not in blocked
        }
        known_vocabulary = existing_vocabulary | preferred
        known = sorted(tag for tag in normalized_tags if tag in known_vocabulary)
        unknown = sorted(tag for tag in normalized_tags if tag not in known_vocabulary)
        cursor = connection.execute(
            "INSERT INTO tag_suggestions "
            "(job_id, media_item_id, provider, known_tags_json, unknown_tags_json, diagnostic_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                job_id, media_item_id, provider.provider_name,
                json.dumps(known), json.dumps(unknown), json.dumps(result.diagnostic),
            ),
        )
        suggestion_id = int(cursor.lastrowid)
        payload = json.dumps({"suggestion_id": suggestion_id, "media_item_id": media_item_id})
        connection.execute(
            "UPDATE background_jobs SET status='completed', progress=100, result_json=?, "
            "finished_at=CURRENT_TIMESTAMP WHERE id=?", (payload, job_id)
        )
        connection.commit()
    except TaggingProviderFailure as error:
        _fail(connection, job_id, error.code, error.message)
    except Exception:
        _fail(connection, job_id, "ai.tagging_failed", "Media tagging failed.")
        raise
    finally:
        connection.close()


def resolve_suggestion(
    connection: sqlite3.Connection, suggestion_id: int,
    accepted: bool, selected_tags: list[str] | None = None,
) -> None:
    row = connection.execute(
        "SELECT * FROM tag_suggestions WHERE id=?", (suggestion_id,)
    ).fetchone()
    if row is None:
        raise LookupError("not found")
    if row["status"] != "pending":
        raise RuntimeError("already resolved")
    proposed = set(json.loads(row["known_tags_json"]) + json.loads(row["unknown_tags_json"]))
    chosen = proposed if selected_tags is None else set(selected_tags)
    if not chosen.issubset(proposed):
        raise ValueError("unrecognized selected tag")
    if accepted:
        connection.executemany(
            "INSERT OR IGNORE INTO media_tags (media_item_id, tag) VALUES (?, ?)",
            ((row["media_item_id"], tag) for tag in sorted(chosen)),
        )
    status = "accepted" if accepted else "rejected"
    connection.execute(
        "UPDATE tag_suggestions SET status=?, resolved_at=CURRENT_TIMESTAMP WHERE id=?",
        (status, suggestion_id),
    )
    connection.execute(
        "INSERT INTO operation_history "
        "(event_type, entity_type, entity_id, details_json) "
        "VALUES (?, 'tag_suggestion', ?, ?)",
        (f"ai_tags.{status}", suggestion_id, json.dumps({"tags": sorted(chosen)})),
    )
    connection.commit()


def _media_path(root_path, stored_path):
    root = root_path.resolve()
    candidate = (root / stored_path).resolve()
    return candidate if candidate.is_relative_to(root) else None


def _fail(connection, job_id, code, message):
    connection.rollback()
    connection.execute(
        "UPDATE background_jobs SET status='failed', error_code=?, error_message=?, "
        "finished_at=CURRENT_TIMESTAMP WHERE id=?", (code, message, job_id)
    )
    connection.commit()
