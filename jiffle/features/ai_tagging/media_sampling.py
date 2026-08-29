from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from jiffle.features.ai_tagging.contracts import MediaSample


def sample_media(path: Path, media_type: str) -> tuple[MediaSample, ...]:
    if media_type == "image":
        with Image.open(path) as image:
            return (_jpeg_sample(image),)
    return _video_samples(path)


def _video_samples(path: Path) -> tuple[MediaSample, ...]:
    try:
        import cv2
    except ImportError as error:
        raise ValueError("Video sampling is unavailable") from error
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise ValueError("Video could not be opened")
        frame_count = max(1, int(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
        samples = []
        for index in range(min(6, frame_count)):
            position = int(index * (frame_count - 1) / max(min(6, frame_count) - 1, 1))
            capture.set(cv2.CAP_PROP_POS_FRAMES, position)
            success, frame = capture.read()
            if success:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                samples.append(_jpeg_sample(Image.fromarray(rgb)))
        if not samples:
            raise ValueError("No video frames could be extracted")
        return tuple(samples)
    finally:
        capture.release()


def _jpeg_sample(image: Image.Image) -> MediaSample:
    normalized = ImageOps.exif_transpose(image).convert("RGB")
    normalized.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
    output = BytesIO()
    normalized.save(output, format="JPEG", quality=85)
    return MediaSample(output.getvalue(), "image/jpeg")
