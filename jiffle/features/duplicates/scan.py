import json
from pathlib import Path
import sqlite3

import imagehash
from PIL import Image

from jiffle.configuration.settings import Settings


def create_duplicate_scan_job(connection: sqlite3.Connection, threshold: float) -> int:
    cursor = connection.execute(
        "INSERT INTO background_jobs (job_type, status, result_json) "
        "VALUES ('duplicate_scan', 'pending', ?)",
        (json.dumps({"threshold": threshold}),),
    )
    connection.commit()
    return int(cursor.lastrowid)


def run_duplicate_scan_job(
    database_path: Path, settings: Settings, job_id: int, threshold: float
) -> None:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute(
            "UPDATE background_jobs SET status='running', progress=5, "
            "started_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,)
        )
        connection.commit()
        items = connection.execute(
            "SELECT id, file_path FROM media_items "
            "WHERE media_type='image' AND deleted_at IS NULL ORDER BY id"
        ).fetchall()
        fingerprints = []
        last_progress = 5
        for index, item in enumerate(items):
            path = _media_path(settings.media_path, item["file_path"])
            if path is not None and path.is_file():
                try:
                    with Image.open(path) as image:
                        fingerprint = str(imagehash.phash(image))
                except (OSError, ValueError):
                    pass
                else:
                    connection.execute(
                        "INSERT INTO media_fingerprints (media_item_id, perceptual_hash) "
                        "VALUES (?, ?) ON CONFLICT(media_item_id) DO UPDATE SET "
                        "perceptual_hash=excluded.perceptual_hash, updated_at=CURRENT_TIMESTAMP",
                        (item["id"], fingerprint),
                    )
                    fingerprints.append(
                        (int(item["id"]), imagehash.hex_to_hash(fingerprint))
                    )
            progress = 5 + int(45 * (index + 1) / max(len(items), 1))
            if progress > last_progress:
                connection.execute(
                    "UPDATE background_jobs SET progress=? WHERE id=?",
                    (progress, job_id),
                )
                connection.commit()
                last_progress = progress
        found = 0
        total_pairs = len(fingerprints) * (len(fingerprints) - 1) // 2
        compared_pairs = 0
        last_progress = 50
        for left_index, (left_id, left_hash) in enumerate(fingerprints):
            for right_id, right_hash in fingerprints[left_index + 1:]:
                bit_count = left_hash.hash.size
                confidence = 100.0 * (bit_count - (left_hash - right_hash)) / bit_count
                if confidence >= threshold:
                    connection.execute(
                        "INSERT INTO duplicate_matches "
                        "(left_media_id, right_media_id, match_method, confidence) "
                        "VALUES (?, ?, 'perceptual', ?) "
                        "ON CONFLICT(left_media_id, right_media_id, match_method) "
                        "DO UPDATE SET confidence=excluded.confidence",
                        (left_id, right_id, round(confidence, 2)),
                    )
                    found += 1
                compared_pairs += 1
                progress = 50 + int(49 * compared_pairs / max(total_pairs, 1))
                if progress > last_progress:
                    connection.execute(
                        "UPDATE background_jobs SET progress=? WHERE id=?",
                        (progress, job_id),
                    )
                    connection.commit()
                    last_progress = progress
        result = json.dumps({"matches_found": found, "items_scanned": len(fingerprints)})
        connection.execute(
            "UPDATE background_jobs SET status='completed', progress=100, result_json=?, "
            "finished_at=CURRENT_TIMESTAMP WHERE id=?", (result, job_id)
        )
        connection.commit()
    except Exception:
        connection.rollback()
        connection.execute(
            "UPDATE background_jobs SET status='failed', "
            "error_code='duplicates.scan_failed', "
            "error_message='Duplicate scanning failed.', "
            "finished_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,)
        )
        connection.commit()
        raise
    finally:
        connection.close()


def _media_path(root_path: Path, stored_path: str) -> Path | None:
    root = root_path.resolve()
    candidate = (root / stored_path).resolve()
    return candidate if candidate.is_relative_to(root) else None
