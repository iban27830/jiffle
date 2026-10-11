"""Revision-scoped crop admission checks; never changes or deletes media files."""

from hashlib import sha256
import json
import logging


BLOCKED_STATUSES = ("pending", "needs_crop", "failed")
# Bound automatic NAS work. Larger sources remain available for explicit review.
MAX_AUTO_PIXELS = 16_000_000


def parameters(settings):
    return {
        "algorithm_version": 1,
        "min_area": float(settings.crop_min_area_percent),
        "padding": float(settings.crop_padding_percent) / 100,
        "tolerance": int(settings.crop_background_tolerance),
    }


def signature(values):
    return sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def record_check(connection, revision_id, status, parameter_signature="", error_code=None, error_message=None):
    connection.execute(
        "INSERT INTO crop_review_checks (revision_id,status,parameter_signature,error_code,error_message) "
        "VALUES (?,?,?,?,?) ON CONFLICT(revision_id) DO UPDATE SET status=excluded.status, "
        "parameter_signature=excluded.parameter_signature,error_code=excluded.error_code, "
        "error_message=excluded.error_message,updated_at=CURRENT_TIMESTAMP",
        (revision_id, status, parameter_signature, error_code, error_message),
    )


def admission_sql(alias="item"):
    # No row means an old, unchecked image, deliberately grandfathered in.
    return (
        f"NOT EXISTS (SELECT 1 FROM crop_review_checks crop_check "
        f"WHERE crop_check.revision_id={alias}.active_revision_id "
        "AND crop_check.status IN ('pending','needs_crop','failed'))"
    )


def blocked_media(connection, media_ids):
    ids = list(dict.fromkeys(media_ids))
    if not ids:
        return []
    return [dict(row) for row in connection.execute(
        "SELECT m.id,c.status,c.error_code,c.error_message FROM media_items m "
        "JOIN crop_review_checks c ON c.revision_id=m.active_revision_id "
        "WHERE m.deleted_at IS NULL AND c.status IN ('pending','needs_crop','failed') "
        f"AND m.id IN ({','.join('?' for _ in ids)}) ORDER BY m.id", ids,
    )]


def media_review(connection, media_id, enabled=False):
    row = connection.execute(
        "SELECT c.* FROM crop_review_checks c JOIN media_items m "
        "ON m.active_revision_id=c.revision_id WHERE m.id=?", (media_id,),
    ).fetchone()
    result = dict(row) if row else {"status": "unchecked"}
    result["blocked"] = bool(enabled and result["status"] in BLOCKED_STATUSES)
    return result


def attention_count(connection):
    return int(connection.execute(
        "SELECT COUNT(*) FROM crop_review_checks c JOIN media_items m ON m.active_revision_id=c.revision_id "
        "WHERE m.deleted_at IS NULL AND c.status IN ('pending','needs_crop','failed')"
    ).fetchone()[0])


def attention_items(connection, limit=40, offset=0):
    rows = connection.execute(
        "SELECT m.id media_id,m.active_revision_id revision_id,m.width,m.height, "
        "c.status,c.error_message,a.id analysis_id,a.confidence,a.removed_area "
        "FROM crop_review_checks c JOIN media_items m ON m.active_revision_id=c.revision_id "
        "LEFT JOIN crop_analyses a ON a.id=(SELECT MAX(latest.id) FROM crop_analyses latest "
        "WHERE latest.revision_id=m.active_revision_id AND latest.method<>'manual' AND latest.status IN ('pending','deferred')) "
        "WHERE m.deleted_at IS NULL AND c.status IN ('pending','needs_crop','failed') "
        "ORDER BY m.id DESC LIMIT ? OFFSET ?", (limit, offset),
    ).fetchall()
    return [{**dict(row), "thumbnail_url": f"/api/v1/media/{row['media_id']}/thumbnail?revision={row['revision_id']}"}
            for row in rows]


def check_new_media(connection, settings, media_id):
    """Run inside the import transaction, before a new image becomes visible.

    Failure is an admission failure, not an import failure: keep the accepted
    file and provide a retry/manual decision. Callers own the transaction.
    """
    if settings is None or not settings.crop_review_enabled:
        return
    row = connection.execute(
        "SELECT active_revision_id,media_type,width,height,file_path FROM media_items WHERE id=?",
        (media_id,),
    ).fetchone()
    if row is None or row["media_type"] != "image":
        return
    from .workflow import CropFailure, analyze_image

    revision_id = row["active_revision_id"]
    record_check(connection, revision_id, "pending", signature(parameters(settings)))
    try:
        from PIL import Image
        from .workflow import media_path
        path = media_path(settings.media_path, row["file_path"])
        if path is not None and path.is_file():
            with Image.open(path) as source:
                animated = bool(getattr(source, "is_animated", False))
            connection.execute("UPDATE media_items SET is_animated=? WHERE id=?", (int(animated), media_id))
            if animated:
                record_check(connection, revision_id, "passed")
                return
        if (row["width"] or 0) * (row["height"] or 0) > MAX_AUTO_PIXELS:
            raise CropFailure(
                "crop.auto_size_limit",
                "This image exceeds the automatic local check limit (16 megapixels). "
                "Inspect it in Crop and confirm No crop needed, or analyze it manually.",
            )
        values = parameters(settings)
        analyze_image(connection, settings, media_id, min_area=values["min_area"],
                      padding=values["padding"], tolerance=values["tolerance"], commit=False)
    except CropFailure as error:
        if error.code == "crop.animated_unsupported":
            record_check(connection, revision_id, "passed")
        else:
            record_check(connection, revision_id, "failed", signature(parameters(settings)),
                         error.code, error.message)
            logging.getLogger(__name__).warning("Crop check for media %s: %s", media_id, error.code)
    except Exception:
        record_check(connection, revision_id, "failed", error_code="crop.check_failed",
                     error_message="Automatic crop check failed. Retry from Editor.")
        logging.getLogger(__name__).exception("Crop check failed for media %s", media_id)


def resume_pending_checks(database_path, settings):
    """Finish a previously committed pending check without enrolling old media."""
    from threading import Thread
    from jiffle.infrastructure.database.connection import connect_database

    def run():
        connection = connect_database(database_path)
        try:
            rows = connection.execute(
                "SELECT m.id FROM media_items m JOIN crop_review_checks c "
                "ON c.revision_id=m.active_revision_id WHERE c.status='pending' AND m.deleted_at IS NULL"
            ).fetchall()
            for row in rows:
                check_new_media(connection, settings, row[0])
                connection.commit()
        finally:
            connection.close()
    if settings.crop_review_enabled:
        Thread(target=run, daemon=True, name="crop-admission-recovery").start()
