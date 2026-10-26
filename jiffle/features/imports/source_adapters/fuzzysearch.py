"""FuzzySearch reverse-image search and FurAffinity file lookup adapter."""

import os
from pathlib import Path
import re
import threading
from urllib.parse import urlparse

import requests

from jiffle.features.imports.source_adapters.contracts import SourceMedia
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.features.imports.source_adapters.platform_tags import add_platform_tags
from jiffle.features.imports.source_adapters.reverse_search import (
    REVERSE_SEARCH_USER_AGENT,
    reverse_preview_bytes,
)

DEFAULT_FUZZYSEARCH_API_KEY = (
    os.environ.get("JIFFLE_FUZZYSEARCH_API_KEY")
    or "eluIOaOhIP1RXlgYetkcZCF8la7p3NoCPy8U0i8dKiT4xdIH"
)
FUZZYSEARCH_IMAGE_ENDPOINT = "https://api.fuzzysearch.net/v1/image?distance=3"
FUZZYSEARCH_FA_FILE_ENDPOINT = "https://api.fuzzysearch.net/v1/file/furaffinity"
FUZZYSEARCH_TIMEOUT = 15

_FUZZYSEARCH_LOCK = threading.Lock()


class FuzzySearchReverseSearchProvider:
    provider_name = "fuzzysearch"
    supports_reverse_search = True
    min_similar_confidence = 80.0
    endpoint = FUZZYSEARCH_IMAGE_ENDPOINT
    timeout = FUZZYSEARCH_TIMEOUT

    def __init__(self, api_key: str | None = None):
        cleaned = str(api_key).strip() if api_key is not None else ""
        self.api_key = cleaned or DEFAULT_FUZZYSEARCH_API_KEY

    def can_handle(self, _url: str) -> bool:
        return False

    def can_handle_set(self, _url: str) -> bool:
        return False

    def _request(self, files, timeout):
        return requests.post(
            self.endpoint,
            files=files,
            headers={
                "x-api-key": self.api_key,
                "User-Agent": REVERSE_SEARCH_USER_AGENT,
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    def search_similar(self, image_path: Path) -> list[dict[str, object]]:
        preview = reverse_preview_bytes(image_path)
        if preview is None:
            return []
        try:
            with _FUZZYSEARCH_LOCK:
                response = self._request(
                    files={"image": ("jiffle-preview.jpg", preview, "image/jpeg")},
                    timeout=self.timeout,
                )
            status = getattr(response, "status_code", None)
            if status == 429:
                raise SourceProviderFailure(
                    "import.rate_limited", "FuzzySearch rate limit was reached."
                )
            if status in {401, 403}:
                raise SourceProviderFailure(
                    "import.provider_auth_required",
                    "FuzzySearch API key was rejected.",
                )
            response.raise_for_status()
            payload = response.json()
        except SourceProviderFailure:
            raise
        except requests.HTTPError as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status == 429:
                raise SourceProviderFailure(
                    "import.rate_limited", "FuzzySearch rate limit was reached."
                ) from error
            if status in {401, 403}:
                raise SourceProviderFailure(
                    "import.provider_auth_required",
                    "FuzzySearch API key was rejected.",
                ) from error
            raise SourceProviderFailure(
                "import.provider_unavailable",
                "FuzzySearch reverse search is unavailable.",
            ) from error
        except (requests.RequestException, ValueError, TypeError) as error:
            raise SourceProviderFailure(
                "import.provider_unavailable",
                "FuzzySearch reverse search is unavailable.",
            ) from error
        return _parse_results(payload)


FuzzySearchReverseSearch = FuzzySearchReverseSearchProvider


def _distance_to_confidence(distance_value: object) -> float | None:
    try:
        distance = float(distance_value)
    except (TypeError, ValueError):
        return None
    if distance < 0:
        return None
    return round(max(0.0, 100.0 - distance * 5.0), 2)


def _normalize_media_url(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("//"):
        return "https:" + text
    if not text.startswith(("http://", "https://")):
        return "https://" + text.lstrip("/")
    return text


def _extract_artists(item: dict) -> str | None:
    artists = item.get("artists")
    if isinstance(artists, (list, tuple)):
        parts = [str(artist).strip() for artist in artists if str(artist).strip()]
        if parts:
            return ", ".join(parts)
    artist = str(item.get("artist") or "").strip()
    return artist or None


def _parse_results(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, dict):
        items = payload.get("matches") or payload.get("results") or []
    elif isinstance(payload, list):
        items = payload
    else:
        return []
    results: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        confidence = _distance_to_confidence(item.get("distance", 0))
        if confidence is None:
            continue
        site = str(item.get("site") or "").strip().lower()
        site_id = str(item.get("site_id_str") or item.get("site_id") or item.get("id") or "").strip()
        direct_url = _normalize_media_url(item.get("url"))
        author = _extract_artists(item)
        raw_tags = item.get("tags")
        tag_list = (
            [str(tag).strip() for tag in raw_tags if str(tag).strip()]
            if isinstance(raw_tags, (list, tuple))
            else []
        )
        if site == "furaffinity" and site_id.isdigit():
            canonical_url = f"https://www.furaffinity.net/view/{site_id}/"
            tags = list(add_platform_tags(tag_list, "furaffinity", "furaffinity.net"))
            results.append({
                "provider": "furaffinity",
                "domain": "furaffinity.net",
                "remote_id": site_id,
                "canonical_url": canonical_url,
                "links": [canonical_url],
                "direct_media_url": direct_url,
                "author": author,
                "tags": tags,
                "confidence": confidence,
                "match_method": "perceptual",
            })
        elif site == "e621" and site_id.isdigit():
            canonical_url = f"https://e621.net/posts/{site_id}"
            tags = list(add_platform_tags(tag_list, "e621", "e621.net"))
            results.append({
                "provider": "e621",
                "domain": "e621.net",
                "remote_id": site_id,
                "canonical_url": canonical_url,
                "links": [canonical_url],
                "direct_media_url": direct_url,
                "author": author,
                "tags": tags,
                "confidence": confidence,
                "match_method": "perceptual",
            })
        elif site in {"twitter", "x"} and site_id.isdigit():
            user_part = (author.split(",")[0].strip().lstrip("@") if author else "") or "i/web"
            canonical_url = f"https://x.com/{user_part}/status/{site_id}"
            results.append({
                "provider": "twitter",
                "domain": "x.com",
                "remote_id": site_id,
                "canonical_url": canonical_url,
                "links": [canonical_url],
                "direct_media_url": direct_url,
                "author": author,
                "tags": tag_list,
                "confidence": confidence,
                "match_method": "perceptual",
            })
        elif site == "weasyl" and site_id.isdigit():
            canonical_url = f"https://www.weasyl.com/submission/{site_id}"
            results.append({
                "provider": FuzzySearchReverseSearchProvider.provider_name,
                "domain": "weasyl.com",
                "remote_id": site_id,
                "canonical_url": canonical_url,
                "links": [canonical_url],
                "direct_media_url": direct_url,
                "author": author,
                "tags": tag_list,
                "confidence": confidence,
                "match_method": "perceptual",
            })
    return results


def fetch_furaffinity_file_from_fuzzysearch(
    submission_id: str,
    api_key: str | None = None,
    timeout: int = FUZZYSEARCH_TIMEOUT,
) -> SourceMedia | None:
    """Resolve a FurAffinity submission via FuzzySearch's file lookup endpoint."""
    cleaned_id = str(submission_id or "").strip()
    if not cleaned_id.isdigit():
        return None
    resolved_key = (str(api_key).strip() if api_key else "") or DEFAULT_FUZZYSEARCH_API_KEY
    try:
        response = requests.get(
            FUZZYSEARCH_FA_FILE_ENDPOINT,
            params={"search": cleaned_id},
            headers={
                "x-api-key": resolved_key,
                "User-Agent": REVERSE_SEARCH_USER_AGENT,
                "Accept": "application/json",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError, TypeError):
        return None
    items = payload if isinstance(payload, list) else (
        payload.get("results") or payload.get("files") or [payload]
        if isinstance(payload, dict)
        else []
    )
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or item.get("site_id") or cleaned_id).strip()
        if item_id and item_id != cleaned_id:
            continue
        direct_url = _normalize_media_url(item.get("url"))
        if not direct_url:
            continue
        author = _extract_artists(item)
        raw_tags = item.get("tags")
        tag_values = (
            tuple(
                dict.fromkeys(
                    str(tag).strip()
                    for tag in raw_tags
                    if str(tag).strip()
                )
            )
            if isinstance(raw_tags, (list, tuple))
            else ()
        )
        tags = add_platform_tags(tag_values, "furaffinity", "furaffinity.net")
        raw_suffix = Path(urlparse(direct_url).path).suffix.lower()
        file_extension = (
            raw_suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", raw_suffix) else ".jpg"
        )
        return SourceMedia(
            canonical_url=f"https://www.furaffinity.net/view/{cleaned_id}/",
            direct_media_url=direct_url,
            provider="furaffinity",
            remote_id=cleaned_id,
            author=author,
            domain="furaffinity.net",
            tags=tags,
            file_extension=file_extension,
        )
    return None
