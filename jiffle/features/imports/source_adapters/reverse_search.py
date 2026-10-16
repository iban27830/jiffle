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
import shutil
import subprocess
from threading import Lock

from PIL import Image, ImageStat

REVERSE_PREVIEW_MAX_SIDE = 256
REVERSE_PREVIEW_QUALITY = 85
REVERSE_PREVIEW_MAX_BYTES = 12_000
REVERSE_SEARCH_USER_AGENT = "Jiffle/2.0"
# A frame near the start is what boorus use for their own animated previews, but
# a title card, fade-in or black lead-in can be unrepresentative, so sample a
# little way into the video and fall back to the first frame when needed.
VIDEO_PREVIEW_POSITION = 0.2
_REVERSE_PREVIEW_FALLBACK_STEPS = (
    (224, 78),
    (192, 72),
    (160, 68),
    (128, 62),
    (96, 52),
)

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


def _encode_jpeg(
    image: Image.Image, max_side: int, quality: int, optimize: bool = False
) -> bytes:
    resized = image.copy()
    try:
        resized.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        resized.save(buffer, "JPEG", quality=quality, optimize=optimize)
        return buffer.getvalue()
    finally:
        resized.close()


def _render_preview(path: Path, max_side: int, quality: int) -> bytes | None:
    image = _open_preview_source(path)
    if image is None:
        return None
    try:
        with image:
            rgb = image.convert("RGB") if image.mode != "RGB" else image.copy()
            try:
                encoded = _encode_jpeg(rgb, max_side, quality)
                if len(encoded) > REVERSE_PREVIEW_MAX_BYTES:
                    for side, step_quality in _REVERSE_PREVIEW_FALLBACK_STEPS:
                        encoded = _encode_jpeg(
                            rgb,
                            min(side, max_side),
                            step_quality,
                            optimize=True,
                        )
                        if len(encoded) <= REVERSE_PREVIEW_MAX_BYTES:
                            break
                return encoded
            finally:
                rgb.close()
    except (OSError, ValueError):
        return None


def _open_preview_source(path: Path) -> Image.Image | None:
    try:
        return Image.open(path)
    except (OSError, ValueError):
        pass
    return extract_video_frame(path)


def extract_video_frame(
    path: Path, position: float = VIDEO_PREVIEW_POSITION
) -> Image.Image | None:
    """Extract one representative frame from a video via OpenCV or FFmpeg."""
    source_path = Path(path)
    if not source_path.is_file():
        return None
    frame = _video_frame_cv2(source_path, position)
    if frame is not None:
        return frame
    return _video_frame_ffmpeg(source_path, position)


def _video_frame(path: Path) -> Image.Image | None:
    """Backward-compatible wrapper around :func:`extract_video_frame`."""
    return extract_video_frame(path, VIDEO_PREVIEW_POSITION)


def _video_frame_cv2(path: Path, position: float) -> Image.Image | None:
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

        def _read_at(pos: float):
            if count > 1 and pos > 0:
                target = min(count - 1, max(0, int(count * pos)))
                capture.set(cv2.CAP_PROP_POS_FRAMES, target)
            elif pos == 0:
                capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, raw_frame = capture.read()
            return raw_frame if ok and raw_frame is not None else None

        frame = _read_at(position)
        if frame is None and position > 0:
            frame = _read_at(0.0)
        if frame is not None and count > 1 and _frame_mean_intensity(frame) < 8.0:
            for alt_pos in (0.5, 0.1, 0.0):
                if abs(alt_pos - position) < 1e-6:
                    continue
                alt_frame = _read_at(alt_pos)
                if alt_frame is not None and _frame_mean_intensity(alt_frame) >= 8.0:
                    frame = alt_frame
                    break
        if frame is None:
            return None
        return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    finally:
        capture.release()


def _frame_mean_intensity(frame) -> float:
    try:
        return float(frame.mean())
    except Exception:
        return 255.0


def _probe_video_duration(path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line or line.upper() == "N/A":
            continue
        try:
            duration = float(line)
        except ValueError:
            continue
        if duration > 0:
            return duration
    return None


def _ffmpeg_decode_png_frame(
    ffmpeg: str, path: Path, seek_seconds: float
) -> Image.Image | None:
    command = [
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-ss",
        f"{max(0.0, seek_seconds):.3f}",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    try:
        with Image.open(io.BytesIO(completed.stdout)) as opened:
            return opened.convert("RGB")
    except (OSError, ValueError):
        return None


def _pil_mean_intensity(image: Image.Image) -> float:
    try:
        means = ImageStat.Stat(image).mean
        return sum(means) / max(1, len(means))
    except Exception:
        return 255.0


def _video_frame_ffmpeg(path: Path, position: float) -> Image.Image | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    duration = _probe_video_duration(path) if position > 0 else None
    seek_seconds = max(0.0, duration * position) if duration and duration > 0 else 0.0
    frame = _ffmpeg_decode_png_frame(ffmpeg, path, seek_seconds)
    if frame is None and seek_seconds > 0:
        frame = _ffmpeg_decode_png_frame(ffmpeg, path, 0.0)
    if (
        frame is not None
        and duration
        and duration > 0
        and _pil_mean_intensity(frame) < 8.0
    ):
        for alt_pos in (0.5, 0.1, 0.0):
            alt_seek = max(0.0, duration * alt_pos)
            if abs(alt_seek - seek_seconds) < 1e-3:
                continue
            alt_frame = _ffmpeg_decode_png_frame(ffmpeg, path, alt_seek)
            if alt_frame is not None and _pil_mean_intensity(alt_frame) >= 8.0:
                frame.close()
                frame = alt_frame
                break
            if alt_frame is not None:
                alt_frame.close()
    return frame


def _valid_md5(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text if len(text) == 32 and all(c in "0123456789abcdef" for c in text) else None


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
        if isinstance(post, dict):
            preview = post.get("preview")
            preview_url = (
                preview.get("url")
                if isinstance(preview, dict) and preview.get("url")
                else (post.get("preview_file_url") or post.get("sample_file_url"))
            )
            if preview_url:
                match["preview_url"] = str(preview_url)
            file_obj = post.get("file")
            raw_md5 = post.get("md5") or (
                file_obj.get("md5") if isinstance(file_obj, dict) else None
            )
            md5_value = _valid_md5(raw_md5)
            if md5_value:
                match["content_md5"] = md5_value
        matches.append(match)
    return matches
