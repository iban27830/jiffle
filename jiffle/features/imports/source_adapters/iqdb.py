"""IQDB reverse-search adapter used after exact lookup fails.

IQDB answers with an HTML page that lists the best match plus additional
results, each with a similarity percentage and a link to the matched post.
Only a small preview is uploaded: the service resets or times out on large
uploads, and the original bytes are not needed for a perceptual match.
"""

import re
import threading
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

import requests

from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.features.imports.source_adapters.reverse_search import (
    REVERSE_SEARCH_USER_AGENT,
    reverse_preview_bytes,
)

_IQDB_LOCK = threading.Lock()
_IQDB_RATE_LIMIT_PHRASES = (
    "can't do more than one search at a time",
    "too many requests",
    "rate limit",
)

RESULT_TABLE = re.compile(r"<table\b.*?</table>", re.I | re.S)
SIMILARITY = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*similarity", re.I)
HREF = re.compile(r"<a[^>]+href=['\"]([^'\"]+)", re.I)
IMAGE = re.compile(r"<img[^>]+src=['\"]([^'\"]+)", re.I)
# Links that belong to the search service itself, not to a matched media post.
IGNORED_LINK_PARTS = (
    "iqdb.org", "saucenao.com", "ascii2d.net", "google.", "tineye.com",
)


class IqdbReverseSearch:
    provider_name = "iqdb"
    supports_reverse_search = True
    endpoint = "https://iqdb.org/"
    timeout = 15

    def _request(self, files, timeout):
        """Send the preview to IQDB.

        Kept as a small seam so the reverse search can be stubbed in tests
        without touching the shared ``requests`` module.
        """
        return requests.post(
            self.endpoint,
            files=files,
            headers={"User-Agent": REVERSE_SEARCH_USER_AGENT, "Accept": "text/html"},
            timeout=timeout,
        )

    def search_similar(self, image_path: Path) -> list[dict[str, object]]:
        preview = reverse_preview_bytes(image_path)
        if preview is None:
            return []
        if not _IQDB_LOCK.acquire(timeout=12.0):
            raise SourceProviderFailure(
                "import.provider_timeout", "IQDB reverse search timed out."
            )
        try:
            try:
                response = self._request(
                    files={"file": ("jiffle-preview.jpg", preview, "image/jpeg")},
                    timeout=self.timeout,
                )
            finally:
                _IQDB_LOCK.release()
            status = getattr(response, "status_code", None)
            if status in {429, 503}:
                raise SourceProviderFailure(
                    "import.rate_limited", "IQDB rate limit was reached."
                )
            response.raise_for_status()
        except SourceProviderFailure:
            raise
        except requests.HTTPError as error:
            resp = getattr(error, "response", None)
            status = getattr(resp, "status_code", None)
            body_text = str(getattr(resp, "text", "") or "").lower()
            if status in {429, 503} or any(
                phrase in body_text for phrase in _IQDB_RATE_LIMIT_PHRASES
            ):
                raise SourceProviderFailure(
                    "import.rate_limited", "IQDB rate limit was reached."
                ) from error
            raise SourceProviderFailure(
                "import.provider_unavailable", "IQDB reverse search is unavailable."
            ) from error
        except requests.RequestException as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "IQDB reverse search is unavailable."
            ) from error
        text = response.text or ""
        lowered = text.lower()
        if any(phrase in lowered for phrase in _IQDB_RATE_LIMIT_PHRASES):
            raise SourceProviderFailure(
                "import.rate_limited", "IQDB rate limit was reached."
            )
        return _parse_results(text)


def _parse_results(html: str) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    seen: set[str] = set()
    for block in RESULT_TABLE.findall(html):
        similarity = SIMILARITY.search(block)
        if similarity is None:
            continue
        links = []
        for href in HREF.findall(block):
            value = unescape(href).strip()
            if not value or value.startswith("#"):
                continue
            if any(part in value for part in IGNORED_LINK_PARTS):
                continue
            links.append(urljoin(IqdbReverseSearch.endpoint, value))
        links = list(dict.fromkeys(links))
        if not links or links[0] in seen:
            continue
        seen.add(links[0])
        image = IMAGE.search(block)
        results.append({
            "provider": IqdbReverseSearch.provider_name,
            "canonical_url": links[0],
            "preview_url": (
                urljoin(IqdbReverseSearch.endpoint, unescape(image.group(1)))
                if image else None
            ),
            "links": links,
            "confidence": float(similarity.group(1)),
            "match_method": "perceptual",
        })
    return results
