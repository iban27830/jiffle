from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import PurePosixPath
import time
from urllib.parse import urlparse

import requests

from jiffle.features.imports.source_adapters.contracts import SetPostIssue, SourceMedia, SourceSet
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure


class E621SourceProvider:
    provider_name = "e621"
    domains = {"e621.net", "e926.net"}
    page_limit = 320

    def __init__(self, login=None, api_key=None, request_interval: float = 0.5):
        self.login = login
        self.api_key = api_key
        self.request_interval = max(0.0, float(request_interval))
        self._last_request = 0.0

    def can_handle(self, url):
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.hostname in self.domains

    def can_handle_set(self, url: str) -> bool:
        return self.can_handle(url) and parse_set_url(url) is not None

    def fetch(self, url):
        parsed = urlparse(url)
        post_id = _post_id(parsed.path)
        if post_id is None:
            raise SourceProviderFailure("import.invalid_source_url", "The URL is not an e621 post URL.")
        domain = parsed.hostname or "e621.net"
        payload = self._get_json(f"https://{domain}/posts/{post_id}.json", context="post")
        post = payload["post"] if isinstance(payload, dict) and "post" in payload else payload
        if isinstance(post, list):
            post = post[0] if post else {}
        try:
            return self._post_to_source(post, domain, str(post_id))
        except (KeyError, TypeError, ValueError) as error:
            raise SourceProviderFailure("import.source_media_missing", "The source has no downloadable media.") from error

    def fetch_set(self, url: str) -> SourceSet:
        parsed = parse_set_url(url)
        if parsed is None:
            raise SourceProviderFailure("import.invalid_source_url", "The URL is not an e621 post set URL.")
        domain, set_id = parsed
        metadata_payload = self._get_json(f"https://{domain}/post_sets/{set_id}.json", context="set")
        metadata = metadata_payload.get("post_set", metadata_payload) if isinstance(metadata_payload, dict) else {}
        if not isinstance(metadata, dict):
            raise SourceProviderFailure("import.e621_set_invalid", "e621 returned invalid set metadata.")
        raw_ids = metadata.get("post_ids") or []
        if not isinstance(raw_ids, (list, tuple)):
            raise SourceProviderFailure("import.e621_set_invalid", "e621 returned invalid set post IDs.")
        post_ids = tuple(str(value) for value in raw_ids if str(value).isdigit())
        if not post_ids:
            raise SourceProviderFailure("import.e621_set_empty", "The e621 post set is empty.")
        shortname = str(metadata.get("shortname") or metadata.get("short_name") or metadata.get("name") or set_id)
        name = str(metadata.get("name") or shortname)

        posts_by_id: dict[str, dict] = {}
        page = 1
        while len(posts_by_id) < len(set(post_ids)):
            payload = self._get_json(
                f"https://{domain}/posts.json",
                params={"tags": f"set:{shortname}", "limit": self.page_limit, "page": page},
                context="set_posts",
            )
            raw_posts = payload.get("posts", []) if isinstance(payload, dict) else payload
            if not isinstance(raw_posts, list):
                raise SourceProviderFailure("import.e621_set_invalid", "e621 returned invalid set posts.")
            if not raw_posts:
                break
            previous_count = len(posts_by_id)
            for post in raw_posts:
                if isinstance(post, dict) and str(post.get("id", "")).isdigit():
                    posts_by_id.setdefault(str(post["id"]), post)
            if len(posts_by_id) == previous_count:
                break
            page += 1

        posts: list[SourceMedia] = []
        issues: list[SetPostIssue] = []
        for post_id in post_ids:
            post = posts_by_id.get(post_id)
            canonical = f"https://{domain}/posts/{post_id}"
            if post is None:
                issues.append(SetPostIssue(post_id, canonical, "import.source_post_unavailable", "The post was not returned by e621 while loading the set."))
                continue
            try:
                posts.append(self._post_to_source(post, domain, post_id))
            except (KeyError, TypeError, ValueError):
                issues.append(SetPostIssue(post_id, canonical, "import.source_media_missing", "The post has no downloadable media."))
        return SourceSet(
            canonical_url=f"https://{domain}/post_sets/{set_id}", provider=self.provider_name,
            remote_id=set_id, name=name, shortname=shortname, post_ids=post_ids,
            posts=tuple(posts), issues=tuple(issues),
        )

    def check_connection(self):
        self._get_json("https://e621.net/posts.json", params={"limit": 1}, context="connection")

    def _post_to_source(self, post: dict, domain: str, post_id: str) -> SourceMedia:
        file_payload = post.get("file") or {}
        sample_payload = post.get("sample") or {}
        direct_url = file_payload.get("url") or sample_payload.get("url")
        if not isinstance(direct_url, str) or not direct_url:
            raise ValueError("missing media")
        if direct_url.startswith("//"):
            direct_url = "https:" + direct_url
        tags_payload = post.get("tags") or {}
        artists = tags_payload.get("artist", [])
        tags = tuple(tag for group in tags_payload.values() if isinstance(group, (list, tuple)) for tag in group)
        return SourceMedia(
            canonical_url=f"https://{domain}/posts/{post_id}", direct_media_url=direct_url,
            provider=self.provider_name, remote_id=post_id,
            author=", ".join(str(artist) for artist in artists) if artists else None,
            domain=domain, tags=tags,
            file_extension=PurePosixPath(urlparse(direct_url).path).suffix.lower() or ".jpg",
        )

    def _get_json(self, url: str, params: dict | None = None, context: str = "post"):
        headers = {"User-Agent": f"Jiffle/2.0 (by {self.login or 'local-user'})", "Accept": "application/json"}
        auth = (self.login, self.api_key) if self.login and self.api_key else None
        for attempt in range(2):
            self._wait_for_rate_limit()
            try:
                response = requests.get(url, params=params, headers=headers, auth=auth, timeout=15)
            except requests.RequestException as error:
                raise SourceProviderFailure("import.e621_unavailable", "e621 is unavailable.") from error
            status = getattr(response, "status_code", None)
            if status == 429:
                if attempt == 0:
                    self._sleep_retry_after(response)
                    continue
                raise SourceProviderFailure("import.e621_rate_limited", "e621 rate limit was reached; try again later.")
            if status == 401:
                raise SourceProviderFailure("import.e621_authentication_failed", "The saved e621 credentials were rejected.")
            if status == 403:
                raise SourceProviderFailure("import.e621_access_denied", "e621 denied access to this resource.")
            if status == 404:
                is_set = context in {"set", "set_posts"}
                code = "import.e621_set_not_found" if is_set else "import.e621_post_not_found"
                message = "The e621 post set was not found." if is_set else "The e621 post was not found."
                raise SourceProviderFailure(code, message)
            try:
                if hasattr(response, "raise_for_status"):
                    response.raise_for_status()
                return response.json()
            except requests.RequestException as error:
                raise SourceProviderFailure("import.e621_unavailable", "e621 is unavailable.") from error
            except (ValueError, TypeError) as error:
                raise SourceProviderFailure("import.e621_invalid_response", "e621 returned invalid metadata.") from error
        raise SourceProviderFailure("import.e621_unavailable", "e621 is unavailable.")

    def _wait_for_rate_limit(self) -> None:
        if not self.request_interval:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)
        self._last_request = time.monotonic()

    @staticmethod
    def _sleep_retry_after(response) -> None:
        value = (getattr(response, "headers", None) or {}).get("Retry-After")
        if not value:
            return
        try:
            delay = max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                delay = 0.0
        if delay:
            time.sleep(delay)


def parse_set_url(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in E621SourceProvider.domains:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "post_sets":
        return None
    set_id = parts[1][:-5] if parts[1].endswith(".json") else parts[1]
    return (parsed.hostname, set_id) if set_id.isdigit() else None


def _post_id(path: str) -> int | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "posts":
        value = parts[1][:-5] if parts[1].endswith(".json") else parts[1]
        if value.isdigit():
            return int(value)
    return None
