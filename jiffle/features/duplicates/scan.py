import json
import math
from pathlib import Path
import sqlite3

import imagehash
import numpy
from PIL import Image

from jiffle.configuration.settings import Settings
from jiffle.infrastructure.database.connection import connect_database


# Confidence at which two images are treated as the same picture when a media
# item is created outside a full scan (for example by accepting a review
# source).  It matches the Duplicates page default so both surfaces agree.
DEFAULT_MATCH_THRESHOLD = 90.0


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
    """Compare live images by perceptual hash and record close pairs.

    The expensive part of a scan is decoding every image.  Fingerprints are
    therefore cached in ``media_fingerprints`` and reused between scans; the
    cache is invalidated by the editors whenever the stored file changes, so a
    cached hash always describes the file that is actually in the library.  A
    second scan of an unchanged library only reads hashes and compares them.

    The comparison itself is vectorised with numpy: every hash becomes one
    ``uint64`` and the Hamming distance to all of the remaining hashes is
    computed in a single operation, so thousands of images stay a matter of
    seconds instead of millions of Python-level pairs.
    """
    connection = connect_database(database_path)
    try:
        connection.execute(
            "UPDATE background_jobs SET status='running', progress=5, "
            "started_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,)
        )
        connection.commit()
        items = connection.execute(
            "SELECT media.id AS id, media.file_path AS file_path, "
            "fp.perceptual_hash AS perceptual_hash "
            "FROM media_items media "
            "LEFT JOIN media_fingerprints fp ON fp.media_item_id=media.id "
            "WHERE media.media_type='image' AND media.deleted_at IS NULL "
            "ORDER BY media.id"
        ).fetchall()
        fingerprints: list[tuple[int, int]] = []
        last_progress = 5
        for index, item in enumerate(items):
            fingerprint = _hash_to_int(item["perceptual_hash"])
            if fingerprint is None:
                fingerprint = _fingerprint_for(settings, item["file_path"])
                if fingerprint is not None:
                    connection.execute(
                        "INSERT INTO media_fingerprints (media_item_id, perceptual_hash) "
                        "VALUES (?, ?) ON CONFLICT(media_item_id) DO UPDATE SET "
                        "perceptual_hash=excluded.perceptual_hash, updated_at=CURRENT_TIMESTAMP",
                        (item["id"], "%016x" % fingerprint),
                    )
            if fingerprint is not None:
                fingerprints.append((int(item["id"]), fingerprint))
            progress = 5 + int(45 * (index + 1) / max(len(items), 1))
            if progress > last_progress:
                connection.execute(
                    "UPDATE background_jobs SET progress=? WHERE id=?",
                    (progress, job_id),
                )
                connection.commit()
                last_progress = progress
        found = _compare_fingerprints(connection, job_id, fingerprints, threshold)
        connection.commit()
        result = json.dumps({
            "matches_found": found,
            "items_scanned": len(fingerprints),
        })
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


def _compare_fingerprints(
    connection: sqlite3.Connection,
    job_id: int,
    fingerprints: list[tuple[int, int]],
    threshold: float,
) -> int:
    """Record every pair whose similarity reaches ``threshold``."""
    count = len(fingerprints)
    if count < 2:
        return 0
    identifiers = numpy.array([item[0] for item in fingerprints], dtype=numpy.int64)
    hashes = numpy.array([item[1] for item in fingerprints], dtype=numpy.uint64)
    bits = 64
    # confidence = 100 * (bits - distance) / bits  =>  distance <= bits * (100 - threshold) / 100
    max_distance = int(math.floor(bits * (100.0 - threshold) / 100.0))
    total_pairs = count * (count - 1) // 2
    compared_pairs = 0
    found = 0
    last_progress = 50
    for left_index in range(count):
        distances = _popcount(hashes[left_index + 1:] ^ hashes[left_index])
        close_offsets = numpy.nonzero(distances <= max_distance)[0]
        for offset in close_offsets:
            right_index = left_index + 1 + int(offset)
            distance = int(distances[offset])
            confidence = 100.0 * (bits - distance) / bits
            if confidence < threshold:
                continue
            left_id = int(identifiers[left_index])
            right_id = int(identifiers[right_index])
            if left_id > right_id:
                left_id, right_id = right_id, left_id
            connection.execute(
                "INSERT INTO duplicate_matches "
                "(left_media_id, right_media_id, match_method, confidence) "
                "VALUES (?, ?, 'perceptual', ?) "
                "ON CONFLICT(left_media_id, right_media_id, match_method) "
                "DO UPDATE SET confidence=excluded.confidence",
                (left_id, right_id, round(confidence, 2)),
            )
            found += 1
        compared_pairs += int(distances.size)
        progress = 50 + int(49 * compared_pairs / max(total_pairs, 1))
        if progress > last_progress:
            connection.execute(
                "UPDATE background_jobs SET progress=? WHERE id=?",
                (progress, job_id),
            )
            connection.commit()
            last_progress = progress
    return found


def _popcount(values):
    """Number of set bits in each ``uint64`` of a numpy array."""
    values = numpy.asarray(values, dtype=numpy.uint64)
    if hasattr(numpy, "bitwise_count"):
        return numpy.bitwise_count(values)
    counts = values - ((values >> numpy.uint64(1)) & numpy.uint64(0x5555555555555555))
    counts = (counts & numpy.uint64(0x3333333333333333)) + (
        (counts >> numpy.uint64(2)) & numpy.uint64(0x3333333333333333)
    )
    counts = (counts + (counts >> numpy.uint64(4))) & numpy.uint64(0x0F0F0F0F0F0F0F0F)
    return (counts * numpy.uint64(0x0101010101010101)) >> numpy.uint64(56)


def _hash_to_int(raw) -> int | None:
    if not raw:
        return None
    try:
        return int(str(raw), 16)
    except (TypeError, ValueError):
        return None


def find_similar_media(
    connection: sqlite3.Connection,
    perceptual_hash: str,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    exclude_media_id: int | None = None,
) -> list[tuple[int, float]]:
    """Return live images whose cached fingerprint is close to ``perceptual_hash``.

    Results are ordered best first as ``(media_id, confidence)``.  Only cached
    fingerprints are read, so this never decodes the library; items without a
    fingerprint are skipped until a scan or import fills them.
    """
    wanted = _hash_to_int(perceptual_hash)
    if wanted is None:
        return []
    rows = connection.execute(
        "SELECT media.id AS id, fp.perceptual_hash AS perceptual_hash "
        "FROM media_fingerprints fp "
        "JOIN media_items media ON media.id=fp.media_item_id "
        "WHERE media.media_type='image' AND media.deleted_at IS NULL"
    ).fetchall()
    bits = 64
    # confidence = 100 * (bits - distance) / bits  =>  distance <= bits * (100 - threshold) / 100
    max_distance = int(math.floor(bits * (100.0 - threshold) / 100.0))
    matches: list[tuple[int, float]] = []
    for row in rows:
        media_id = int(row["id"])
        if exclude_media_id is not None and media_id == int(exclude_media_id):
            continue
        candidate = _hash_to_int(row["perceptual_hash"])
        if candidate is None:
            continue
        distance = (wanted ^ candidate).bit_count()
        if distance > max_distance:
            continue
        confidence = 100.0 * (bits - distance) / bits
        if confidence < threshold:
            continue
        matches.append((media_id, round(confidence, 2)))
    matches.sort(key=lambda item: item[1], reverse=True)
    return matches


def register_pending_match(
    connection: sqlite3.Connection,
    left_media_id: int,
    right_media_id: int,
    confidence: float,
) -> None:
    """Record a pending perceptual duplicate pair for the Duplicates page."""
    left, right = sorted((int(left_media_id), int(right_media_id)))
    if left == right:
        return
    connection.execute(
        "INSERT INTO duplicate_matches "
        "(left_media_id, right_media_id, match_method, confidence) "
        "VALUES (?, ?, 'perceptual', ?) "
        "ON CONFLICT(left_media_id, right_media_id, match_method) "
        "DO UPDATE SET confidence=excluded.confidence",
        (left, right, round(float(confidence), 2)),
    )


def _fingerprint_for(settings: Settings, stored_path: str) -> int | None:
    """Return the pHash of a stored file, or ``None`` when it is unreadable."""
    path = _media_path(settings.media_path, stored_path)
    if path is None or not path.is_file():
        return None
    try:
        with Image.open(path) as image:
            return int(str(imagehash.phash(image)), 16)
    except (OSError, ValueError):
        return None


def _media_path(root_path: Path, stored_path: str) -> Path | None:
    root = root_path.resolve()
    candidate = (root / stored_path).resolve()
    return candidate if candidate.is_relative_to(root) else None
