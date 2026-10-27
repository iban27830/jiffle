"""Fluffle reverse-image search adapter."""

import io
from pathlib import Path
import threading
from urllib.parse import urlparse

from PIL import Image
import requests

from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.features.imports.source_adapters.reverse_search import _open_preview_source

FLUFFLE_ENDPOINT = "https://api.fluffle.xyz/exact-search-by-file"
FLUFFLE_USER_AGENT = "Jiffle/2.0 (by developer)"
FLUFFLE_MIN_SIDE = 256
FLUFFLE_LIMIT = "16"
FLUFFLE_TIMEOUT = 15

_FLUFFLE_LOCK = threading.Lock()


def fluffle_preview_bytes(
    image_path: Path | str,
    min_side: int = FLUFFLE_MIN_SIDE,
) -> bytes | None:
    """Resize an image or representative video frame so ``min(width, height) == 256``."""
    path = Path(image_path)
    source = _open_preview_source(path)
    if source is None:
        return None
    try:
        with source:
            rgb = source.convert("RGB") if source.mode != "RGB" else source.copy()
            try:
                width, height = rgb.size
                if width <= 0 or height <= 0:
                    return None
                if width <= height:
                    new_width = min_side
                    new_height = max(min_side, int(round(height * (min_side / float(width)))))
                else:
                    new_height = min_side
                    new_width = max(min_side, int(round(width * (min_side / float(height)))))
                resampling = getattr(Image, "Resampling", Image).LANCZOS
                resized = rgb.resize((new_width, new_height), resampling)
                try:
                    buffer = io.BytesIO()
                    resized.save(buffer, format="PNG")
                    return buffer.getvalue()
                finally:
                    resized.close()
            finally:
                rgb.close()
    except (OSError, ValueError):
        return None


class FluffleReverseSearchProvider:
    provider_name = "fluffle"
    supports_reverse_search = True
    min_similar_confidence = 80.0
    endpoint = FLUFFLE_ENDPOINT
    timeout = FLUFFLE_TIMEOUT

    def can_handle(self, _url: str) -> bool:
        return False

    def can_handle_set(self, _url: str) -> bool:
        return False

    def _request(self, files, data, timeout):
        return requests.post(
            self.endpoint,
            files=files,
            data=data,
            headers={"User-Agent": FLUFFLE_USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
        )

    def search_similar(self, image_path: Path) -> list[dict[str, object]]:
        png_bytes = fluffle_preview_bytes(image_path)
        if png_bytes is None:
            return []
        data = {"includeNsfw": "true", "limit": FLUFFLE_LIMIT}
        if not _FLUFFLE_LOCK.acquire(timeout=12.0):
            raise SourceProviderFailure(
                "import.provider_timeout", "Fluffle reverse search timed out."
            )
        try:
            try:
                response = self._request(
                    files={"file": ("image.png", png_bytes, "image/png")},
                    data=data,
                    timeout=self.timeout,
                )
            finally:
                _FLUFFLE_LOCK.release()
            status = getattr(response, "status_code", None)
            if status == 429:
                raise SourceProviderFailure(
                    "import.rate_limited", "Fluffle rate limit was reached."
                )
            response.raise_for_status()
            payload = response.json()
        except SourceProviderFailure:
            raise
        except requests.HTTPError as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status == 429:
                raise SourceProviderFailure(
                    "import.rate_limited", "Fluffle rate limit was reached."
                ) from error
            raise SourceProviderFailure(
                "import.provider_unavailable", "Fluffle reverse search is unavailable."
            ) from error
        except (requests.RequestException, ValueError, TypeError) as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "Fluffle reverse search is unavailable."
            ) from error
        return _parse_results(payload, self.min_similar_confidence)


FluffleReverseSearch = FluffleReverseSearchProvider


def _extract_author(authors: object) -> str | None:
    if not isinstance(authors, (list, tuple)):
        return None
    for entry in authors:
        if not isinstance(entry, dict):
            continue
        author_id = str(entry.get("id") or "").strip()
        author_name = str(entry.get("name") or "").strip()
        if author_id and not author_id.isdigit():
            return author_id
        if author_name:
            return author_name
        if author_id:
            return author_id
    return None


def _normalize_matched_url(raw_url: str) -> str | None:
    url = str(raw_url or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return None
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in {"furaffinity.net", "www.furaffinity.net"} and not url.endswith("/"):
        return url + "/"
    return url


def _parse_results(payload: object, min_confidence: float = 80.0) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []
    results: list[dict[str, object]] = []
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        url = _normalize_matched_url(entry.get("url") or "")
        if not url:
            continue
        try:
            distance = float(entry.get("distance"))
        except (TypeError, ValueError):
            continue
        confidence = round(distance * 100.0, 2)
        match_kind = str(entry.get("match") or "").strip().lower()
        if match_kind == "exact" and confidence < min_confidence:
            confidence = round(float(min_confidence), 2)
        thumb = entry.get("thumbnail")
        preview_url = (
            str(thumb.get("url")).strip()
            if isinstance(thumb, dict) and str(thumb.get("url") or "").strip()
            else None
        )
        author = _extract_author(entry.get("authors"))
        raw_id = str(entry.get("id") or "").strip()
        remote_id = raw_id.split("_", 1)[1] if "_" in raw_id else (raw_id or None)
        domain = (urlparse(url).hostname or "").lower()
        if domain.startswith("www."):
            domain = domain[4:]
        results.append({
            "provider": FluffleReverseSearchProvider.provider_name,
            "canonical_url": url,
            "links": [url],
            "preview_url": preview_url,
            "author": author,
            "remote_id": remote_id,
            "domain": domain or None,
            "confidence": confidence,
            "match_method": "perceptual",
        })
    return results
