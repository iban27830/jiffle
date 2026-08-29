from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

from PIL import Image

from jiffle.configuration.settings import Settings
from jiffle.features.imports.domain import ImportOutcome, LocalImportCommand, LocalImportResult
from jiffle.features.imports.history import create_import_history, update_import_history
from jiffle.infrastructure.media_revisions import create_original_revision

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".webm"}


@dataclass(frozen=True)
class MediaInspection:
    media_type: str
    width: int
    height: int
    file_size: int
    content_hash: str


class ImportFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def create_local_import_job(
    connection: sqlite3.Connection, command: LocalImportCommand
) -> int:
    cursor = connection.execute(
        "INSERT INTO background_jobs (job_type, status) VALUES ('local_import', 'pending')"
    )
    job_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO import_candidates "
        "(job_id, source_path, original_name, status) VALUES (?, ?, ?, 'pending')",
        (job_id, str(command.source_path), command.source_path.name),
    )
    create_import_history(connection, job_id, {"name": command.source_path.name})
    connection.commit()
    return job_id


def run_local_import_job(
    database_path: Path,
    settings: Settings,
    job_id: int,
    command: LocalImportCommand,
) -> None:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        _mark_running(connection, job_id)
        result = _import_file(connection, settings, job_id, command)
        _mark_completed(connection, job_id, result)
    except ImportFailure as error:
        _mark_failed(connection, job_id, error.code, error.message)
    except Exception:
        _mark_failed(
            connection,
            job_id,
            "import.unexpected_error",
            "The local file could not be imported.",
        )
        raise
    finally:
        connection.close()


def _import_file(
    connection: sqlite3.Connection,
    settings: Settings,
    job_id: int,
    command: LocalImportCommand,
) -> LocalImportResult:
    source = command.source_path.resolve()
    if not source.is_file():
        raise ImportFailure("import.file_not_found", "The selected file does not exist.")
    inspection = inspect_media(source)
    candidate_id = _candidate_id(connection, job_id)
    pending_review = _pending_review_for_hash(connection, inspection.content_hash)
    if pending_review:
        connection.execute(
            "UPDATE import_candidates SET media_type=?, content_hash=?, width=?, height=?, "
            "file_size=?, status='duplicate' WHERE id=?",
            (inspection.media_type, inspection.content_hash, inspection.width,
             inspection.height, inspection.file_size, candidate_id),
        )
        connection.commit()
        return LocalImportResult(
            ImportOutcome.DUPLICATE, candidate_id,
            review_item_id=int(pending_review["review_item_id"]),
        )
    blocked = connection.execute(
        "SELECT 1 FROM blocked_media_signatures WHERE content_hash=?",
        (inspection.content_hash,),
    ).fetchone()
    if blocked:
        if settings.block_previously_deleted:
            raise ImportFailure(
                "import.previously_deleted",
                "This media was previously deleted and is blocked by settings.",
            )
        relative_path = atomic_copy(source, settings.resolved_import_staging_path, "candidate")
        connection.execute("UPDATE import_candidates SET media_type=?, content_hash=?, width=?, height=?, file_size=?, stored_path=?, status='review' WHERE id=?", (inspection.media_type, inspection.content_hash, inspection.width, inspection.height, inspection.file_size, relative_path, candidate_id))
        connection.execute("INSERT INTO review_items (import_candidate_id, reason) VALUES (?, 'previously_deleted')", (candidate_id,))
        connection.commit()
        return LocalImportResult(ImportOutcome.REVIEW, candidate_id, review_item_id=connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    connection.execute(
        "UPDATE import_candidates SET media_type = ?, content_hash = ?, width = ?, "
        "height = ?, file_size = ? WHERE id = ?",
        (
            inspection.media_type,
            inspection.content_hash,
            inspection.width,
            inspection.height,
            inspection.file_size,
            candidate_id,
        ),
    )
    duplicate = connection.execute(
        "SELECT id FROM media_items WHERE content_hash = ?",
        (inspection.content_hash,),
    ).fetchone()
    if duplicate:
        media_item_id = int(duplicate[0])
        connection.execute(
            "UPDATE import_candidates SET status = 'duplicate', media_item_id = ? WHERE id = ?",
            (media_item_id, candidate_id),
        )
        connection.commit()
        return LocalImportResult(ImportOutcome.DUPLICATE, candidate_id, media_item_id)

    if not command.accept_without_source:
        relative_path = atomic_copy(
            source, settings.resolved_import_staging_path, "candidate"
        )
        try:
            connection.execute(
                "UPDATE import_candidates SET status = 'review', stored_path = ? WHERE id = ?",
                (relative_path, candidate_id),
            )
            cursor = connection.execute(
                "INSERT INTO review_items (import_candidate_id, reason) VALUES (?, ?)",
                (candidate_id, "source_required"),
            )
            connection.commit()
        except Exception:
            (settings.resolved_import_staging_path / relative_path).unlink(missing_ok=True)
            connection.rollback()
            raise
        return LocalImportResult(
            ImportOutcome.REVIEW, candidate_id, review_item_id=int(cursor.lastrowid)
        )

    relative_path = atomic_copy(source, settings.media_path, "media")
    try:
        cursor = connection.execute(
            "INSERT INTO media_items "
            "(file_path, media_type, width, height, file_size, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                relative_path,
                inspection.media_type,
                inspection.width,
                inspection.height,
                inspection.file_size,
                inspection.content_hash,
            ),
        )
        media_item_id = int(cursor.lastrowid)
        create_original_revision(connection, media_item_id)
        connection.execute(
            "UPDATE import_candidates SET status = 'accepted', stored_path = ?, "
            "media_item_id = ? WHERE id = ?",
            (relative_path, media_item_id, candidate_id),
        )
        connection.commit()
    except sqlite3.IntegrityError:
        (settings.media_path / relative_path).unlink(missing_ok=True)
        connection.rollback()
        duplicate = connection.execute(
            "SELECT id FROM media_items WHERE content_hash = ?",
            (inspection.content_hash,),
        ).fetchone()
        if duplicate is None:
            raise
        media_item_id = int(duplicate[0])
        connection.execute(
            "UPDATE import_candidates SET status = 'duplicate', media_type = ?, "
            "content_hash = ?, width = ?, height = ?, file_size = ?, media_item_id = ? "
            "WHERE id = ?",
            (
                inspection.media_type,
                inspection.content_hash,
                inspection.width,
                inspection.height,
                inspection.file_size,
                media_item_id,
                candidate_id,
            ),
        )
        connection.commit()
        return LocalImportResult(ImportOutcome.DUPLICATE, candidate_id, media_item_id)
    except Exception:
        (settings.media_path / relative_path).unlink(missing_ok=True)
        connection.rollback()
        raise
    return LocalImportResult(ImportOutcome.ACCEPTED, candidate_id, media_item_id)


def inspect_media(source: Path) -> MediaInspection:
    extension = source.suffix.lower()
    if extension in IMAGE_EXTENSIONS:
        try:
            with Image.open(source) as image:
                image.verify()
            with Image.open(source) as image:
                width, height = image.size
        except (OSError, ValueError) as error:
            raise ImportFailure("import.invalid_media", "The image file is invalid.") from error
        media_type = "image"
    elif extension in VIDEO_EXTENSIONS:
        width, height = _video_dimensions(source)
        media_type = "video"
    else:
        raise ImportFailure(
            "import.unsupported_media_type", "The selected file type is not supported."
        )
    return MediaInspection(
        media_type=media_type,
        width=width,
        height=height,
        file_size=source.stat().st_size,
        content_hash=_sha256(source),
    )


def _video_dimensions(source: Path) -> tuple[int, int]:
    try:
        import cv2
    except ImportError as error:
        raise ImportFailure(
            "import.video_support_unavailable", "Video support is unavailable."
        ) from error
    capture = cv2.VideoCapture(str(source))
    try:
        if not capture.isOpened():
            raise ImportFailure("import.invalid_media", "The video file is invalid.")
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if width <= 0 or height <= 0:
            raise ImportFailure("import.invalid_media", "The video file is invalid.")
        return width, height
    finally:
        capture.release()


def _sha256(source: Path) -> str:
    digest = sha256()
    with source.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_copy(source: Path, destination_root: Path, prefix: str) -> str:
    destination_root.mkdir(parents=True, exist_ok=True)
    filename = f"{prefix}-{uuid4().hex}{source.suffix.lower()}"
    destination = destination_root / filename
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with source.open("rb") as source_file, temporary.open("xb") as target_file:
            shutil.copyfileobj(source_file, target_file, length=1024 * 1024)
            target_file.flush()
            os.fsync(target_file.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return filename


def _candidate_id(connection: sqlite3.Connection, job_id: int) -> int:
    row = connection.execute(
        "SELECT id FROM import_candidates WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("Import candidate is missing")
    return int(row[0])


def _pending_review_for_hash(connection: sqlite3.Connection, content_hash: str):
    return connection.execute(
        "SELECT review.id AS review_item_id FROM review_items review "
        "JOIN import_candidates candidate ON candidate.id=review.import_candidate_id "
        "WHERE review.status='pending' AND candidate.content_hash=? LIMIT 1",
        (content_hash,),
    ).fetchone()


def _mark_running(connection: sqlite3.Connection, job_id: int) -> None:
    connection.execute(
        "UPDATE background_jobs SET status = 'running', progress = 10, "
        "started_at = CURRENT_TIMESTAMP WHERE id = ?",
        (job_id,),
    )
    connection.commit()


def _mark_completed(
    connection: sqlite3.Connection, job_id: int, result: LocalImportResult
) -> None:
    payload = {
        "outcome": result.outcome.value,
        "candidate_id": result.candidate_id,
        "media_item_id": result.media_item_id,
        "review_item_id": result.review_item_id,
    }
    connection.execute(
        "UPDATE background_jobs SET status = 'completed', progress = 100, "
        "result_json = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
        (json.dumps(payload), job_id),
    )
    update_import_history(connection, job_id, result.outcome.value, payload)
    connection.commit()


def _mark_failed(
    connection: sqlite3.Connection, job_id: int, code: str, message: str
) -> None:
    connection.execute(
        "UPDATE background_jobs SET status = 'failed', error_code = ?, "
        "error_message = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
        (code, message, job_id),
    )
    connection.execute(
        "UPDATE import_candidates SET status = 'failed' WHERE job_id = ?", (job_id,)
    )
    update_import_history(connection, job_id, "failed", {"code": code, "message": message})
    connection.commit()
