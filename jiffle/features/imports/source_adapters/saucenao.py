"""SauceNAO reverse-search adapter used to discover FurAffinity sources.

FurAffinity has no reverse-image search of its own, so an image that exists
only there cannot be found by the booru IQDB endpoints.  SauceNAO indexes
FurAffinity (and many other galleries) and returns the matching post URLs,
which the shared import workflow then loads through the matching adapter.

The JSON API needs a registered account key, so the public HTML results page
is parsed instead.  Only a small preview is uploaded, matching the other
reverse-search adapters.
"""

import json
import re
import threading
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

_SAUCENAO_LOCK = threading.Lock()
_SAUCENAO_RATE_LIMIT_PHRASES = (
    "search rate too high",
    "daily search limit",
    "exceeded your daily",
    "rate limit",
    "too many searches",
    "too many requests",
)

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
    supports_reverse_search = True
    credentials_improve_results = True
    endpoint = SAUCENAO_ENDPOINT
    timeout = SAUCENAO_TIMEOUT

    def __init__(self, api_key: str | None = None):
        cleaned = str(api_key).strip() if api_key is not None else ""
        self.api_key = cleaned or None

    def can_handle(self, _url: str) -> bool:
        return False

    def can_handle_set(self, _url: str) -> bool:
        return False

    def _request(self, files, data, timeout):
        """Send the preview to SauceNAO.

        Kept as a small seam so the reverse search can be stubbed in tests
        without touching the shared ``requests`` module.
        """
        accept = (
            "application/json, text/html;q=0.9"
            if isinstance(data, dict) and data.get("output_type") == "2"
            else "text/html"
        )
        return requests.post(
            self.endpoint,
            files=files,
            data=data,
            headers={"User-Agent": SAUCENAO_USER_AGENT, "Accept": accept},
            timeout=timeout,
        )

    def search_similar(self, image_path: Path) -> list[dict[str, object]]:
        preview = reverse_preview_bytes(image_path)
        if preview is None:
            return []
        data: dict[str, str] = {"db": "999", "numres": SAUCENAO_MAX_RESULTS}
        if self.api_key:
            data["output_type"] = "2"
            data["api_key"] = self.api_key
        try:
            with _SAUCENAO_LOCK:
                response = self._request(
                    files={"file": ("jiffle-preview.jpg", preview, "image/jpeg")},
                    data=data,
                    timeout=self.timeout,
                )
            status = getattr(response, "status_code", None)
            if status == 429:
                raise SourceProviderFailure(
                    "import.rate_limited", "SauceNAO rate limit was reached."
                )
            response.raise_for_status()
        except SourceProviderFailure:
            raise
        except requests.HTTPError as error:
            resp = getattr(error, "response", None)
            status = getattr(resp, "status_code", None)
            body_text = str(getattr(resp, "text", "") or "")
            if status == 429 or _is_rate_limited_text(body_text):
                raise SourceProviderFailure(
                    "import.rate_limited", "SauceNAO rate limit was reached."
                ) from error
            if status in {401, 403} and self.api_key:
                raise SourceProviderFailure(
                    "import.provider_auth_required",
                    "SauceNAO API key was rejected.",
                ) from error
            raise SourceProviderFailure(
                "import.provider_unavailable", "SauceNAO reverse search is unavailable."
            ) from error
        except requests.RequestException as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "SauceNAO reverse search is unavailable."
            ) from error

        if self.api_key:
            payload = _try_json_payload(response)
            if isinstance(payload, dict):
                return _parse_json_results(payload)

        text = response.text or ""
        if _is_rate_limited_text(text):
            raise SourceProviderFailure(
                "import.rate_limited", "SauceNAO rate limit was reached."
            )
        payload = _try_json_payload(response)
        if isinstance(payload, dict) and ("results" in payload or "header" in payload):
            return _parse_json_results(payload)
        return _parse_results(text)

    def check_connection(self) -> None:
        params: dict[str, str] = {"db": "999", "numres": "1"}
        if self.api_key:
            params["output_type"] = "2"
            params["api_key"] = self.api_key
        try:
            with _SAUCENAO_LOCK:
                response = requests.get(
                    self.endpoint,
                    params=params,
                    headers={"User-Agent": SAUCENAO_USER_AGENT},
                    timeout=self.timeout,
                )
            status = getattr(response, "status_code", None)
            if status == 429:
                raise SourceProviderFailure(
                    "import.rate_limited", "SauceNAO rate limit was reached."
                )
            if status in {401, 403}:
                raise SourceProviderFailure(
                    "import.provider_auth_required", "SauceNAO API key was rejected."
                )
            response.raise_for_status()
        except SourceProviderFailure:
            raise
        except requests.HTTPError as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status == 429:
                raise SourceProviderFailure(
                    "import.rate_limited", "SauceNAO rate limit was reached."
                ) from error
            if status in {401, 403}:
                raise SourceProviderFailure(
                    "import.provider_auth_required", "SauceNAO API key was rejected."
                ) from error
            raise SourceProviderFailure(
                "import.provider_unavailable", "SauceNAO reverse search is unavailable."
            ) from error
        except requests.RequestException as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "SauceNAO reverse search is unavailable."
            ) from error
        text = getattr(response, "text", "") or ""
        if _is_rate_limited_text(text):
            raise SourceProviderFailure(
                "import.rate_limited", "SauceNAO rate limit was reached."
            )
        payload = _try_json_payload(response)
        if isinstance(payload, dict):
            header = payload.get("header")
            if isinstance(header, dict):
                message = str(header.get("message") or "")
                if _is_rate_limited_text(message):
                    raise SourceProviderFailure(
                        "import.rate_limited", "SauceNAO rate limit was reached."
                    )
                if "invalid" in message.lower() and "key" in message.lower():
                    raise SourceProviderFailure(
                        "import.provider_auth_required", "SauceNAO API key was rejected."
                    )


def _is_rate_limited_text(text: str | None) -> bool:
    lowered = str(text or "").lower()
    return any(phrase in lowered for phrase in _SAUCENAO_RATE_LIMIT_PHRASES)


def _try_json_payload(response) -> dict | None:
    json_method = getattr(response, "json", None)
    if callable(json_method):
        try:
            payload = json_method()
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
    text = str(getattr(response, "text", "") or "").strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                return payload
        except (TypeError, ValueError):
            return None
    return None


def _valid_md5(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text if len(text) == 32 and all(c in "0123456789abcdef" for c in text) else None


def _parse_json_results(payload: dict) -> list[dict[str, object]]:
    header = payload.get("header")
    if isinstance(header, dict):
        message = str(header.get("message") or "")
        if _is_rate_limited_text(message):
            raise SourceProviderFailure(
                "import.rate_limited", "SauceNAO rate limit was reached."
            )
        try:
            status_code = int(header.get("status", 0) or 0)
        except (TypeError, ValueError):
            status_code = 0
        if status_code < 0:
            lowered_msg = message.lower()
            if (
                _is_rate_limited_text(lowered_msg)
                or "limit" in lowered_msg
                or "rate" in lowered_msg
            ):
                raise SourceProviderFailure(
                    "import.rate_limited", "SauceNAO rate limit was reached."
                )
            if "api key" in lowered_msg or "account" in lowered_msg or "auth" in lowered_msg:
                raise SourceProviderFailure(
                    "import.provider_auth_required", "SauceNAO API key was rejected."
                )
            raise SourceProviderFailure(
                "import.provider_unavailable", "SauceNAO reverse search is unavailable."
            )

    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []
    results: list[dict[str, object]] = []
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        item_header = entry.get("header")
        item_data = entry.get("data")
        if not isinstance(item_header, dict) or not isinstance(item_data, dict):
            continue
        try:
            confidence = float(item_header.get("similarity"))
        except (TypeError, ValueError):
            continue
        links = _json_item_links(item_data)
        if not links:
            continue
        thumbnail = str(item_header.get("thumbnail") or "").strip()
        preview_url = urljoin(SAUCENAO_ENDPOINT, thumbnail) if thumbnail else None
        author = _json_author(item_data)
        title = str(item_data.get("title") or "").strip() or None
        characters = _json_characters(item_data.get("characters"))
        content_md5 = _valid_md5(item_data.get("md5"))
        match: dict[str, object] = {
            "provider": SaucenaoReverseSearch.provider_name,
            "canonical_url": links[0],
            "links": links,
            "preview_url": preview_url,
            "title": title,
            "author": author,
            "confidence": confidence,
            "match_method": "perceptual",
        }
        if content_md5:
            match["content_md5"] = content_md5
        if characters:
            match["character_tags"] = characters
        results.append(match)
    return results


def _json_item_links(data: dict) -> list[str]:
    links: list[str] = []
    ext_urls = data.get("ext_urls")
    if isinstance(ext_urls, (list, tuple)):
        for raw in ext_urls:
            if not isinstance(raw, str) or not raw.strip():
                continue
            absolute = urljoin(SAUCENAO_ENDPOINT, raw.strip())
            host = (urlparse(absolute).hostname or "").lower()
            if not host or host == "saucenao.com" or host.endswith(".saucenao.com"):
                continue
            links.append(absolute)

    id_templates = (
        ("e621_id", "https://e621.net/posts/{id}"),
        ("danbooru_id", "https://danbooru.donmai.us/posts/{id}"),
        ("gelbooru_id", "https://gelbooru.com/index.php?page=post&s=view&id={id}"),
        ("fa_id", "https://www.furaffinity.net/view/{id}/"),
        ("sankaku_id", "https://chan.sankakucomplex.com/post/show/{id}"),
        ("yandere_id", "https://yande.re/post/show/{id}"),
        ("konachan_id", "https://konachan.com/post/show/{id}"),
        ("pixiv_id", "https://www.pixiv.net/artworks/{id}"),
    )
    for field, template in id_templates:
        raw_id = data.get(field)
        if raw_id is not None and str(raw_id).strip():
            links.append(template.format(id=str(raw_id).strip()))
    return list(dict.fromkeys(links))


def _json_author(data: dict) -> str | None:
    for key in ("author_name", "creator", "member_name", "user_name"):
        value = data.get(key)
        if isinstance(value, (list, tuple)):
            parts = [str(item).strip() for item in value if str(item).strip()]
            if parts:
                return ", ".join(parts)
        elif isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _json_characters(raw) -> list[str]:
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str) and raw.strip():
        return [
            part.strip()
            for part in re.split(r"[,;\n]+", raw)
            if part.strip()
        ]
    return []


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
