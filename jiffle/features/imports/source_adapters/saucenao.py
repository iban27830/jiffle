"""SauceNAO reverse-search adapter used to discover FurAffinity sources.

FurAffinity has no reverse-image search of its own, so an image that exists
only there cannot be found by the booru IQDB endpoints.  SauceNAO indexes
FurAffinity (and many other galleries) and returns the matching post URLs,
which the shared import workflow then loads through the matching adapter.

The JSON API needs a registered account key, so the public HTML results page
is parsed instead.  Only a small preview is uploaded, matching the other
reverse-search adapters.
"""

import re
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.features.imports.source_adapters.reverse_search import reverse_preview_bytes

SAUCENAO_ENDPOINT = "https://saucenao.com/search.php"

# SauceNAO throttles and can filter clearly automated clients, so the
# anonymous HTML search is queried with a normal browser User-Agent.
SAUCENAO_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
SAUCENAO_TIMEOUT = 15
SAUCENAO_MAX_RESULTS = "16"

# One result is an element whose class is exactly ``result`` (optionally
# ``result hidden``); ``resultimage``/``resulttable`` are nested inside it.
RESULT_BLOCK = re.compile(r'<div class="result(?:\s[^"]*)?"', re.I)
SIMILARITY = re.compile(
    r'class="resultsimilarityinfo"[^>]*>\s*(\d+(?:\.\d+)?)\s*%', re.I
)
HREF = re.compile(r'href="([^"]+)"', re.I)
IMAGE_SOURCE = re.compile(r"<img[^>]+src=\"([^\"]+)\"", re.I)
TITLE = re.compile(r'class="resulttitle"[^>]*>\s*<strong>(.*?)</strong>', re.I | re.S)
AUTHOR = re.compile(
    r"(?:Author|Creator|Artist)\s*:\s*</strong>\s*<a[^>]*>(.*?)</a>", re.I | re.S
)
TAG = re.compile(r"<[^>]+>")


class SaucenaoReverseSearch:
    provider_name = "saucenao"
    endpoint = SAUCENAO_ENDPOINT
    timeout = SAUCENAO_TIMEOUT

    def _request(self, files, data, timeout):
        """Send the preview to SauceNAO.

        Kept as a small seam so the reverse search can be stubbed in tests
        without touching the shared ``requests`` module.
        """
        return requests.post(
            self.endpoint,
            files=files,
            data=data,
            headers={"User-Agent": SAUCENAO_USER_AGENT, "Accept": "text/html"},
            timeout=timeout,
        )

    def search_similar(self, image_path: Path) -> list[dict[str, object]]:
        preview = reverse_preview_bytes(image_path)
        if preview is None:
            return []
        try:
            response = self._request(
                files={"file": ("jiffle-preview.jpg", preview, "image/jpeg")},
                data={"db": "999", "numres": SAUCENAO_MAX_RESULTS},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.HTTPError as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status == 429:
                raise SourceProviderFailure(
                    "import.rate_limited", "SauceNAO rate limit was reached."
                ) from error
            raise SourceProviderFailure(
                "import.provider_unavailable", "SauceNAO reverse search is unavailable."
            ) from error
        except requests.RequestException as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "SauceNAO reverse search is unavailable."
            ) from error
        text = response.text or ""
        lowered = text.lower()
        if "rate limit" in lowered or "too many searches" in lowered:
            raise SourceProviderFailure(
                "import.rate_limited", "SauceNAO rate limit was reached."
            )
        return _parse_results(text)


def _parse_results(html: str) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for block in RESULT_BLOCK.split(html)[1:]:
        similarity = SIMILARITY.search(block)
        if similarity is None:
            continue
        links = _external_links(block)
        if not links:
            continue
        results.append({
            "provider": SaucenaoReverseSearch.provider_name,
            "canonical_url": links[0],
            "links": links,
            "preview_url": _preview_url(block),
            "title": _clean(TITLE.search(block)),
            "author": _clean(AUTHOR.search(block)),
            "confidence": float(similarity.group(1)),
            "match_method": "perceptual",
        })
    return results


def _external_links(block: str) -> list[str]:
    """Return matched-post links, dropping SauceNAO's own navigation links."""
    links: list[str] = []
    for raw in HREF.findall(block):
        value = unescape(raw).strip()
        if not value or value.startswith(("#", "javascript:", "mailto:")):
            continue
        absolute = urljoin(SAUCENAO_ENDPOINT, value)
        host = (urlparse(absolute).hostname or "").lower()
        if not host or host == "saucenao.com" or host.endswith(".saucenao.com"):
            continue
        links.append(absolute)
    return list(dict.fromkeys(links))


def _preview_url(block: str) -> str | None:
    image = IMAGE_SOURCE.search(block)
    if image is None:
        return None
    value = unescape(image.group(1)).strip()
    return urljoin(SAUCENAO_ENDPOINT, value) if value else None


def _clean(match) -> str | None:
    if match is None:
        return None
    value = unescape(TAG.sub("", match.group(1))).strip()
    return value or None
