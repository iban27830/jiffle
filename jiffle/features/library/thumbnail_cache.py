from hashlib import sha256
import os
from pathlib import Path

from PIL import Image, ImageOps

from jiffle.features.library.domain import MediaItem, MediaType


def thumbnail_path(item: MediaItem, cache_root: Path) -> Path:
    identity = sha256(item.file_path.encode("utf-8")).hexdigest()[:12]
    return cache_root / f"media-{item.id}-{identity}.jpg"


def ensure_thumbnail(item: MediaItem, source: Path, cache_root: Path) -> Path:
    destination = thumbnail_path(item, cache_root)
    if destination.is_file():
        return destination
    cache_root.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    try:
        image = _load_preview(item.media_type, source)
        with image:
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            normalized.thumbnail((480, 480), Image.Resampling.LANCZOS)
            normalized.save(temporary, format="JPEG", quality=85)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _load_preview(media_type: MediaType, source: Path) -> Image.Image:
    if media_type is MediaType.IMAGE:
        return Image.open(source)

    try:
        import cv2
    except ImportError as error:
        raise RuntimeError("Video thumbnail support is unavailable") from error
    capture = cv2.VideoCapture(str(source))
    try:
        success, frame = capture.read()
        if not success:
            raise ValueError("Could not read a video frame")
        return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    finally:
        capture.release()
