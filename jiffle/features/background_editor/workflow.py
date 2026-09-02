from collections import Counter, deque
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageFilter, ImageOps

from jiffle.features.crop_editor.workflow import media_path


DETECTOR_VERSION = 1
DEFAULT_TOLERANCE = 24
DEFAULT_MIN_BACKGROUND_PERCENT = 25.0
MODEL_NAME = "briaai/RMBG-2.0"
MIN_PRESERVE = 0
MAX_PRESERVE = 100
DEFAULT_PRESERVE = 0


class BackgroundFailure(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def preserve_value(value=DEFAULT_PRESERVE):
    try:
        value = int(value)
    except (TypeError, ValueError) as error:
        raise BackgroundFailure(
            "background.invalid_preserve",
            "Detail preservation must be between 0 and 100.",
        ) from error
    if not MIN_PRESERVE <= value <= MAX_PRESERVE:
        raise BackgroundFailure(
            "background.invalid_preserve",
            "Detail preservation must be between 0 and 100.",
        )
    return value


def _apply_preserve(image, preserve):
    preserve = preserve_value(preserve)
    if preserve == DEFAULT_PRESERVE:
        return image.convert("RGBA")
    gamma = 1.0 - (preserve * 0.006)
    alpha = image.convert("RGBA").getchannel("A")
    alpha = alpha.point(
        lambda level: max(0, min(255, round(((level / 255.0) ** gamma) * 255.0)))
    )
    result = image.convert("RGBA")
    result.putalpha(alpha)
    return result


def detector_parameters(tolerance=DEFAULT_TOLERANCE, min_background_percent=DEFAULT_MIN_BACKGROUND_PERCENT):
    try:
        tolerance = int(tolerance)
        min_background_percent = float(min_background_percent)
    except (TypeError, ValueError) as error:
        raise BackgroundFailure(
            "background.invalid_parameters",
            "Detector parameters must be numeric.",
        ) from error
    if not 5 <= tolerance <= 80:
        raise BackgroundFailure(
            "background.invalid_parameters", "Tolerance must be between 5 and 80."
        )
    if not 5 <= min_background_percent <= 95:
        raise BackgroundFailure(
            "background.invalid_parameters",
            "Minimum background area must be between 5 and 95 percent.",
        )
    return {
        "tolerance": tolerance,
        "min_background_percent": min_background_percent,
        "algorithm_version": DETECTOR_VERSION,
    }


def detector_signature(parameters):
    canonical = {
        "algorithm_version": int(parameters.get("algorithm_version", DETECTOR_VERSION)),
        "min_background_percent": float(parameters["min_background_percent"]),
        "tolerance": int(parameters["tolerance"]),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def analyze_background_candidate(connection, settings, media_id, parameters=None):
    parameters = parameters or detector_parameters()
    signature = detector_signature(parameters)
    row = connection.execute(
        "SELECT m.id,m.media_type,m.active_revision_id,r.file_path "
        "FROM media_items m JOIN media_revisions r ON r.id=m.active_revision_id "
        "WHERE m.id=? AND m.deleted_at IS NULL",
        (media_id,),
    ).fetchone()
    if row is None:
        raise BackgroundFailure("background.media_not_found", "Media item was not found.")
    if row["media_type"] != "image":
        raise BackgroundFailure(
            "background.unsupported_media", "Only static images can be analyzed."
        )
    cached = connection.execute(
        "SELECT * FROM background_candidate_results "
        "WHERE revision_id=? AND parameter_signature=?",
        (row["active_revision_id"], signature),
    ).fetchone()
    if cached is not None:
        return cached
    source = media_path(settings.media_path, row["file_path"])
    if source is None or not source.is_file():
        raise BackgroundFailure("background.file_missing", "Media file is unavailable.")
    try:
        with Image.open(source) as opened:
            if getattr(opened, "is_animated", False):
                raise BackgroundFailure(
                    "background.animated_unsupported", "Animated images are not analyzed."
                )
            image = ImageOps.exif_transpose(opened).convert("RGBA")
            image.thumbnail((512, 512), Image.Resampling.LANCZOS)
            result = _detect_background(
                image,
                parameters["tolerance"],
                parameters["min_background_percent"],
            )
    except BackgroundFailure:
        raise
    except (OSError, ValueError) as error:
        raise BackgroundFailure(
            "background.decode_failed", "Image could not be analyzed."
        ) from error
    connection.execute(
        "INSERT INTO background_candidate_results "
        "(revision_id,media_item_id,parameter_signature,candidate_found,confidence,"
        "background_area_percent,background_color,edge_consistency) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(revision_id,parameter_signature) DO UPDATE SET "
        "candidate_found=excluded.candidate_found,confidence=excluded.confidence,"
        "background_area_percent=excluded.background_area_percent,"
        "background_color=excluded.background_color,edge_consistency=excluded.edge_consistency,"
        "analyzed_at=CURRENT_TIMESTAMP",
        (
            row["active_revision_id"],
            media_id,
            signature,
            int(result["candidate_found"]),
            result["confidence"],
            result["background_area_percent"],
            result["background_color"],
            result["edge_consistency"],
        ),
    )
    connection.commit()
    return connection.execute(
        "SELECT * FROM background_candidate_results "
        "WHERE revision_id=? AND parameter_signature=?",
        (row["active_revision_id"], signature),
    ).fetchone()


def _detect_background(image, tolerance, min_background_percent):
    width, height = image.size
    pixels = image.load()
    edge_coordinates = list(_edge_coordinates(width, height))
    edge_pixels = [pixels[x, y] for x, y in edge_coordinates]
    buckets = Counter(_color_bucket(pixel) for pixel in edge_pixels)
    dominant, _ = buckets.most_common(1)[0]
    members = [pixel for pixel in edge_pixels if _color_bucket(pixel) == dominant]
    transparent = dominant == ("transparent",)
    reference = None if transparent else tuple(
        round(sum(pixel[channel] for pixel in members) / len(members)) for channel in range(3)
    )

    def is_background(pixel):
        if transparent:
            return pixel[3] < 24
        return pixel[3] >= 24 and max(
            abs(pixel[channel] - reference[channel]) for channel in range(3)
        ) <= tolerance

    edge_consistency = sum(is_background(pixel) for pixel in edge_pixels) / len(edge_pixels)
    seen = bytearray(width * height)
    queue = deque()
    background_pixels = 0
    for x, y in edge_coordinates:
        index = y * width + x
        if seen[index]:
            continue
        seen[index] = 1
        if is_background(pixels[x, y]):
            queue.append((x, y))
            background_pixels += 1
    while queue:
        x, y = queue.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if nx < 0 or ny < 0 or nx >= width or ny >= height:
                continue
            index = ny * width + nx
            if seen[index]:
                continue
            seen[index] = 1
            if is_background(pixels[nx, ny]):
                queue.append((nx, ny))
                background_pixels += 1
    background_area = 100.0 * background_pixels / max(width * height, 1)
    candidate = (
        edge_consistency >= 0.60
        and min_background_percent <= background_area <= 98.5
    )
    area_strength = min(1.0, background_area / 65.0)
    confidence = 100.0 * (0.60 * edge_consistency + 0.40 * area_strength)
    return {
        "candidate_found": candidate,
        "confidence": round(min(99.0, confidence), 2),
        "background_area_percent": round(background_area, 2),
        "background_color": None if reference is None else "#%02x%02x%02x" % reference,
        "edge_consistency": round(100.0 * edge_consistency, 2),
    }


def _edge_coordinates(width, height):
    for x in range(width):
        yield x, 0
        if height > 1:
            yield x, height - 1
    for y in range(1, max(1, height - 1)):
        yield 0, y
        if width > 1:
            yield width - 1, y


def _color_bucket(pixel):
    if pixel[3] < 24:
        return ("transparent",)
    return tuple(channel // 32 for channel in pixel[:3])


def create_background_preview(connection, settings, media_id, remover, force=False):
    if isinstance(force, str):
        force = force.strip().lower() in {"1", "true", "yes", "on"}
    row = connection.execute(
        "SELECT m.active_revision_id,r.file_path FROM media_items m "
        "JOIN media_revisions r ON r.id=m.active_revision_id "
        "WHERE m.id=? AND m.media_type='image' AND m.deleted_at IS NULL",
        (media_id,),
    ).fetchone()
    if row is None:
        raise BackgroundFailure("background.media_not_found", "Media item was not found.")
    if not force:
        existing = connection.execute(
            "SELECT * FROM background_previews WHERE media_item_id=? AND source_revision_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (media_id, row["active_revision_id"]),
        ).fetchone()
        if existing is not None:
            preview_path = media_path(preview_root(settings), existing["file_path"])
            if preview_path is not None and preview_path.is_file():
                return existing
    source = media_path(settings.media_path, row["file_path"])
    if source is None or not source.is_file():
        raise BackgroundFailure("background.file_missing", "Media file is unavailable.")
    try:
        with Image.open(source) as opened:
            if getattr(opened, "is_animated", False):
                raise BackgroundFailure(
                    "background.animated_unsupported", "Animated images are not supported."
                )
            original = ImageOps.exif_transpose(opened).convert("RGBA")
            try:
                isolated = remover(original, settings.database_path.parent / "models")
            except BackgroundFailure:
                raise
            except Exception as error:
                raise BackgroundFailure(
                    "background.inference_failed",
                    "The background model could not process the image.",
                ) from error
            if not isinstance(isolated, Image.Image):
                raise BackgroundFailure(
                    "background.inference_failed", "The background model returned no image."
                )
            isolated = isolated.convert("RGBA")
            if isolated.size != original.size:
                isolated = isolated.resize(original.size, Image.Resampling.LANCZOS)
    except BackgroundFailure:
        raise
    except (OSError, ValueError) as error:
        raise BackgroundFailure(
            "background.decode_failed", "Image could not be processed."
        ) from error
    alpha = isolated.getchannel("A")
    histogram = alpha.histogram()
    coverage = sum(level * count for level, count in enumerate(histogram)) / (
        max(isolated.width * isolated.height, 1) * 255
    )
    extrema = alpha.getextrema()
    if extrema[0] == extrema[1] or coverage < 0.002 or coverage > 0.995:
        raise BackgroundFailure(
            "background.subject_not_isolated",
            "The model could not isolate a usable foreground subject.",
        )
    preview_id = uuid4().hex
    root = preview_root(settings)
    target = root / f"{preview_id}.png"
    temporary = target.with_suffix(".tmp")
    try:
        isolated.save(temporary, format="PNG")
        os.replace(temporary, target)
        connection.execute(
            "INSERT INTO background_previews "
            "(id,media_item_id,source_revision_id,file_path,width,height,subject_coverage,model) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                preview_id,
                media_id,
                row["active_revision_id"],
                target.name,
                isolated.width,
                isolated.height,
                round(100.0 * coverage, 2),
                MODEL_NAME,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        target.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return connection.execute(
        "SELECT * FROM background_previews WHERE id=?", (preview_id,)
    ).fetchone()


def compose_background(connection, settings, media_id, preview_id, asset_id, blur, preserve=DEFAULT_PRESERVE):
    result = _compose_background_image(
        connection, settings, media_id, preview_id, asset_id, blur, preserve
    )
    return _store_composed_revision(
        connection, settings, media_id, result["preview"], result["asset"],
        result["image"], result["blur"], result["preserve"]
    )


def compose_background_preview(connection, settings, media_id, preview_id, asset_id, blur, preserve):
    result = _compose_background_image(
        connection, settings, media_id, preview_id, asset_id, blur, preserve
    )
    return result["image"]


def _compose_background_image(connection, settings, media_id, preview_id, asset_id, blur, preserve):
    preserve = preserve_value(preserve)
    try:
        asset_id = int(asset_id)
    except (TypeError, ValueError) as error:
        raise BackgroundFailure(
            "background.invalid_request", "Background and blur values are required."
        ) from error
    try:
        blur = float(blur)
    except (TypeError, ValueError) as error:
        raise BackgroundFailure(
            "background.invalid_request", "Background and blur values are required."
        ) from error
    if not 0 <= blur <= 100:
        raise BackgroundFailure(
            "background.invalid_blur", "Background blur must be between 0 and 100."
        )
    media = connection.execute(
        "SELECT active_revision_id FROM media_items "
        "WHERE id=? AND media_type='image' AND deleted_at IS NULL",
        (media_id,),
    ).fetchone()
    if media is None:
        raise BackgroundFailure("background.media_not_found", "Media item was not found.")
    preview = connection.execute(
        "SELECT * FROM background_previews WHERE id=? AND media_item_id=?",
        (str(preview_id or ""), media_id),
    ).fetchone()
    if preview is None:
        raise BackgroundFailure(
            "background.preview_required",
            "Create a successful background-removal preview before applying a background.",
        )
    asset = connection.execute(
        "SELECT * FROM background_assets WHERE id=?", (asset_id,)
    ).fetchone()
    if asset is None:
        raise BackgroundFailure("background.not_found", "Background was not found.")
    if int(preview["source_revision_id"]) != int(media["active_revision_id"]):
        raise BackgroundFailure(
            "background.preview_stale",
            "The preview is stale because the active image version changed.",
        )
    foreground_path = media_path(preview_root(settings), preview["file_path"])
    background_path = media_path(background_root(settings), asset["file_path"])
    if foreground_path is None or not foreground_path.is_file():
        raise BackgroundFailure(
            "background.preview_missing", "The background-removal preview is unavailable."
        )
    if background_path is None or not background_path.is_file():
        raise BackgroundFailure("background.not_found", "Background was not found.")
    try:
        with Image.open(foreground_path) as foreground_opened, Image.open(background_path) as background_opened:
            foreground = _apply_preserve(foreground_opened, preserve)
            background = ImageOps.fit(
                ImageOps.exif_transpose(background_opened).convert("RGB"),
                foreground.size,
                method=Image.Resampling.LANCZOS,
            ).convert("RGBA")
            if blur:
                background = background.filter(ImageFilter.GaussianBlur(blur / 5.0))
            result = Image.alpha_composite(background, foreground)
            return {
                "image": result,
                "preview": preview,
                "asset": asset,
                "blur": blur,
                "preserve": preserve,
            }
    except BackgroundFailure:
        raise
    except (OSError, ValueError) as error:
        raise BackgroundFailure(
            "background.compose_failed", "The background result could not be created."
        ) from error


def _store_composed_revision(connection, settings, media_id, preview, asset, result, blur, preserve):
    target = None
    try:
        directory = settings.media_path / "revisions"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"media-{media_id}-background-{uuid4().hex}.png"
        temporary = target.with_suffix(".tmp")
        result.save(temporary, format="PNG")
        os.replace(temporary, target)
        digest = _hash_file(target)
        relative = target.relative_to(settings.media_path).as_posix()
        details = json.dumps(
            {
                "background_id": asset["id"],
                "background_category": asset["category"],
                "blur": blur,
                "preserve": preserve,
                "model": MODEL_NAME,
                "preview_id": preview["id"],
                "source_revision_id": preview["source_revision_id"],
            }
        )
        cursor = connection.execute(
            "INSERT INTO media_revisions "
            "(media_item_id,parent_revision_id,file_path,operation,width,height,file_size,content_hash,details_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                media_id,
                preview["source_revision_id"],
                relative,
                "background_replace",
                result.width,
                result.height,
                target.stat().st_size,
                digest,
                details,
            ),
        )
        revision_id = int(cursor.lastrowid)
        connection.execute(
            "UPDATE media_items SET active_revision_id=?,file_path=?,width=?,height=?,file_size=?,content_hash=? WHERE id=?",
            (revision_id, relative, result.width, result.height, target.stat().st_size, digest, media_id),
        )
        connection.execute("DELETE FROM media_fingerprints WHERE media_item_id=?", (media_id,))
        connection.execute(
            "INSERT INTO operation_history (event_type,entity_type,entity_id,details_json) "
            "VALUES ('background.created','media',?,?)",
            (media_id, json.dumps({"revision_id": revision_id, "preview_id": preview["id"]})),
        )
        connection.commit()
        return revision_id
    except BackgroundFailure:
        raise
    except (OSError, ValueError) as error:
        connection.rollback()
        if target:
            target.unlink(missing_ok=True)
        raise BackgroundFailure(
            "background.compose_failed", "The background result could not be created."
        ) from error
    except Exception:
        connection.rollback()
        if target:
            target.unlink(missing_ok=True)
        raise


def background_root(settings):
    root = settings.database_path.parent / "backgrounds"
    root.mkdir(parents=True, exist_ok=True)
    return root


def preview_root(settings):
    root = settings.database_path.parent / "background-previews"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cleanup_expired_previews(connection, settings):
    # Previews are tied to a source revision and intentionally retained so a
    # user can change the background without running segmentation again.
    return None


def _hash_file(path):
    digest = sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
