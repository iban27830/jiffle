"""Twitter / X status source adapter via FxTwitter JSON API."""

from pathlib import PurePosixPath
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlsplit, urlunsplit

import requests

from jiffle.features.imports.source_adapters.contracts import SourceMedia
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.features.imports.source_adapters.platform_tags import add_platform_tags

FXTWITTER_API_BASE = "https://api.fxtwitter.com"
TWITTER_DOMAINS = frozenset({
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
    "x.com",
    "www.x.com",
    "fxtwitter.com",
    "www.fxtwitter.com",
    "vxtwitter.com",
    "www.vxtwitter.com",
    "fixupx.com",
    "www.fixupx.com",
})

_HASHTAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9_]+)", re.UNICODE)


class TwitterSourceProvider:
    provider_name = "twitter"
    supports_reverse_search = False
    domains = TWITTER_DOMAINS

    def can_handle(self, url: str) -> bool:
        parsed = urlparse(url)
        return (
            parsed.scheme in {"http", "https"}
            and (parsed.hostname or "").lower() in self.domains
            and _parse_status_url(url) is not None
        )

    def can_handle_set(self, _url: str) -> bool:
        return False

    def _get_json(self, api_url: str) -> dict:
        try:
            response = requests.get(
                api_url,
                headers={"User-Agent": "Jiffle/2.0", "Accept": "application/json"},
                timeout=15,
            )
            status = getattr(response, "status_code", None)
            if status == 404:
                raise SourceProviderFailure(
                    "import.source_post_not_found", "The Twitter/X post was not found."
                )
            if status in {401, 403}:
                raise SourceProviderFailure(
                    "import.provider_access_denied", "Twitter/X denied access to this post."
                )
            response.raise_for_status()
            payload = response.json()
        except SourceProviderFailure:
            raise
        except requests.HTTPError as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status == 404:
                raise SourceProviderFailure(
                    "import.source_post_not_found", "The Twitter/X post was not found."
                ) from error
            if status in {401, 403}:
                raise SourceProviderFailure(
                    "import.provider_access_denied", "Twitter/X denied access to this post."
                ) from error
            raise SourceProviderFailure(
                "import.provider_unavailable", "Twitter/X metadata could not be loaded."
            ) from error
        except (requests.RequestException, ValueError, TypeError) as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "Twitter/X metadata could not be loaded."
            ) from error
        if not isinstance(payload, dict):
            raise SourceProviderFailure(
                "import.provider_unavailable", "Twitter/X returned invalid metadata."
            )
        return payload

    def fetch(self, url: str) -> SourceMedia:
        parsed = _parse_status_url(url)
        if parsed is None:
            raise SourceProviderFailure(
                "import.invalid_source_url", "The URL is not a Twitter/X status URL."
            )
        user, tweet_id, media_index = parsed
        api_url = (
            f"{FXTWITTER_API_BASE}/{user}/status/{tweet_id}"
            if user
            else f"{FXTWITTER_API_BASE}/status/{tweet_id}"
        )
        payload = self._get_json(api_url)
        if payload.get("code") == 404:
            raise SourceProviderFailure(
                "import.source_post_not_found", "The Twitter/X post was not found."
            )
        tweet = payload.get("tweet")
        if not isinstance(tweet, dict):
            raise SourceProviderFailure(
                "import.source_post_not_found", "The Twitter/X post was not found."
            )
        author_obj = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
        screen_name = str(author_obj.get("screen_name") or user or "").strip().lower() or None
        resolved_id = str(tweet.get("id") or tweet_id).strip()
        canonical_url = (
            f"https://x.com/{screen_name}/status/{resolved_id}"
            if screen_name
            else f"https://x.com/i/web/status/{resolved_id}"
        )
        direct_url, extension = _select_media(tweet.get("media"), media_index)
        if not direct_url:
            raise SourceProviderFailure(
                "import.source_media_missing",
                "The Twitter/X post has no downloadable media.",
            )
        tags = add_platform_tags(_extract_hashtags(tweet), self.provider_name, "x.com")
        return SourceMedia(
            canonical_url=canonical_url,
            direct_media_url=direct_url,
            provider=self.provider_name,
            remote_id=resolved_id,
            author=screen_name,
            domain="x.com",
            tags=tags,
            file_extension=extension,
        )

    def fetch_metadata(self, url: str) -> SourceMedia:
        return self.fetch(url)

    def search_similar(self, _image_path):
        return []

    def check_connection(self) -> None:
        response = requests.get(
            f"{FXTWITTER_API_BASE}/Twitter/status/20",
            headers={"User-Agent": "Jiffle/2.0", "Accept": "application/json"},
            timeout=15,
        )
        response.raise_for_status()


def _parse_status_url(url: str) -> tuple[str | None, str, int | None] | None:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    user: str | None = None
    tweet_id: str | None = None
    rest: list[str] = []
    if len(parts) >= 2 and parts[0] in {"status", "statuses"} and parts[1].isdigit():
        tweet_id = parts[1]
        rest = parts[2:]
    elif (
        len(parts) >= 3
        and parts[0] == "i"
        and parts[1] in {"status", "statuses"}
        and parts[2].isdigit()
    ):
        tweet_id = parts[2]
        rest = parts[3:]
    elif (
        len(parts) >= 4
        and parts[0] == "i"
        and parts[1] == "web"
        and parts[2] in {"status", "statuses"}
        and parts[3].isdigit()
    ):
        tweet_id = parts[3]
        rest = parts[4:]
    elif len(parts) >= 3 and parts[1] in {"status", "statuses"} and parts[2].isdigit():
        user = parts[0] if parts[0].lower() != "i" else None
        tweet_id = parts[2]
        rest = parts[3:]
    if not tweet_id:
        return None
    media_index: int | None = None
    if len(rest) >= 2 and rest[0] in {"photo", "video"} and rest[1].isdigit():
        idx = int(rest[1])
        if idx >= 1:
            media_index = idx
    return user, tweet_id, media_index


def _ensure_orig_photo_url(raw_url: str) -> str:
    url = str(raw_url or "").strip()
    if not url:
        return ""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host == "pbs.twimg.com" and "/media/" in parsed.path:
        query = parse_qs(parsed.query, keep_blank_values=True)
        if query.get("name") != ["orig"]:
            query["name"] = ["orig"]
            new_query = urlencode(query, doseq=True)
            return urlunsplit((parsed.scheme or "https", parsed.netloc, parsed.path, new_query, ""))
    return url


def _select_media(media_payload: object, media_index: int | None) -> tuple[str | None, str]:
    if not isinstance(media_payload, dict):
        return None, ".jpg"
    items: list[dict] = []
    if isinstance(media_payload.get("all"), list):
        items.extend(item for item in media_payload["all"] if isinstance(item, dict))
    else:
        for key in ("photos", "videos"):
            group = media_payload.get(key)
            if isinstance(group, list):
                items.extend(item for item in group if isinstance(item, dict))
    if not items:
        return None, ".jpg"
    index = (media_index - 1) if (media_index and 1 <= media_index <= len(items)) else 0
    chosen = items[index]
    raw_url = str(chosen.get("url") or "").strip()
    if not raw_url:
        return None, ".jpg"
    media_type = str(chosen.get("type") or "").strip().lower()
    if media_type in {"photo", "image", ""}:
        direct_url = _ensure_orig_photo_url(raw_url)
    else:
        direct_url = raw_url
    parsed = urlsplit(direct_url)
    suffix = PurePosixPath(parsed.path).suffix.lower()
    if not suffix and parsed.query:
        fmt = (parse_qs(parsed.query).get("format") or [""])[0].strip().lower().lstrip(".")
        if fmt:
            suffix = ".jpg" if fmt == "jpeg" else f".{fmt}"
    if not suffix:
        suffix = ".mp4" if media_type in {"video", "gif"} else ".jpg"
    return direct_url, suffix


def _extract_hashtags(tweet: dict) -> tuple[str, ...]:
    tags: list[str] = []
    text = str(tweet.get("text") or "")
    raw_text = tweet.get("raw_text")
    if isinstance(raw_text, dict):
        if raw_text.get("text"):
            text = f"{text}\n{raw_text.get('text')}"
        facets = raw_text.get("facets")
        if isinstance(facets, list):
            for facet in facets:
                if not isinstance(facet, dict):
                    continue
                if str(facet.get("type") or "").lower() in {"hashtag", "tag"}:
                    val = str(facet.get("original") or facet.get("display") or "").strip().lstrip("#")
                    if val and not val.isdigit():
                        tags.append(val.lower())
    for match in _HASHTAG_RE.findall(text):
        cleaned = match.strip().lower()
        if cleaned and not cleaned.isdigit():
            tags.append(cleaned)
    return tuple(dict.fromkeys(tags))
