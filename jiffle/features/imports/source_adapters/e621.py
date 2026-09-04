from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import PurePosixPath
import threading
import time
from urllib.parse import urlparse

import requests

from jiffle.features.imports.source_adapters.contracts import SetPostIssue, SourceMedia, SourceSet
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure


class E621SourceProvider:
    provider_name = "e621"
    domains = {"e621.net", "e926.net"}
    page_limit = 320
    _transient_statuses = {408, 429, 500, 502, 503, 504}
    _max_attempts = 3

    def __init__(self, login=None, api_key=None, request_interval: float = 0.5):
        self.login = login
        self.api_key = api_key
        self.request_interval = max(0.0, float(request_interval))
        self._last_request = 0.0
        self._rate_lock = threading.Lock()

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

    def fetch_metadata(self, url):
        """Load post metadata even when the post's media URL was removed."""
        parsed = urlparse(url)
        post_id = _post_id(parsed.path)
        if post_id is None:
            raise SourceProviderFailure("import.invalid_source_url", "The URL is not an e621 post URL.")
        domain = parsed.hostname or "e621.net"
        payload = self._get_json(f"https://{domain}/posts/{post_id}.json", context="post")
        post = payload["post"] if isinstance(payload, dict) and "post" in payload else payload
        if isinstance(post, list):
            post = post[0] if post else {}
        if not isinstance(post, dict):
            raise SourceProviderFailure("import.source_media_missing", "The source has no metadata.")
        return self._post_to_source(post, domain, str(post_id), allow_missing=True)

    def metadata_md5(self, url: str) -> str | None:
        """Return the stored file MD5 even when e621 has deleted the media."""
        parsed = urlparse(url)
        post_id = _post_id(parsed.path)
        if post_id is None:
            raise SourceProviderFailure("import.invalid_source_url", "The URL is not an e621 post URL.")
        domain = parsed.hostname or "e621.net"
        payload = self._get_json(f"https://{domain}/posts/{post_id}.json", context="post")
        post = payload["post"] if isinstance(payload, dict) and "post" in payload else payload
        if isinstance(post, list):
            post = post[0] if post else {}
        if not isinstance(post, dict):
            return None
        value = str((post.get("file") or {}).get("md5") or "").lower()
        return value if _valid_md5(value) else None

    def search_by_md5(self, digest: str) -> list[dict[str, object]]:
        digest = _valid_md5(digest)
        if digest is None:
            return []
        payload = self._get_json(
            "https://e621.net/posts.json",
            params={"tags": f"md5:{digest} status:any", "limit": 100},
            context="search",
        )
        posts = payload.get("posts", []) if isinstance(payload, dict) else payload
        matches: list[dict[str, object]] = []
        for post in posts if isinstance(posts, list) else []:
            if not isinstance(post, dict) or not str(post.get("id", "")).isdigit():
                continue
            post_id = str(post["id"])
            file_payload = post.get("file") or {}
            sample_payload = post.get("sample") or {}
            direct_url = _media_url(file_payload, sample_payload)
            tags_payload = post.get("tags") or {}
            tags = tuple(
                tag
                for group in tags_payload.values()
                if isinstance(group, (list, tuple))
                for tag in group
            )
            artists = tags_payload.get("artist", [])
            matches.append({
                "provider": self.provider_name,
                "domain": "e621.net",
                "remote_id": post_id,
                "canonical_url": f"https://e621.net/posts/{post_id}",
                "direct_media_url": direct_url,
                "author": ", ".join(str(artist) for artist in artists) if artists else None,
                "tags": list(tags),
                "content_md5": _valid_md5((file_payload or {}).get("md5")),
                "width": file_payload.get("width"),
                "height": file_payload.get("height"),
                "deleted": bool((post.get("flags") or {}).get("deleted")),
            })
        return matches

    def search_similar(self, image_path):
        return []

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
            if post is None:
                # Deleted posts are omitted from the set listing, but their
                # individual metadata can still include the original MD5.
                try:
                    payload = self._get_json(
                        f"https://{domain}/posts/{post_id}.json", context="post"
                    )
                    post = payload.get("post", payload) if isinstance(payload, dict) else payload
                    if isinstance(post, list):
                        post = post[0] if post else None
                except SourceProviderFailure:
                    post = None
            canonical = f"https://{domain}/posts/{post_id}"
            if post is None:
                issues.append(SetPostIssue(post_id, canonical, "import.source_post_unavailable", "The post was not returned by e621 while loading the set."))
                continue
            try:
                source = self._post_to_source(post, domain, post_id, allow_missing=True)
                if source.direct_media_url or source.content_md5:
                    posts.append(source)
                else:
                    issues.append(SetPostIssue(post_id, canonical, "import.source_media_missing", "The post has no downloadable media."))
            except (KeyError, TypeError, ValueError):
                issues.append(SetPostIssue(post_id, canonical, "import.source_media_missing", "The post has no downloadable media."))
        return SourceSet(
            canonical_url=f"https://{domain}/post_sets/{set_id}", provider=self.provider_name,
            remote_id=set_id, name=name, shortname=shortname, post_ids=post_ids,
            posts=tuple(posts), issues=tuple(issues),
        )

    def check_connection(self):
        self._get_json("https://e621.net/posts.json", params={"limit": 1}, context="connection")

    def _post_to_source(self, post: dict, domain: str, post_id: str, allow_missing: bool = False) -> SourceMedia:
        file_payload = post.get("file") or {}
        sample_payload = post.get("sample") or {}
        direct_url = _media_url(file_payload, sample_payload)
        if not direct_url and not allow_missing:
            raise ValueError("missing media")
        tags_payload = post.get("tags") or {}
        artists = tags_payload.get("artist", [])
        tags = tuple(tag for group in tags_payload.values() if isinstance(group, (list, tuple)) for tag in group)
        character_tags = tuple(str(tag) for tag in tags_payload.get("character", []) if str(tag).strip())
        raw_parent_id = post.get("parent_id")
        parent_id = str(raw_parent_id) if raw_parent_id not in (None, "", 0, "0") else None
        file_extension = PurePosixPath(urlparse(direct_url or "").path).suffix.lower()
        if not file_extension:
            raw_extension = _valid_extension(file_payload.get("ext"))
            file_extension = f".{raw_extension}" if raw_extension else ".jpg"
        return SourceMedia(
            canonical_url=f"https://{domain}/posts/{post_id}", direct_media_url=direct_url,
            provider=self.provider_name, remote_id=post_id,
            author=", ".join(str(artist) for artist in artists) if artists else None,
            domain=domain, tags=tags,
            file_extension=file_extension,
            character_tags=character_tags, parent_id=parent_id,
            content_md5=_valid_md5(file_payload.get("md5")),
        )

    def _get_json(self, url: str, params: dict | None = None, context: str = "post"):
        headers = {"User-Agent": f"Jiffle/2.0 (by {self.login or 'local-user'})", "Accept": "application/json"}
        auth = (self.login, self.api_key) if self.login and self.api_key else None
        for attempt in range(self._max_attempts):
            self._wait_for_rate_limit()
            try:
                response = requests.get(url, params=params, headers=headers, auth=auth, timeout=15)
            except requests.RequestException as error:
                if _is_transient_request_error(error) and attempt < self._max_attempts - 1:
                    _sleep_backoff(None, attempt)
                    continue
                error_status = getattr(getattr(error, "response", None), "status_code", None)
                if error_status in self._transient_statuses and attempt < self._max_attempts - 1:
                    _sleep_backoff(getattr(error, "response", None), attempt)
                    continue
                if error_status == 429:
                    raise SourceProviderFailure("import.e621_rate_limited", "e621 rate limit was reached; try again later.") from error
                code = "import.e621_timeout" if isinstance(error, requests.Timeout) else "import.e621_unavailable"
                message = "e621 request timed out." if code.endswith("timeout") else "e621 is unavailable."
                raise SourceProviderFailure(code, message) from error
            status = getattr(response, "status_code", None)
            if status in self._transient_statuses:
                if attempt < self._max_attempts - 1:
                    _sleep_backoff(response, attempt)
                    continue
                if status == 429:
                    raise SourceProviderFailure("import.e621_rate_limited", "e621 rate limit was reached; try again later.")
                raise SourceProviderFailure("import.e621_unavailable", f"e621 returned temporary HTTP {status}.")
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
        with self._rate_lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.request_interval:
                time.sleep(self.request_interval - elapsed)
            self._last_request = time.monotonic()



def _media_url(file_payload: dict, sample_payload: dict) -> str | None:
    """Prefer the original file URL and rebuild old e621 CDN URLs when needed."""
    explicit = _normalize_url(file_payload.get("url"))
    if explicit:
        return explicit
    digest = _valid_md5(file_payload.get("md5"))
    extension = _valid_extension(file_payload.get("ext"))
    if digest and extension:
        return f"https://static1.e621.net/data/{digest[:2]}/{digest[2:4]}/{digest}.{extension}"
    return _normalize_url(sample_payload.get("url"))


def _normalize_url(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    return "https:" + value if value.startswith("//") else value


def _valid_extension(value) -> str | None:
    value = str(value or "").strip().lower().lstrip(".")
    return value if value and len(value) <= 12 and all(char.isalnum() for char in value) else None


def _is_transient_request_error(error) -> bool:
    return isinstance(error, (requests.Timeout, requests.ConnectionError))


def _sleep_backoff(response, attempt: int) -> None:
    value = (getattr(response, "headers", None) or {}).get("Retry-After") if response is not None else None
    delay = 0.0
    if value:
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
    if delay <= 0:
        delay = min(5.0, 0.5 * (2 ** attempt))
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


def _valid_md5(value: str | None) -> str | None:
    value = str(value or "").strip().lower()
    return value if len(value) == 32 and all(c in "0123456789abcdef" for c in value) else None
