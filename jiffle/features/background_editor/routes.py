import base64
import json
from io import BytesIO
import os
from pathlib import Path
import sqlite3
from threading import Thread
from uuid import uuid4

from flask import Blueprint, current_app, jsonify, request, send_file
from PIL import Image, ImageOps

from jiffle.features.crop_editor.workflow import media_path
from jiffle.infrastructure.database.connection import get_database
from .runtime import remove_background
from .workflow import (
    BackgroundFailure, analyze_background_candidate, background_root,
    compose_background, compose_background_preview, create_background_preview,
    detector_parameters, detector_signature, preserve_value, preview_root,
    _apply_preserve,
)


background_blueprint = Blueprint("background_editor", __name__)
SUPPORTED_BACKGROUND_FORMATS = {"BMP", "JPEG", "PNG", "TIFF", "WEBP"}


@background_blueprint.get("/api/v1/background-assets")
def list_assets():
    category = request.args.get("category", "").strip()
    connection = get_database()
    if category:
        rows = connection.execute(
            "SELECT * FROM background_assets WHERE category=? ORDER BY id DESC", (category,)
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM background_assets ORDER BY category COLLATE NOCASE,id DESC"
        ).fetchall()
    return jsonify({"items": [_serialize_asset(row) for row in rows]})


@background_blueprint.get("/api/v1/background-assets/categories")
def list_asset_categories():
    return jsonify({"items": _asset_categories(get_database())})


@background_blueprint.post("/api/v1/background-assets/import")
def import_asset():
    upload = request.files.get("file")
    if upload is not None:
        original_name = Path(upload.filename or "background").name
        category = request.form.get("category", "").strip()
        source = upload.stream
        if not category:
            return _error("background.category_required", "Choose a category for the background.", 400)
    else:
        # Keep the previous local-path API working for existing local clients.
        payload = request.get_json(silent=True)
        payload = payload if isinstance(payload, dict) else {}
        legacy_path = Path(str(payload.get("path", ""))).expanduser()
        if not legacy_path.is_file():
            return _error("background.file_missing", "Background file was not found.", 404)
        original_name = legacy_path.name
        category = str(payload.get("category") or "General").strip()
        source = legacy_path
    try:
        category = _validated_category(category)
        with Image.open(source) as opened:
            if getattr(opened, "is_animated", False):
                raise BackgroundFailure(
                    "background.animated_unsupported", "Animated images cannot be used as backgrounds."
                )
            if (opened.format or "").upper() not in SUPPORTED_BACKGROUND_FORMATS:
                raise BackgroundFailure(
                    "background.unsupported_format",
                    "Use a PNG, JPEG, WebP, BMP, or TIFF background image.",
                )
            image = ImageOps.exif_transpose(opened)
            if image.width < 2 or image.height < 2:
                raise BackgroundFailure("background.invalid_dimensions", "Background image is too small.")
            image.load()
            normalized = image.convert("RGBA" if "A" in image.getbands() else "RGB")
    except BackgroundFailure as error:
        return _background_error(error)
    except (OSError, ValueError):
        return _error("background.decode_failed", "Background image could not be opened.", 422)
    root = background_root(current_app.config["JIFFLE_SETTINGS"])
    target = root / f"{uuid4().hex}.png"
    temporary = target.with_suffix(".tmp")
    try:
        normalized.save(temporary, format="PNG")
        os.replace(temporary, target)
        connection = get_database()
        cursor = connection.execute(
            "INSERT INTO background_assets "
            "(file_path,original_name,width,height,category) VALUES (?,?,?,?,?)",
            (target.name, original_name, normalized.width, normalized.height, category),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM background_assets WHERE id=?", (cursor.lastrowid,)
        ).fetchone()
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return jsonify({"id": row["id"], "item": _serialize_asset(row)}), 201


@background_blueprint.get("/api/v1/background-assets/<int:asset_id>/content")
def asset_content(asset_id):
    row = get_database().execute(
        "SELECT file_path FROM background_assets WHERE id=?", (asset_id,)
    ).fetchone()
    path = media_path(background_root(current_app.config["JIFFLE_SETTINGS"]), row["file_path"]) if row else None
    if path is None or not path.is_file():
        return _error("background.not_found", "Background was not found.", 404)
    return send_file(path, mimetype="image/png", conditional=True)


@background_blueprint.post("/api/v1/media/<int:media_id>/background-analysis")
def create_selected_analysis(media_id):
    try:
        parameters = _parameters_from_request()
        row = analyze_background_candidate(
            get_database(), current_app.config["JIFFLE_SETTINGS"], media_id, parameters
        )
    except BackgroundFailure as error:
        return _background_error(error)
    found = bool(row["candidate_found"])
    return jsonify({
        "status": "candidate" if found else "no_candidate",
        "candidate": _serialize_candidate(row) if found else None,
    }), 201


@background_blueprint.get("/api/v1/media/<int:media_id>/background-candidates")
def get_selected_candidate(media_id):
    try:
        parameters = detector_parameters(
            request.args.get("tolerance", 24), request.args.get("min_background_percent", 25)
        )
    except BackgroundFailure as error:
        return _background_error(error)
    row = get_database().execute(
        "SELECT r.* FROM background_candidate_results r JOIN media_items m "
        "ON m.active_revision_id=r.revision_id WHERE m.id=? AND r.parameter_signature=?",
        (media_id, detector_signature(parameters)),
    ).fetchone()
    found = bool(row and row["candidate_found"])
    return jsonify({
        "status": "candidate" if found else "not_analyzed" if row is None else "no_candidate",
        "candidate": _serialize_candidate(row) if found else None,
    })


@background_blueprint.get("/api/v1/background-candidates")
def list_candidates():
    try:
        parameters = detector_parameters(
            request.args.get("tolerance", 24), request.args.get("min_background_percent", 25)
        )
    except BackgroundFailure as error:
        return _background_error(error)
    rows = get_database().execute(
        "SELECT r.* FROM background_candidate_results r JOIN media_items m "
        "ON m.id=r.media_item_id AND m.active_revision_id=r.revision_id "
        "WHERE m.deleted_at IS NULL AND m.media_type='image' "
        "AND r.parameter_signature=? AND r.candidate_found=1 "
        "ORDER BY r.confidence DESC,r.background_area_percent DESC,r.media_item_id",
        (detector_signature(parameters),),
    ).fetchall()
    return jsonify({"items": [_serialize_candidate(row) for row in rows]})


@background_blueprint.post("/api/v1/background-scan-jobs")
def create_scan_job():
    try:
        parameters = _parameters_from_request()
    except BackgroundFailure as error:
        return _background_error(error)
    connection = get_database()
    cursor = connection.execute(
        "INSERT INTO background_jobs (job_type,status,result_json) "
        "VALUES ('background_scan','pending',?)", (json.dumps(parameters),)
    )
    job_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO background_scan_jobs (job_id,parameters_json) VALUES (?,?)",
        (job_id, json.dumps(parameters)),
    )
    connection.commit()
    settings = current_app.config["JIFFLE_SETTINGS"]
    arguments = (settings.database_path, settings, job_id, parameters)
    if settings.run_jobs_inline:
        _run_background_scan(*arguments)
    else:
        Thread(target=_run_background_scan, args=arguments, daemon=True).start()
    return jsonify({"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}), 202


@background_blueprint.get("/api/v1/background-scan-jobs/active")
def active_scan():
    connection = get_database()
    row = connection.execute(
        "SELECT b.id,b.status,b.progress,s.cancel_requested,s.scanned_count,s.candidate_count "
        "FROM background_jobs b JOIN background_scan_jobs s ON s.job_id=b.id "
        "WHERE b.status IN ('pending','running') ORDER BY b.id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return jsonify({"job": None})
    total = connection.execute(
        "SELECT COUNT(*) FROM media_items WHERE deleted_at IS NULL AND media_type='image'"
    ).fetchone()[0]
    return jsonify({"job": {
        "id": row["id"], "status": row["status"], "progress": row["progress"],
        "cancel_requested": bool(row["cancel_requested"]), "scanned": row["scanned_count"],
        "candidates": row["candidate_count"], "total": total,
        "status_url": f"/api/v1/jobs/{row['id']}",
    }})


@background_blueprint.post("/api/v1/background-scan-jobs/<int:job_id>/cancel")
def cancel_scan(job_id):
    connection = get_database()
    cursor = connection.execute(
        "UPDATE background_scan_jobs SET cancel_requested=1 WHERE job_id=?", (job_id,)
    )
    connection.commit()
    if cursor.rowcount != 1:
        return _error("background.scan_not_found", "Background scan was not found.", 404)
    return jsonify({"status": "cancelling"})


@background_blueprint.post("/api/v1/media/<int:media_id>/background-preview")
def create_preview(media_id):
    configured = current_app.config.get("JIFFLE_BACKGROUND_REMOVER")
    settings = current_app.config["JIFFLE_SETTINGS"]
    try:
        preserve = preserve_value((request.get_json(silent=True) or {}).get("preserve", 0))
    except BackgroundFailure as error:
        return _background_error(error)
    remover = configured or (
        lambda image, model_root: remove_background(
            image, model_root, settings.huggingface_token
        )
    )
    try:
        row = create_background_preview(
            get_database(), settings, media_id, remover,
        )
    except BackgroundFailure as error:
        return _background_error(error)
    return jsonify(_serialize_preview(row, preserve)), 201


@background_blueprint.get("/api/v1/media/<int:media_id>/background-preview")
def get_preview(media_id):
    connection = get_database()
    media = connection.execute(
        "SELECT active_revision_id FROM media_items WHERE id=? AND media_type='image' AND deleted_at IS NULL",
        (media_id,),
    ).fetchone()
    if media is None:
        return _error("background.media_not_found", "Media item was not found.", 404)
    row = connection.execute(
        "SELECT * FROM background_previews WHERE media_item_id=? AND source_revision_id=? ORDER BY created_at DESC LIMIT 1",
        (media_id, media["active_revision_id"]),
    ).fetchone()
    if row is None:
        return jsonify({"status": "none", "preview": None})
    path = media_path(preview_root(current_app.config["JIFFLE_SETTINGS"]), row["file_path"])
    if path is None or not path.is_file():
        return jsonify({"status": "none", "preview": None})
    try:
        preserve = preserve_value(request.args.get("preserve", 0))
    except BackgroundFailure as error:
        return _background_error(error)
    return jsonify(_serialize_preview(row, preserve))


@background_blueprint.get("/api/v1/background-previews/<preview_id>/content")
def preview_content(preview_id):
    row = get_database().execute(
        "SELECT file_path FROM background_previews "
        "WHERE id=?", (preview_id,)
    ).fetchone()
    path = media_path(preview_root(current_app.config["JIFFLE_SETTINGS"]), row["file_path"]) if row else None
    if path is None or not path.is_file():
        return _error("background.preview_missing", "Background preview was not found.", 404)
    try:
        preserve = preserve_value(request.args.get("preserve", 0))
    except BackgroundFailure as error:
        return _background_error(error)
    if preserve == 0:
        return send_file(path, mimetype="image/png", conditional=True)
    try:
        with Image.open(path) as opened:
            rendered = _apply_preserve(opened, preserve)
        output = BytesIO()
        rendered.save(output, format="PNG")
        output.seek(0)
        return send_file(output, mimetype="image/png", conditional=False)
    except (OSError, ValueError) as error:
        return _background_error(BackgroundFailure("background.preview_missing", "Background preview was not found."))


@background_blueprint.post("/api/v1/media/<int:media_id>/background-compose-preview")
def compose_preview(media_id):
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    try:
        preserve = preserve_value(payload.get("preserve", 0))
        image = compose_background_preview(
            get_database(), current_app.config["JIFFLE_SETTINGS"], media_id,
            payload.get("preview_id"), payload.get("background_id"),
            payload.get("blur", 0), preserve,
        )
    except BackgroundFailure as error:
        return _background_error(error)
    output = BytesIO()
    image.save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return jsonify({"status": "ready", "preview_id": uuid4().hex, "content_url": f"data:image/png;base64,{encoded}"})


@background_blueprint.post("/api/v1/media/<int:media_id>/background-compose")
def compose(media_id):
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    try:
        revision_id = compose_background(
            get_database(), current_app.config["JIFFLE_SETTINGS"], media_id,
            payload.get("preview_id"), payload.get("background_id"), payload.get("blur", 0),
            payload.get("preserve", 0),
        )
    except BackgroundFailure as error:
        return _background_error(error)
    return jsonify({
        "status": "completed", "revision_id": revision_id, "active": True,
        "content_url": f"/api/v1/media/{media_id}/revisions/{revision_id}/content",
    }), 201


def _run_background_scan(database_path, settings, job_id, parameters):
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    signature = detector_signature(parameters)
    try:
        total = connection.execute(
            "SELECT COUNT(*) FROM media_items WHERE deleted_at IS NULL AND media_type='image'"
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT m.id,m.active_revision_id FROM media_items m "
            "LEFT JOIN background_candidate_results r "
            "ON r.revision_id=m.active_revision_id AND r.parameter_signature=? "
            "WHERE m.deleted_at IS NULL AND m.media_type='image' "
            "AND r.revision_id IS NULL ORDER BY m.id", (signature,)
        ).fetchall()
        completed = total - len(rows)
        candidates = connection.execute(
            "SELECT COUNT(*) FROM media_items m JOIN background_candidate_results r "
            "ON r.revision_id=m.active_revision_id "
            "WHERE m.deleted_at IS NULL AND m.media_type='image' "
            "AND r.parameter_signature=? AND r.candidate_found=1", (signature,)
        ).fetchone()[0]
        connection.execute(
            "UPDATE background_jobs SET status='running',progress=?,"
            "started_at=COALESCE(started_at,CURRENT_TIMESTAMP) WHERE id=?",
            (int(99 * completed / max(total, 1)), job_id),
        )
        connection.execute(
            "UPDATE background_scan_jobs SET scanned_count=?,candidate_count=? WHERE job_id=?",
            (completed, candidates, job_id),
        )
        connection.commit()
        for index, media in enumerate(rows, 1):
            cancelled = connection.execute(
                "SELECT cancel_requested FROM background_scan_jobs WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            if cancelled:
                scanned = completed + index - 1
                result = json.dumps({"scanned": scanned, "candidates": candidates, "cancelled": True})
                connection.execute(
                    "UPDATE background_jobs SET status='completed',progress=100,result_json=?,"
                    "finished_at=CURRENT_TIMESTAMP WHERE id=?", (result, job_id)
                )
                connection.execute(
                    "UPDATE background_scan_jobs SET scanned_count=?,candidate_count=? WHERE job_id=?",
                    (scanned, candidates, job_id),
                )
                connection.commit()
                return
            try:
                candidate = analyze_background_candidate(connection, settings, int(media["id"]), parameters)
                candidates += int(candidate["candidate_found"])
            except BackgroundFailure as error:
                if error.code in {"background.animated_unsupported", "background.decode_failed"}:
                    connection.execute(
                        "INSERT OR REPLACE INTO background_candidate_results "
                        "(revision_id,media_item_id,parameter_signature,candidate_found) VALUES (?,?,?,0)",
                        (media["active_revision_id"], media["id"], signature),
                    )
            scanned = completed + index
            connection.execute(
                "UPDATE background_jobs SET progress=? WHERE id=?",
                (int(99 * scanned / max(total, 1)), job_id),
            )
            connection.execute(
                "UPDATE background_scan_jobs SET scanned_count=?,candidate_count=? WHERE job_id=?",
                (scanned, candidates, job_id),
            )
            connection.commit()
        result = json.dumps({"scanned": total, "candidates": candidates, "cancelled": False})
        connection.execute(
            "UPDATE background_jobs SET status='completed',progress=100,result_json=?,"
            "finished_at=CURRENT_TIMESTAMP WHERE id=?", (result, job_id)
        )
        connection.execute(
            "UPDATE background_scan_jobs SET scanned_count=?,candidate_count=? WHERE job_id=?",
            (total, candidates, job_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        connection.execute(
            "UPDATE background_jobs SET status='failed',error_code='background.scan_failed',"
            "error_message='Background candidate scan failed.',finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (job_id,),
        )
        connection.commit()
        raise
    finally:
        connection.close()


def resume_background_scans(database_path, settings):
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT b.id,s.parameters_json FROM background_jobs b "
            "JOIN background_scan_jobs s ON s.job_id=b.id "
            "WHERE b.status IN ('pending','running') AND s.cancel_requested=0 ORDER BY b.id"
        ).fetchall()
    finally:
        connection.close()
    for row in rows:
        Thread(
            target=_run_background_scan,
            args=(database_path, settings, int(row["id"]), json.loads(row["parameters_json"])),
            daemon=True,
        ).start()


def _parameters_from_request():
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    return detector_parameters(
        payload.get("tolerance", 24), payload.get("min_background_percent", 25)
    )


def _validated_category(value):
    category = " ".join(str(value or "").split())
    if not category:
        raise BackgroundFailure("background.category_required", "Choose a category for the background.")
    if len(category) > 80:
        raise BackgroundFailure("background.invalid_category", "Background category is too long.")
    return category


def _asset_categories(connection):
    rows = connection.execute(
        "SELECT category,COUNT(*) count FROM background_assets "
        "GROUP BY category ORDER BY category COLLATE NOCASE"
    ).fetchall()
    return [{"name": row["category"], "count": row["count"]} for row in rows]


def _serialize_asset(row):
    return {
        "id": row["id"], "original_name": row["original_name"],
        "width": row["width"], "height": row["height"], "category": row["category"],
        "created_at": row["created_at"],
        "content_url": f"/api/v1/background-assets/{row['id']}/content",
    }


def _serialize_candidate(row):
    return {
        "media_id": row["media_item_id"], "revision_id": row["revision_id"],
        "confidence": row["confidence"],
        "background_area_percent": row["background_area_percent"],
        "background_color": row["background_color"],
        "edge_consistency": row["edge_consistency"],
        "thumbnail_url": f"/api/v1/media/{row['media_item_id']}/thumbnail",
        "content_url": f"/api/v1/media/{row['media_item_id']}/content",
    }


def _serialize_preview(row, preserve=0):
    return {
        "status": "ready", "preview_id": row["id"],
        "source_revision_id": row["source_revision_id"], "width": row["width"],
        "height": row["height"], "subject_coverage": row["subject_coverage"],
        "preserve": preserve,
        "content_url": f"/api/v1/background-previews/{row['id']}/content?preserve={preserve}",
    }


def _background_error(error):
    if error.code in {
        "background.media_not_found", "background.file_missing", "background.not_found",
        "background.preview_missing",
    }:
        status = 404
    elif error.code in {"background.preview_required", "background.preview_stale"}:
        status = 409
    elif error.code in {
        "background.runtime_install_failed", "background.model_download_failed",
        "background.inference_failed", "background.huggingface_unavailable",
    }:
        status = 503
    elif error.code == "background.huggingface_token_invalid":
        status = 401
    elif error.code == "background.huggingface_access_denied":
        status = 403
    elif error.code in {
        "background.decode_failed", "background.animated_unsupported",
        "background.unsupported_media", "background.unsupported_format",
        "background.invalid_dimensions", "background.subject_not_isolated",
    }:
        status = 422
    elif error.code in {"background.invalid_preserve", "background.invalid_request", "background.invalid_blur", "background.invalid_parameters"}:
        status = 400
    else:
        status = 400
    return jsonify({
        "error": {"code": error.code, "message": error.message, "details": error.details}
    }), status


def _error(code, message, status):
    return jsonify({"error": {"code": code, "message": message, "details": {}}}), status
