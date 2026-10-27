"""Yandex CBIR reverse-image search adapter."""

import html
import json
from pathlib import Path
import re
import threading
from urllib.parse import parse_qs, unquote, urlparse

import requests

from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.features.imports.source_adapters.joyreactor import (
    JoyReactorSourceProvider,
    extract_external_source_links,
)
from jiffle.features.imports.source_adapters.reverse_search import reverse_preview_bytes

YANDEX_UPLOAD_ENDPOINT = "https://yandex.ru/images-apphost/image-download"
YANDEX_SEARCH_ENDPOINT = "https://yandex.ru/images/search"
YANDEX_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
YANDEX_TIMEOUT = 10

_YANDEX_LOCK = threading.Lock()

_SUPPORTED_EXACT_HOSTS = (
    "reactor.cc",
    "joyreactor.cc",
    "joyreactor.com",
    "pornreactor.cc",
    "twitter.com",
    "x.com",
    "furaffinity.net",
    "e621.net",
    "danbooru.donmai.us",
    "gelbooru.com",
    "rule34.xxx",
    "bsky.app",
)

_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\]+",
    re.I,
)


class YandexReverseSearchProvider:
    provider_name = "yandex"
    supports_reverse_search = True
    min_similar_confidence = 80.0
    upload_endpoint = YANDEX_UPLOAD_ENDPOINT
    search_endpoint = YANDEX_SEARCH_ENDPOINT
    timeout = YANDEX_TIMEOUT

    def can_handle(self, _url: str) -> bool:
        return False

    def can_handle_set(self, _url: str) -> bool:
        return False

    def _upload_image(self, preview_bytes: bytes, timeout: int):
        return requests.post(
            self.upload_endpoint,
            params={
                "cbird": "111",
                "images_avatars_size": "preview",
                "images_avatars_namespace": "images-cbir",
            },
            data=preview_bytes,
            headers={
                "User-Agent": YANDEX_USER_AGENT,
                "Content-Type": "image/jpeg",
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    def _search_cbir(self, cbir_id: str, timeout: int):
        return requests.get(
            self.search_endpoint,
            params={"rpt": "imageview", "cbir_id": cbir_id},
            headers={
                "User-Agent": YANDEX_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=timeout,
        )

    def _resolve_joyreactor_external_links(self, url: str) -> tuple[str | None, str | None, list[str]]:
        """Query JoyReactor for a matched post/image URL and return ``(canonical_post_url, attr_id, external_links)``."""
        try:
            jr = JoyReactorSourceProvider()
            post_node, post_id, attr_id, _ext = jr.fetch_post_node(url)
            if not isinstance(post_node, dict):
                return (f"https://joyreactor.cc/post/{post_id}" if post_id else None), attr_id, []
            from jiffle.features.imports.source_adapters.joyreactor import _decode_global_id
            resolved_post_id = _decode_global_id(post_node.get("id")) or post_id
            canonical = (
                f"https://joyreactor.cc/post/{resolved_post_id}"
                if resolved_post_id
                else None
            )
            external_links = extract_external_source_links(post_node.get("text"))
            return canonical, attr_id, external_links
        except Exception:
            return None, None, []

    def search_similar(self, image_path: Path) -> list[dict[str, object]]:
        preview = reverse_preview_bytes(image_path, max_side=512, quality=90) or reverse_preview_bytes(image_path)
        if preview is None:
            return []
        if not _YANDEX_LOCK.acquire(timeout=12.0):
            raise SourceProviderFailure(
                "import.provider_timeout", "Yandex reverse search timed out."
            )
        try:
            try:
                upload_resp = self._upload_image(preview, self.timeout)
                if getattr(upload_resp, "status_code", None) == 429:
                    raise SourceProviderFailure(
                        "import.rate_limited", "Yandex reverse search rate limit was reached."
                    )
                upload_resp.raise_for_status()
                upload_json = upload_resp.json()
                if not isinstance(upload_json, dict):
                    return []
                cbir_id = str(upload_json.get("cbir_id") or "").strip()
                if not cbir_id and upload_json.get("image_shard") and upload_json.get("image_id"):
                    cbir_id = f"{upload_json['image_shard']}/{upload_json['image_id']}"
                if not cbir_id:
                    return []
                search_resp = self._search_cbir(cbir_id, self.timeout)
                if getattr(search_resp, "status_code", None) == 429:
                    raise SourceProviderFailure(
                        "import.rate_limited", "Yandex reverse search rate limit was reached."
                    )
                search_resp.raise_for_status()
                html_text = search_resp.text or ""
            finally:
                _YANDEX_LOCK.release()
        except SourceProviderFailure:
            raise
        except requests.HTTPError as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status == 429:
                raise SourceProviderFailure(
                    "import.rate_limited", "Yandex reverse search rate limit was reached."
                ) from error
            raise SourceProviderFailure(
                "import.provider_unavailable", "Yandex reverse search is unavailable."
            ) from error
        except (requests.RequestException, ValueError, TypeError) as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "Yandex reverse search is unavailable."
            ) from error

        raw_matches = _parse_yandex_html(html_text)
        enriched: list[dict[str, object]] = []
        seen_urls: set[str] = set()
        joyreactor_checked: set[str] = set()
        for match in raw_matches:
            url = str(match.get("canonical_url") or "")
            if not url:
                continue
            host = (urlparse(url).hostname or "").lower()
            if _is_supported_reactor_url(url) and len(joyreactor_checked) < 2:
                joyreactor_checked.add(url)
                canonical_post, attr_id, ext_links = self._resolve_joyreactor_external_links(url)
                for ext_url in ext_links:
                    if ext_url not in seen_urls:
                        seen_urls.add(ext_url)
                        enriched.append({
                            "provider": self.provider_name,
                            "canonical_url": ext_url,
                            "links": [ext_url],
                            "confidence": 96.0,
                            "match_method": "perceptual",
                        })
                if canonical_post and attr_id:
                    jr_link = f"{canonical_post}?attr={attr_id}"
                else:
                    jr_link = canonical_post or url
                if jr_link not in seen_urls:
                    seen_urls.add(jr_link)
                    updated = dict(match)
                    updated["canonical_url"] = canonical_post or url
                    updated["links"] = list(dict.fromkeys([jr_link, url, *(match.get("links") or [])]))
                    enriched.append(updated)
                continue
            if url not in seen_urls:
                seen_urls.add(url)
                enriched.append(match)
        return enriched


YandexReverseSearch = YandexReverseSearchProvider


def _is_supported_host(hostname: str) -> bool:
    host = (hostname or "").lower().strip(".")
    if not host:
        return False
    return any(host == dom or host.endswith("." + dom) for dom in _SUPPORTED_EXACT_HOSTS)


def _is_supported_reactor_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in ("reactor.cc", "joyreactor.cc", "joyreactor.com", "pornreactor.cc")):
        return False
    path = unquote(parsed.path or "")
    return bool(
        re.search(r"/post/\d+", path)
        or re.search(r"/pics/thumbnail/post-\d+\.", path)
        or re.search(r"/pics/post/(?:full/)?.*?(?:-|/post-)\d+\.", path)
    )


def _is_actionable_url(url: str) -> bool:
    if not url or "cbir_page=similar" in url or "avatars.mds.yandex.net" in url:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not _is_supported_host(host):
        return False
    if any(host == d or host.endswith("." + d) for d in ("reactor.cc", "joyreactor.cc", "joyreactor.com", "pornreactor.cc")):
        return _is_supported_reactor_url(url)
    return True


def _clean_url(raw_url: str) -> str:
    url = html.unescape(str(raw_url or "")).strip().rstrip(".,;'\")}]")
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.query and "utm_source=" in parsed.query:
        # Strip Yandex tracking query parameters from site links.
        params = parse_qs(parsed.query)
        filtered = {k: v for k, v in params.items() if not k.startswith("utm_")}
        if not filtered:
            url = url.split("?", 1)[0]
    return url


def _extract_json_block(text: str, marker: str) -> dict | None:
    idx = text.find(marker)
    if idx == -1:
        return None
    brace_start = text.find("{", idx + len(marker))
    if brace_start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for pos in range(brace_start, min(len(text), brace_start + 100_000)):
        ch = text[pos]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[brace_start : pos + 1])
                        return parsed if isinstance(parsed, dict) else None
                    except Exception:
                        return None
    return None


def _parse_yandex_html(raw_html: str) -> list[dict[str, object]]:
    unescaped = html.unescape(raw_html or "")
    candidate_urls: list[tuple[str, float, str | None]] = []

    other_sizes = _extract_json_block(unescaped, '"cbirOtherSizes":')
    has_other_sizes_block = isinstance(other_sizes, dict) and isinstance(
        other_sizes.get("items"), dict
    )
    dup_attr_ids: set[str] = set()
    if has_other_sizes_block:
        items = other_sizes["items"]
        total_dups = 0
        for group_name, confidence in (
            ("large_dups", 95.0),
            ("medium_dups", 92.0),
            ("small_dups", 90.0),
        ):
            dups = items.get(group_name)
            if isinstance(dups, list):
                total_dups += len(dups)
                for dup in dups:
                    if not isinstance(dup, dict):
                        continue
                    url = _clean_url(dup.get("url") or "")
                    attr_match = re.search(r"-(\d+)\.[a-zA-Z0-9]+$", url)
                    if attr_match:
                        dup_attr_ids.add(attr_match.group(1))
                    if _is_actionable_url(url):
                        candidate_urls.append((url, confidence, None))
        if total_dups == 0:
            # Yandex explicitly reported zero exact/other-size duplicates;
            # cbirSites in this case only lists visually similar pages.
            return []

    for block_marker in ('"cbirSites":', '"cbirSitesList":'):
        sites_block = _extract_json_block(unescaped, block_marker)
        if not isinstance(sites_block, dict):
            continue
        sites = sites_block.get("sites")
        if not isinstance(sites, list):
            continue
        for site in sites:
            if not isinstance(site, dict):
                continue
            page_url = _clean_url(site.get("url") or "")
            orig_img = site.get("originalImage")
            orig_url = (
                _clean_url(orig_img.get("url") or "")
                if isinstance(orig_img, dict)
                else ""
            )
            if dup_attr_ids and orig_url:
                orig_attr = re.search(r"-(\d+)\.[a-zA-Z0-9]+$", orig_url)
                if orig_attr and orig_attr.group(1) not in dup_attr_ids:
                    continue
            thumb = site.get("thumb")
            thumb_url = (
                _clean_url(thumb.get("url") or "")
                if isinstance(thumb, dict)
                else None
            )
            if thumb_url and thumb_url.startswith("//"):
                thumb_url = "https:" + thumb_url
            if _is_actionable_url(page_url):
                candidate_urls.append((page_url, 95.0, thumb_url))
            if _is_actionable_url(orig_url):
                candidate_urls.append((orig_url, 94.0, thumb_url))

    if not candidate_urls:
        # Fallback regex scan outside cbirSimilar when JSON blocks are reshaped.
        similar_pos = unescaped.find('"cbirSimilar":')
        sites_pos = unescaped.find('"cbirSites":')
        searchable_slices = (
            [unescaped[:similar_pos], unescaped[sites_pos:]]
            if similar_pos != -1 and sites_pos != -1 and similar_pos < sites_pos
            else [unescaped]
        )
        for chunk in searchable_slices:
            for match in _URL_RE.finditer(chunk):
                url = _clean_url(match.group(0))
                if _is_actionable_url(url):
                    candidate_urls.append((url, 92.0, None))

    results: list[dict[str, object]] = []
    seen: set[str] = set()
    for url, confidence, preview_url in candidate_urls:
        if url in seen:
            continue
        seen.add(url)
        results.append({
            "provider": YandexReverseSearchProvider.provider_name,
            "canonical_url": url,
            "links": [url],
            "preview_url": preview_url,
            "confidence": confidence,
            "match_method": "perceptual",
        })
    return results
