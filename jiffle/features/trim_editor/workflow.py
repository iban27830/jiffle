"""Trim videos and animations into linked, standalone fragment media items.

The original file is never changed: every segment is re-encoded into its own
MP4 under ``media/revisions`` and registered as a new media item that points
back at the clip it was cut from. Sources that own live fragments are kept out
of tag-based collection assembly so only the fragments are picked automatically.
"""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from threading import Thread
from uuid import uuid4

from PIL import Image

from jiffle.infrastructure.database.connection import connect_database


# Images that may be animated. APNG (animated PNG) is intentionally out of
# scope for the first version: probing every PNG would be far too expensive.
ANIMATED_IMAGE_EXTENSIONS = frozenset({".gif", ".webp"})
MIN_SEGMENT_MS = 200
REVIEW_STATUSES = ("pending", "approved")


class TrimFailure(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def media_path(root, stored):
    root = Path(root).resolve()
    candidate = (root / stored).resolve()
    return candidate if candidate.is_relative_to(root) else None


def _suffix_of(stored) -> str:
    return Path(str(stored or "")).suffix.lower()


def is_animated_image(source: Path) -> bool:
    try:
        with Image.open(source) as image:
            return bool(getattr(image, "is_animated", False))
    except (OSError, ValueError):
        return False


def probe_duration_ms(source: Path) -> int | None:
    """Read a clip duration through ffmpeg without ffprobe being installed."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or source is None or not Path(source).is_file():
        return None
    try:
        probe = subprocess.run(
            [ffmpeg, "-i", str(source)], capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", probe.stderr or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return max(0, int(round(total * 1000)))


def mark_known_queue(connection, settings) -> int:
    """Flag already-classified videos/animations as pending without probing."""
    if not settings.trim_review_enabled:
        return 0
    cursor = connection.execute(
        "UPDATE media_items SET trim_review_status='pending' "
        "WHERE deleted_at IS NULL AND derived_from_media_id IS NULL "
        "AND trim_review_status IS NULL "
        "AND (media_type='video' OR (media_type='image' AND is_animated=1))"
    )
    connection.commit()
    return int(cursor.rowcount)


def reconcile_review_queue(connection, settings, on_progress=None) -> int:
    """Probe unclassified images once, then flag every eligible item pending.

    Returns the number of items flagged as pending in this pass. Cheap to call
    repeatedly: after the first run no image is left with an unknown animation
    state, so only newly imported media is inspected.
    """
    if not settings.trim_review_enabled:
        return 0
    # Non-animatable images never need a Pillow probe.
    connection.execute(
        "UPDATE media_items SET is_animated=0 "
        "WHERE media_type='image' AND is_animated IS NULL "
        "AND LOWER(file_path) NOT LIKE '%.gif' AND LOWER(file_path) NOT LIKE '%.webp'"
    )
    rows = connection.execute(
        "SELECT id, file_path FROM media_items "
        "WHERE media_type='image' AND is_animated IS NULL AND deleted_at IS NULL"
    ).fetchall()
    total = len(rows)
    for position, row in enumerate(rows, start=1):
        if _suffix_of(row["file_path"]) not in ANIMATED_IMAGE_EXTENSIONS:
            connection.execute("UPDATE media_items SET is_animated=0 WHERE id=?", (row["id"],))
        else:
            path = media_path(settings.media_path, row["file_path"])
            animated = 1 if path is not None and path.is_file() and is_animated_image(path) else 0
            connection.execute("UPDATE media_items SET is_animated=? WHERE id=?", (animated, row["id"]))
        if on_progress and (position % 25 == 0 or position == total):
            connection.commit()
            on_progress(position, total)
    connection.commit()
    return mark_known_queue(connection, settings)


def create_review_scan_job(connection, settings) -> int:
    cursor = connection.execute(
        "INSERT INTO background_jobs (job_type, status) VALUES ('trim_review_scan', 'pending')"
    )
    job_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO trim_scan_jobs (job_id, parameters_json) VALUES (?, '{}')", (job_id,)
    )
    connection.commit()
    return job_id


def start_review_scan(settings, job_id: int) -> None:
    args = (settings.database_path, settings, job_id)
    if settings.run_jobs_inline:
        run_review_scan(*args)
    else:
        Thread(target=run_review_scan, args=args, daemon=True).start()


def run_review_scan(database_path, settings, job_id: int) -> None:
    connection = connect_database(database_path)
    try:
        connection.execute(
            "UPDATE background_jobs SET status='running', "
            "started_at=COALESCE(started_at, CURRENT_TIMESTAMP) WHERE id=?",
            (job_id,),
        )
        connection.commit()
        def report(processed, total_probes):
            progress = 1 + int(90 * processed / max(total_probes, 1))
            connection.execute(
                "UPDATE background_jobs SET progress=?, status_message=? WHERE id=?",
                (progress, f"Checked {processed} of {total_probes} images", job_id),
            )
            connection.commit()

        candidates = reconcile_review_queue(connection, settings, on_progress=report)
        total = connection.execute(
            "SELECT COUNT(*) FROM media_items WHERE deleted_at IS NULL AND trim_review_status='pending'"
        ).fetchone()[0]
        connection.execute(
            "UPDATE trim_scan_jobs SET scanned_count=?, candidate_count=? WHERE job_id=?",
            (total, candidates, job_id),
        )
        connection.execute(
            "UPDATE background_jobs SET status='completed', progress=100, result_json=?, "
            "status_message=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (
                json.dumps({"candidates": candidates, "pending": total}),
                f"{candidates} waiting for review",
                job_id,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        connection.execute(
            "UPDATE background_jobs SET status='failed', error_code='trim.scan_failed', "
            "error_message='Review scan failed.', finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (job_id,),
        )
        connection.commit()
        raise
    finally:
        connection.close()


def resume_review_scans(database_path, settings) -> None:
    connection = connect_database(database_path)
    try:
        rows = connection.execute(
            "SELECT b.id FROM background_jobs b JOIN trim_scan_jobs t ON t.job_id=b.id "
            "WHERE b.status IN ('pending', 'running') AND t.cancel_requested=0 ORDER BY b.id"
        ).fetchall()
    finally:
        connection.close()
    if settings.trim_review_enabled:
        for row in rows:
            start_review_scan(settings, int(row["id"]))


def list_review_items(
    connection, settings, status: str = "pending",
    limit: int | None = None, offset: int = 0,
) -> list[dict]:
    if status == "pending":
        mark_known_queue(connection, settings)
    clauses = [
        "deleted_at IS NULL",
        "derived_from_media_id IS NULL",
        "trim_review_status IS NOT NULL",
    ]
    parameters: list[object] = []
    if status in REVIEW_STATUSES:
        clauses.append("trim_review_status=?")
        parameters.append(status)
    page = ""
    if limit is not None:
        page = " LIMIT ? OFFSET ?"
        parameters = [*parameters, max(0, int(limit)), max(0, int(offset))]
    rows = connection.execute(
        "SELECT id, media_type, width, height, file_size, created_at, is_animated, "
        "trim_review_status, trim_deferred_at FROM media_items "
        f"WHERE {' AND '.join(clauses)} "
        # New eligible clips have no deferral stamp and stay at the front.  A
        # deferred clip sinks below all of them; the oldest deferral comes first
        # so "Move to end" behaves like a queue rotation.
        "ORDER BY (trim_deferred_at IS NOT NULL) ASC, trim_deferred_at ASC, id DESC"
        + page,
        parameters,
    ).fetchall()
    items = []
    for row in rows:
        fragment_count = connection.execute(
            "SELECT COUNT(*) FROM media_items WHERE derived_from_media_id=? AND deleted_at IS NULL",
            (row["id"],),
        ).fetchone()[0]
        items.append({
            "media_id": int(row["id"]),
            "type": row["media_type"],
            "width": row["width"],
            "height": row["height"],
            "file_size": row["file_size"],
            "created_at": row["created_at"],
            "is_animated": bool(row["is_animated"]),
            "status": row["trim_review_status"],
            "deferred": row["trim_deferred_at"] is not None,
            "fragment_count": int(fragment_count),
            "content_url": f"/api/v1/media/{row['id']}/content",
            "thumbnail_url": f"/api/v1/media/{row['id']}/thumbnail",
        })
    return items


def count_review_items(connection, settings, status: str = "pending") -> int:
    """Number of clips in a trim review category, for pagination."""
    if status == "pending":
        mark_known_queue(connection, settings)
    clauses = [
        "deleted_at IS NULL",
        "derived_from_media_id IS NULL",
        "trim_review_status IS NOT NULL",
    ]
    parameters: list[object] = []
    if status in REVIEW_STATUSES:
        clauses.append("trim_review_status=?")
        parameters.append(status)
    return int(connection.execute(
        f"SELECT COUNT(*) FROM media_items WHERE {' AND '.join(clauses)}",
        parameters,
    ).fetchone()[0])


def defer_review_item(connection, media_id: int) -> None:
    """Move one pending clip to the end of the trim review queue.

    The clip stays pending and keeps every other property; only its ordering
    stamp changes, so it is offered again after the clips the user has not
    deferred yet.
    """
    cursor = connection.execute(
        "UPDATE media_items SET trim_deferred_at=CURRENT_TIMESTAMP "
        "WHERE id=? AND deleted_at IS NULL AND derived_from_media_id IS NULL "
        "AND trim_review_status='pending'",
        (media_id,),
    )
    if cursor.rowcount != 1:
        raise TrimFailure("trim.media_not_found", "Media item is not waiting for review.")
    connection.execute(
        "INSERT INTO operation_history (event_type, entity_type, entity_id, details_json) "
        "VALUES ('trim.deferred', 'media', ?, '{}')",
        (media_id,),
    )
    connection.commit()


def approve_review_item(connection, media_id: int) -> None:
    cursor = connection.execute(
        "UPDATE media_items SET trim_review_status='approved', trim_deferred_at=NULL "
        "WHERE id=? AND deleted_at IS NULL AND derived_from_media_id IS NULL",
        (media_id,),
    )
    if cursor.rowcount != 1:
        raise TrimFailure("trim.media_not_found", "Media item was not found.")
    connection.execute(
        "INSERT INTO operation_history (event_type, entity_type, entity_id, details_json) "
        "VALUES ('trim.approved', 'media', ?, '{}')",
        (media_id,),
    )
    connection.commit()


def _source_row(connection, media_id: int):
    row = connection.execute(
        "SELECT * FROM media_items WHERE id=? AND deleted_at IS NULL", (media_id,)
    ).fetchone()
    if row is None:
        raise TrimFailure("trim.media_not_found", "Media item was not found.")
    return row


def trim_state(connection, settings, media_id: int) -> dict:
    item = _source_row(connection, media_id)
    editor_id = int(item["derived_from_media_id"] or item["id"])
    editor = item if editor_id == int(item["id"]) else _source_row(connection, editor_id)
    editor_path = media_path(settings.media_path, editor["file_path"])
    duration_ms = probe_duration_ms(editor_path) if editor_path else None
    fragments = connection.execute(
        "SELECT id, trim_start_ms, trim_end_ms, trim_index, file_size, media_type "
        "FROM media_items WHERE derived_from_media_id=? AND deleted_at IS NULL "
        "ORDER BY trim_index, id",
        (editor_id,),
    ).fetchall()
    return {
        "media_id": int(item["id"]),
        "media_type": editor["media_type"],
        "is_animated": bool(editor["is_animated"]),
        "duration_ms": duration_ms,
        "derived_from_media_id": (
            int(item["derived_from_media_id"]) if item["derived_from_media_id"] else None
        ),
        "trim_index": item["trim_index"],
        "review_status": item["trim_review_status"],
        "source": {
            "id": editor_id,
            "media_type": editor["media_type"],
            "width": editor["width"],
            "height": editor["height"],
            "family_id": editor["family_id"],
            "content_url": f"/api/v1/media/{editor_id}/content",
            "thumbnail_url": f"/api/v1/media/{editor_id}/thumbnail",
        },
        "segments": [
            {
                "media_item_id": int(row["id"]),
                "index": int(row["trim_index"] or position),
                "start_ms": int(row["trim_start_ms"] or 0),
                "end_ms": int(row["trim_end_ms"] or 0),
                "file_size": row["file_size"],
                "content_url": f"/api/v1/media/{row['id']}/content",
                "thumbnail_url": f"/api/v1/media/{row['id']}/thumbnail",
            }
            for position, row in enumerate(fragments, start=1)
        ],
    }


def normalize_segments(segments, duration_ms: int | None = None) -> list[tuple[int, int]]:
    if not isinstance(segments, (list, tuple)) or not segments:
        raise TrimFailure("trim.no_segments", "At least one segment is required.")
    normalized: list[tuple[int, int]] = []
    for entry in segments:
        if isinstance(entry, dict):
            raw_start, raw_end = entry.get("start_ms"), entry.get("end_ms")
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            raw_start, raw_end = entry[0], entry[1]
        else:
            raise TrimFailure("trim.invalid_segment", "Each segment needs a start and an end.")
        try:
            start = int(round(float(raw_start)))
            end = int(round(float(raw_end)))
        except (TypeError, ValueError):
            raise TrimFailure("trim.invalid_segment", "Each segment needs numeric start and end.")
        if start < 0 or end <= start:
            raise TrimFailure("trim.invalid_segment", "The segment end must be after its start.")
        if end - start < MIN_SEGMENT_MS:
            raise TrimFailure(
                "trim.segment_too_short",
                f"Each segment must last at least {MIN_SEGMENT_MS} ms.",
            )
        normalized.append((start, end))
    normalized.sort(key=lambda value: value[0])
    for previous, current in zip(normalized, normalized[1:]):
        if current[0] < previous[1]:
            raise TrimFailure("trim.overlapping_segments", "Segments must not overlap.")
    if duration_ms:
        if any(end > duration_ms + 1000 for _, end in normalized):
            raise TrimFailure("trim.segment_out_of_range", "A segment is past the end of the clip.")
    return normalized


def _render_fragment(connection, settings, source_path, source_row, index, start_ms, end_ms,
                     exclude_media_id=None):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise TrimFailure(
            "trim.ffmpeg_unavailable",
            "FFmpeg is required to trim media. Install FFmpeg and add it to PATH.",
        )
    directory = settings.media_path / "revisions"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"media-{source_row['id']}-part-{index}-{uuid4().hex}.mp4"
    temporary = directory / f".{target.stem}-{uuid4().hex}.mp4"
    start = start_ms / 1000
    duration = (end_ms - start_ms) / 1000
    command = [
        ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(source_path), "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(temporary),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=900)
        os.replace(temporary, target)
    except (OSError, subprocess.SubprocessError) as error:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise TrimFailure("trim.encode_failed", "The segment could not be encoded.") from error
    digest = _hash_file(target)
    duplicate = connection.execute(
        "SELECT id FROM media_items WHERE content_hash=? AND id<>?",
        (digest, int(exclude_media_id or 0)),
    ).fetchone()
    if duplicate:
        digest = None
    width, height = _video_dimensions(target, source_row)
    return {
        "path": target,
        "relative": target.relative_to(settings.media_path).as_posix(),
        "digest": digest,
        "size": target.stat().st_size,
        "width": width,
        "height": height,
    }


def _video_dimensions(path, source_row) -> tuple[int, int]:
    width = int(source_row["width"] or 0)
    height = int(source_row["height"] or 0)
    try:
        import cv2

        capture = cv2.VideoCapture(str(path))
        try:
            detected_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            detected_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        finally:
            capture.release()
        if detected_width and detected_height:
            width, height = detected_width, detected_height
    except Exception:
        pass
    return max(2, width - width % 2), max(2, height - height % 2)


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_family(connection, source_row) -> int:
    family_id = source_row["family_id"]
    if family_id is not None:
        return int(family_id)
    family_id = int(
        connection.execute("INSERT INTO media_families DEFAULT VALUES").lastrowid
    )
    connection.execute(
        "UPDATE media_items SET family_id=? WHERE id=?", (family_id, source_row["id"])
    )
    return family_id


def _copy_tags(connection, source_id: int, fragment_id: int) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO media_tags (media_item_id, tag) "
        "SELECT ?, tag FROM media_tags WHERE media_item_id=?",
        (fragment_id, source_id),
    )


def apply_trim(connection, settings, media_id: int, segments, progress=None) -> dict:
    source = _source_row(connection, media_id)
    source_path = media_path(settings.media_path, source["file_path"])
    if source_path is None or not source_path.is_file():
        raise TrimFailure("trim.file_missing", "The media file is unavailable.")
    duration_ms = probe_duration_ms(source_path)
    normalized = normalize_segments(segments, duration_ms)
    total = len(normalized)

    def notify(index, status, fragment_id=None):
        if progress is None:
            return
        try:
            progress(index, total, status, fragment_id)
        except Exception:
            # Progress reporting must never break the encode itself.
            pass

    family_id = _ensure_family(connection, source)
    existing = connection.execute(
        "SELECT * FROM media_items WHERE derived_from_media_id=? AND deleted_at IS NULL "
        "ORDER BY trim_index, id",
        (media_id,),
    ).fetchall()
    created: list[int] = []
    updated: list[int] = []
    removed: list[int] = []
    rendered_paths: list[Path] = []
    try:
        for index, (start_ms, end_ms) in enumerate(normalized, start=1):
            if index <= len(existing):
                fragment = existing[index - 1]
                fragment_id = int(fragment["id"])
                fragment_path = media_path(settings.media_path, fragment["file_path"])
                if (
                    fragment["trim_start_ms"] == start_ms
                    and fragment["trim_end_ms"] == end_ms
                    and fragment_path is not None
                    and fragment_path.is_file()
                ):
                    notify(index, "saved", fragment_id)
                    continue
                notify(index, "encoding")
                rendered = _render_fragment(
                    connection, settings, source_path, source, index, start_ms, end_ms,
                    exclude_media_id=fragment_id,
                )
                rendered_paths.append(rendered["path"])
                details = json.dumps({
                    "source_media_id": media_id,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "index": index,
                    "source_revision_id": source["active_revision_id"],
                })
                cursor = connection.execute(
                    "INSERT INTO media_revisions "
                    "(media_item_id, parent_revision_id, file_path, operation, width, height, "
                    "file_size, content_hash, details_json) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        fragment_id, fragment["active_revision_id"], rendered["relative"],
                        "trim", rendered["width"], rendered["height"], rendered["size"],
                        rendered["digest"], details,
                    ),
                )
                revision_id = int(cursor.lastrowid)
                connection.execute(
                    "UPDATE media_items SET active_revision_id=?, file_path=?, width=?, "
                    "height=?, file_size=?, content_hash=?, trim_start_ms=?, trim_end_ms=?, "
                    "trim_index=?, family_id=? WHERE id=?",
                    (
                        revision_id, rendered["relative"], rendered["width"],
                        rendered["height"], rendered["size"], rendered["digest"],
                        start_ms, end_ms, index, family_id, fragment_id,
                    ),
                )
                connection.execute(
                    "DELETE FROM media_fingerprints WHERE media_item_id=?", (fragment_id,)
                )
                updated.append(fragment_id)
                connection.commit()
                # The file is now referenced by a committed revision; it must
                # survive a failure while a later segment is encoded.
                rendered_paths.clear()
                notify(index, "saved", fragment_id)
            else:
                notify(index, "encoding")
                rendered = _render_fragment(
                    connection, settings, source_path, source, index, start_ms, end_ms,
                )
                rendered_paths.append(rendered["path"])
                cursor = connection.execute(
                    "INSERT INTO media_items "
                    "(file_path, media_type, source_url, file_source_url, author, domain, "
                    "width, height, file_size, content_hash, parent_id, character_tags_json, "
                    "derived_from_media_id, trim_start_ms, trim_end_ms, trim_index, family_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        rendered["relative"], "video", source["source_url"],
                        source["file_source_url"], source["author"], source["domain"],
                        rendered["width"], rendered["height"], rendered["size"],
                        rendered["digest"], source["parent_id"],
                        source["character_tags_json"] or "[]", media_id, start_ms, end_ms,
                        index, family_id,
                    ),
                )
                fragment_id = int(cursor.lastrowid)
                from jiffle.infrastructure.media_revisions import create_original_revision

                create_original_revision(connection, fragment_id)
                _copy_tags(connection, media_id, fragment_id)
                created.append(fragment_id)
                connection.commit()
                rendered_paths.clear()
                notify(index, "saved", fragment_id)
        for fragment in existing[len(normalized):]:
            fragment_id = int(fragment["id"])
            connection.execute(
                "UPDATE media_items SET deleted_at=CURRENT_TIMESTAMP WHERE id=?", (fragment_id,)
            )
            connection.execute(
                "DELETE FROM media_fingerprints WHERE media_item_id=?", (fragment_id,)
            )
            removed.append(fragment_id)
        connection.execute(
            "UPDATE media_items SET trim_review_status='approved', trim_deferred_at=NULL "
            "WHERE id=?", (media_id,)
        )
        result = {
            "source_media_id": int(media_id),
            "family_id": family_id,
            "segment_count": len(normalized),
            "created": created,
            "updated": updated,
            "removed": removed,
        }
        connection.execute(
            "INSERT INTO operation_history (event_type, entity_type, entity_id, details_json) "
            "VALUES ('trim.applied', 'media', ?, ?)",
            (media_id, json.dumps(result)),
        )
        connection.commit()
        return result
    except TrimFailure:
        connection.rollback()
        for path in rendered_paths:
            path.unlink(missing_ok=True)
        raise
    except Exception:
        connection.rollback()
        for path in rendered_paths:
            path.unlink(missing_ok=True)
        raise


def run_trim_job(database_path, settings, job_id: int, media_id: int, segments) -> None:
    connection = connect_database(database_path)
    try:
        connection.execute(
            "UPDATE background_jobs SET status='running', "
            "started_at=COALESCE(started_at, CURRENT_TIMESTAMP) WHERE id=?",
            (job_id,),
        )
        connection.commit()
        try:
            normalized = normalize_segments(segments)
        except TrimFailure as error:
            _fail_job(connection, job_id, error.code, error.message)
            return
        states = [
            {"index": index, "start_ms": start, "end_ms": end, "status": "queued"}
            for index, (start, end) in enumerate(normalized, start=1)
        ]
        total = len(states)

        def publish(message=None):
            saved = sum(1 for state in states if state["status"] == "saved")
            connection.execute(
                "UPDATE background_jobs SET progress=?, result_json=?, status_message=? WHERE id=?",
                (
                    5 + int(90 * saved / max(1, total)),
                    json.dumps({"outcome": "running", "segments": states}),
                    message,
                    job_id,
                ),
            )
            connection.commit()

        def report(index, _count, status, fragment_id=None):
            if 1 <= index <= len(states):
                states[index - 1]["status"] = status
                if fragment_id is not None:
                    states[index - 1]["media_item_id"] = int(fragment_id)
            label = "Encoding fragment" if status == "encoding" else "Saved fragment"
            publish(f"{label} {index} of {total}")

        try:
            result = apply_trim(
                connection, settings, media_id, normalized, progress=report
            )
        except TrimFailure as error:
            connection.rollback()
            _fail_job(connection, job_id, error.code, error.message)
            return
        except Exception:
            connection.rollback()
            _fail_job(connection, job_id, "trim.failed", "Trimming failed.")
            raise
        connection.execute(
            "UPDATE background_jobs SET status='completed', progress=100, result_json=?, "
            "status_message=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (
                json.dumps({**result, "outcome": "completed", "segments": states}),
                f"{result['segment_count']} fragment(s) ready",
                job_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _fail_job(connection, job_id: int, code: str, message: str) -> None:
    connection.execute(
        "UPDATE background_jobs SET status='failed', error_code=?, error_message=?, "
        "finished_at=CURRENT_TIMESTAMP WHERE id=?",
        (code, message, job_id),
    )
    connection.commit()
