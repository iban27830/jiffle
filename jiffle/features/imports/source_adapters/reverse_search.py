"""Shared helpers for perceptual (reverse image) source lookup.

Reverse-search services expect a small preview instead of the full original.
Uploading the original file is slow and several services reset or reject large
requests, so every reverse-search adapter sends the same downscaled JPEG.
"""

import io

from PIL import Image

REVERSE_PREVIEW_MAX_SIDE = 256
REVERSE_PREVIEW_QUALITY = 85
REVERSE_SEARCH_USER_AGENT = "Jiffle/2.0"


def reverse_preview_bytes(
    image_path,
    max_side: int = REVERSE_PREVIEW_MAX_SIDE,
    quality: int = REVERSE_PREVIEW_QUALITY,
) -> bytes | None:
    """Return a small JPEG copy of an image for reverse-search uploads.

    Returns ``None`` when the file cannot be read as an image, which tells the
    caller to skip the reverse search instead of uploading an unreadable file.
    """
    try:
        with Image.open(image_path) as image:
            if image.mode != "RGB":
                image = image.convert("RGB")
            image.thumbnail((max_side, max_side))
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=quality)
            return buffer.getvalue()
    except (OSError, ValueError):
        return None


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
