"""Shared helpers for perceptual (reverse image) source lookup.

Reverse-search services expect a small preview instead of the full original.
Uploading the original file is slow and several services reset or reject large
requests, so every reverse-search adapter sends the same downscaled JPEG.

A video cannot be uploaded as-is either, so one representative frame is used as
its preview.  That lets a re-encoded, cropped or sample-quality video match the
post it came from even though its bytes differ from the original, which exact
MD5 lookup can never do.

One resolution asks several providers about the same file, so the encoded JPEG
is cached for the duration of the process; a video is decoded once instead of
once per provider.
"""

import io
from collections import OrderedDict
from pathlib import Path
from threading import Lock

from PIL import Image

REVERSE_PREVIEW_MAX_SIDE = 256
REVERSE_PREVIEW_QUALITY = 85
REVERSE_SEARCH_USER_AGENT = "Jiffle/2.0"
# A frame near the start is what boorus use for their own animated previews, but
# a title card, fade-in or black lead-in can be unrepresentative, so sample a
# little way into the video and fall back to the first frame when needed.
VIDEO_PREVIEW_POSITION = 0.2

_PREVIEW_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_PREVIEW_CACHE_LIMIT = 8
_PREVIEW_CACHE_LOCK = Lock()


def reverse_preview_bytes(
    image_path,
    max_side: int = REVERSE_PREVIEW_MAX_SIDE,
    quality: int = REVERSE_PREVIEW_QUALITY,
) -> bytes | None:
    """Return a small JPEG copy of an image or video for reverse-search uploads.

    Returns ``None`` when the file cannot be read as an image or a video frame,
    which tells the caller to skip the reverse search instead of uploading an
    unreadable file.
    """
    path = Path(image_path)
    key = _cache_key(path, max_side, quality)
    if key is not None:
        with _PREVIEW_CACHE_LOCK:
            cached = _PREVIEW_CACHE.get(key)
            if cached is not None:
                _PREVIEW_CACHE.move_to_end(key)
                return cached
    data = _render_preview(path, max_side, quality)
    if data is not None and key is not None:
        with _PREVIEW_CACHE_LOCK:
            _PREVIEW_CACHE[key] = data
            _PREVIEW_CACHE.move_to_end(key)
            while len(_PREVIEW_CACHE) > _PREVIEW_CACHE_LIMIT:
                _PREVIEW_CACHE.popitem(last=False)
    return data


def _cache_key(path: Path, max_side: int, quality: int) -> tuple | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_size, int(stat.st_mtime_ns), max_side, quality)


def _render_preview(path: Path, max_side: int, quality: int) -> bytes | None:
    image = _open_preview_source(path)
    if image is None:
        return None
    try:
        with image:
            if image.mode != "RGB":
                image = image.convert("RGB")
            image.thumbnail((max_side, max_side))
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=quality)
            return buffer.getvalue()
    except (OSError, ValueError):
        return None


def _open_preview_source(path: Path) -> Image.Image | None:
    try:
        return Image.open(path)
    except (OSError, ValueError):
        pass
    return _video_frame(path)


def _video_frame(path: Path) -> Image.Image | None:
    """Extract one representative frame from a video for the reverse search."""
    try:
        import cv2
    except ImportError:
        return None
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        return None
    try:
        try:
            count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        except Exception:
            count = 0
        if count > 1:
            target = min(count - 1, int(count * VIDEO_PREVIEW_POSITION))
            capture.set(cv2.CAP_PROP_POS_FRAMES, target)
        ok, frame = capture.read()
        if not ok or frame is None:
            capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = capture.read()
        if not ok or frame is None:
            return None
        return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    finally:
        capture.release()


def iqdb_query_matches(
    payload,
    provider: str,
    domain: str,
    post_url_prefix: str,
) -> list[dict[str, object]]:
    """Convert an ``/iqdb_queries.json`` response into source-match dicts.

    Danbooru and e621 return a list of ``{"score": ..., "post": {...}}`` items.
    The post object is sometimes wrapped as ``{"posts": {...}}`` by the newer
    e621 API.  Only the identity of the matched post is kept here; the caller
    loads full metadata before the candidate is offered for confirmation.
    """
    matches: list[dict[str, object]] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        post = item.get("post")
        if isinstance(post, dict) and isinstance(post.get("posts"), dict):
            post = post["posts"]
        post_id = (post or {}).get("id") if isinstance(post, dict) else None
        if post_id is None:
            post_id = item.get("post_id")
        if not str(post_id or "").isdigit():
            continue
        try:
            score = float(item.get("score", 0))
        except (TypeError, ValueError):
            continue
        match: dict[str, object] = {
            "provider": provider,
            "domain": domain,
            "remote_id": str(post_id),
            "canonical_url": f"{post_url_prefix}/{post_id}",
            "confidence": round(score, 2),
            "match_method": "perceptual",
        }
        preview = (post or {}).get("preview") if isinstance(post, dict) else None
        if isinstance(preview, dict) and preview.get("url"):
            match["preview_url"] = str(preview["url"])
        matches.append(match)
    return matches
