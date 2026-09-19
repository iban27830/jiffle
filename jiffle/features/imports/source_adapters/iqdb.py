"""IQDB reverse-search adapter used after exact lookup fails.

IQDB answers with an HTML page that lists the best match plus additional
results, each with a similarity percentage and a link to the matched post.
Only a small preview is uploaded: the service resets or times out on large
uploads, and the original bytes are not needed for a perceptual match.
"""

import re
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

import requests

from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.features.imports.source_adapters.reverse_search import (
    REVERSE_SEARCH_USER_AGENT,
    reverse_preview_bytes,
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
    endpoint = "https://iqdb.org/"
    timeout = 15

    def search_similar(self, image_path: Path) -> list[dict[str, object]]:
        preview = reverse_preview_bytes(image_path)
        if preview is None:
            return []
        try:
            response = requests.post(
                self.endpoint,
                files={"file": ("jiffle-preview.jpg", preview, "image/jpeg")},
                headers={
                    "User-Agent": REVERSE_SEARCH_USER_AGENT,
                    "Accept": "text/html",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "IQDB reverse search is unavailable."
            ) from error
        return _parse_results(response.text)


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
