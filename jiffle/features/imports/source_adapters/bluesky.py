"""Bluesky post source adapter via public AT Protocol XRPC API."""

from pathlib import PurePosixPath
import re
from urllib.parse import urlparse, urlsplit

import requests

from jiffle.features.imports.source_adapters.contracts import SourceMedia
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.features.imports.source_adapters.platform_tags import add_platform_tags

BSKY_THREAD_ENDPOINT = "https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread"
BSKY_DOMAINS = frozenset({"bsky.app", "www.bsky.app"})

_HASHTAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9_]+)", re.UNICODE)


class BlueskySourceProvider:
    provider_name = "bluesky"
    supports_reverse_search = False
    domains = BSKY_DOMAINS

    def can_handle(self, url: str) -> bool:
        parsed = urlparse(url)
        return (
            parsed.scheme in {"http", "https"}
            and (parsed.hostname or "").lower() in self.domains
            and _parse_post_url(url) is not None
        )

    def can_handle_set(self, _url: str) -> bool:
        return False

    def _get_json(self, params: dict[str, str]) -> dict:
        try:
            response = requests.get(
                BSKY_THREAD_ENDPOINT,
                params=params,
                headers={"User-Agent": "Jiffle/2.0", "Accept": "application/json"},
                timeout=15,
            )
            status = getattr(response, "status_code", None)
            if status in {400, 404}:
                raise SourceProviderFailure(
                    "import.source_post_not_found", "The Bluesky post was not found."
                )
            if status in {401, 403}:
                raise SourceProviderFailure(
                    "import.provider_access_denied", "Bluesky denied access to this post."
                )
            response.raise_for_status()
            payload = response.json()
        except SourceProviderFailure:
            raise
        except requests.HTTPError as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status in {400, 404}:
                raise SourceProviderFailure(
                    "import.source_post_not_found", "The Bluesky post was not found."
                ) from error
            if status in {401, 403}:
                raise SourceProviderFailure(
                    "import.provider_access_denied", "Bluesky denied access to this post."
                ) from error
            raise SourceProviderFailure(
                "import.provider_unavailable", "Bluesky metadata could not be loaded."
            ) from error
        except (requests.RequestException, ValueError, TypeError) as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "Bluesky metadata could not be loaded."
            ) from error
        if not isinstance(payload, dict):
            raise SourceProviderFailure(
                "import.provider_unavailable", "Bluesky returned invalid metadata."
            )
        return payload

    def fetch(self, url: str) -> SourceMedia:
        parsed = _parse_post_url(url)
        if parsed is None:
            raise SourceProviderFailure(
                "import.invalid_source_url", "The URL is not a Bluesky post URL."
            )
        actor, rkey = parsed
        at_uri = f"at://{actor}/app.bsky.feed.post/{rkey}"
        payload = self._get_json({"uri": at_uri, "depth": "0"})
        thread = payload.get("thread") if isinstance(payload.get("thread"), dict) else {}
        post = thread.get("post") if isinstance(thread.get("post"), dict) else payload.get("post")
        if not isinstance(post, dict):
            raise SourceProviderFailure(
                "import.source_post_not_found", "The Bluesky post was not found."
            )
        author_obj = post.get("author") if isinstance(post.get("author"), dict) else {}
        handle = str(author_obj.get("handle") or actor).strip()
        canonical_url = f"https://bsky.app/profile/{handle}/post/{rkey}"
        direct_url = _extract_image_url(post.get("embed"))
        if not direct_url:
            raise SourceProviderFailure(
                "import.source_media_missing",
                "The Bluesky post has no downloadable media.",
            )
        record = post.get("record") if isinstance(post.get("record"), dict) else {}
        tags = add_platform_tags(_extract_tags(record), self.provider_name, "bsky.app")
        return SourceMedia(
            canonical_url=canonical_url,
            direct_media_url=direct_url,
            provider=self.provider_name,
            remote_id=rkey,
            author=handle or None,
            domain="bsky.app",
            tags=tags,
            file_extension=_extension_from_bsky_url(direct_url),
        )

    def fetch_metadata(self, url: str) -> SourceMedia:
        return self.fetch(url)

    def search_similar(self, _image_path):
        return []

    def check_connection(self) -> None:
        response = requests.get(
            "https://public.api.bsky.app/xrpc/_health",
            headers={"User-Agent": "Jiffle/2.0", "Accept": "application/json"},
            timeout=15,
        )
        response.raise_for_status()


def _parse_post_url(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 4 and parts[0] == "profile" and parts[2] == "post":
        actor = parts[1].strip()
        rkey = parts[3].strip()
        if actor and rkey:
            return actor, rkey
    return None


def _extract_image_url(embed: object) -> str | None:
    if not isinstance(embed, dict):
        return None
    images = embed.get("images")
    if not isinstance(images, list):
        media = embed.get("media")
        if isinstance(media, dict):
            images = media.get("images")
    if isinstance(images, list):
        for image in images:
            if not isinstance(image, dict):
                continue
            fullsize = str(image.get("fullsize") or image.get("thumb") or "").strip()
            if fullsize:
                return fullsize
    return None


def _extract_tags(record: dict) -> tuple[str, ...]:
    tags: list[str] = []
    facets = record.get("facets")
    if isinstance(facets, list):
        for facet in facets:
            if not isinstance(facet, dict):
                continue
            features = facet.get("features")
            if not isinstance(features, list):
                continue
            for feature in features:
                if not isinstance(feature, dict):
                    continue
                tag_val = str(feature.get("tag") or "").strip().lstrip("#").lower()
                if tag_val:
                    tags.append(tag_val)
    raw_tags = record.get("tags")
    if isinstance(raw_tags, list):
        for raw_tag in raw_tags:
            tag_val = str(raw_tag or "").strip().lstrip("#").lower()
            if tag_val:
                tags.append(tag_val)
    text = str(record.get("text") or "")
    for match in _HASHTAG_RE.findall(text):
        cleaned = match.strip().lower()
        if cleaned and not cleaned.isdigit():
            tags.append(cleaned)
    return tuple(dict.fromkeys(tags))


def _extension_from_bsky_url(url: str) -> str:
    path = urlsplit(str(url)).path or ""
    suffix = PurePosixPath(path).suffix.lower()
    if not suffix and "@" in path:
        tail = path.rsplit("@", 1)[-1].lower()
        if tail in {"jpg", "jpeg", "png", "gif", "webp", "mp4", "webm"}:
            return ".jpg" if tail == "jpeg" else f".{tail}"
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".webm"}:
        return suffix
    return ".jpg"
