from pathlib import PurePosixPath
from urllib.parse import urlparse

import requests

from jiffle.features.imports.source_adapters.contracts import SetPostIssue, SourceMedia, SourceSet
from jiffle.features.imports.source_adapters.reverse_search import (
    REVERSE_SEARCH_USER_AGENT,
    iqdb_query_matches,
    reverse_preview_bytes,
)
from jiffle.features.imports.source_adapters.platform_tags import add_platform_tags


class SourceProviderFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class DanbooruSourceProvider:
    provider_name = "danbooru"
    supports_reverse_search = True
    # A Danbooru account raises the tag/search limits and its reverse-search
    # endpoint is authenticated-only.
    credentials_improve_results = True
    reverse_search_requires_auth = True
    domains = {"danbooru.donmai.us", "safebooru.donmai.us"}
    page_limit = 200

    def __init__(self, login: str | None = None, api_key: str | None = None):
        self.login = login
        self.api_key = api_key

    def reverse_search_readiness(self):
        """Return ``(ready, missing_fields)`` for the authenticated reverse search."""
        missing = []
        if not self.login:
            missing.append("login")
        if not self.api_key:
            missing.append("api_key")
        return (not missing), missing

    def can_handle(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.hostname in self.domains

    def can_handle_set(self, url: str) -> bool:
        return self.can_handle(url) and parse_pool_url(url) is not None

    def fetch(self, url: str) -> SourceMedia:
        return self._fetch_post(url, allow_missing=False)

    def fetch_metadata(self, url: str) -> SourceMedia:
        return self._fetch_post(url, allow_missing=True)

    def metadata_md5(self, url: str) -> str | None:
        return self.fetch_metadata(url).content_md5

    def _fetch_post(self, url: str, allow_missing: bool = False) -> SourceMedia:
        parsed = urlparse(url)
        post_id = _post_id(parsed.path)
        if post_id is None:
            raise SourceProviderFailure(
                "import.invalid_source_url", "The URL is not a Danbooru post URL."
            )
        domain = parsed.hostname or "danbooru.donmai.us"
        api_url = f"https://{domain}/posts/{post_id}.json"
        parameters = {}
        if self.login and self.api_key:
            parameters = {"login": self.login, "api_key": self.api_key}
        try:
            response = requests.get(
                api_url,
                params=parameters,
                headers={"User-Agent": "Jiffle/2.0", "Accept": "application/json"},
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as error:
            failure = _http_auth_failure(error, "Danbooru")
            if failure:
                raise failure from error
            raise SourceProviderFailure(
                "import.provider_unavailable", "Danbooru metadata could not be loaded."
            ) from error
        except (requests.RequestException, ValueError) as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "Danbooru metadata could not be loaded."
            ) from error
        if not isinstance(payload, dict):
            raise SourceProviderFailure(
                "import.source_media_missing", "The source has no metadata."
            )
        return self._post_to_source(payload, domain, str(post_id), allow_missing=allow_missing)

    def _post_to_source(
        self, payload: dict, domain: str, post_id: str, allow_missing: bool = False
    ) -> SourceMedia:
        direct_url = payload.get("file_url") or payload.get("large_file_url")
        if not isinstance(direct_url, str) or not direct_url:
            direct_url = None
        if not direct_url and not allow_missing:
            raise SourceProviderFailure(
                "import.source_media_missing", "The source has no downloadable media."
            )
        if direct_url and direct_url.startswith("//"):
            direct_url = "https:" + direct_url
        tags = add_platform_tags(
            tuple(filter(None, str(payload.get("tag_string", "")).split())),
            self.provider_name, domain, payload.get("source"),
        )
        character_tags = tuple(filter(None, str(payload.get("tag_string_character", "")).split()))
        raw_parent_id = payload.get("parent_id")
        parent_id = str(raw_parent_id) if raw_parent_id not in (None, "", 0, "0") else None
        artists = str(payload.get("tag_string_artist", "")).split()
        extension = PurePosixPath(urlparse(direct_url or "").path).suffix.lower()
        if not extension:
            file_ext = str(payload.get("file_ext") or "").strip().lower().lstrip(".")
            extension = f".{file_ext}" if file_ext and file_ext.isalnum() else ".jpg"
        return SourceMedia(
            canonical_url=f"https://{domain}/posts/{post_id}",
            direct_media_url=direct_url,
            provider=self.provider_name,
            remote_id=str(post_id),
            author=", ".join(artists) if artists else None,
            domain=domain,
            tags=tags,
            file_extension=extension,
            character_tags=character_tags,
            parent_id=parent_id,
            content_md5=_valid_md5(payload.get("md5")),
        )

    def fetch_set(self, url: str) -> SourceSet:
        parsed = parse_pool_url(url)
        if parsed is None:
            raise SourceProviderFailure(
                "import.invalid_source_url", "The URL is not a Danbooru pool URL."
            )
        domain, pool_id = parsed
        auth_params = (
            {"login": self.login, "api_key": self.api_key}
            if self.login and self.api_key
            else {}
        )
        try:
            response = requests.get(
                f"https://{domain}/pools/{pool_id}.json",
                params=auth_params,
                headers={"User-Agent": "Jiffle/2.0", "Accept": "application/json"},
                timeout=15,
            )
            if getattr(response, "status_code", None) == 404:
                raise SourceProviderFailure(
                    "import.danbooru_pool_not_found", "The Danbooru pool was not found."
                )
            response.raise_for_status()
            metadata = response.json()
        except SourceProviderFailure:
            raise
        except requests.HTTPError as error:
            failure = _http_auth_failure(error, "Danbooru")
            if failure:
                raise failure from error
            if getattr(getattr(error, "response", None), "status_code", None) == 404:
                raise SourceProviderFailure(
                    "import.danbooru_pool_not_found", "The Danbooru pool was not found."
                ) from error
            raise SourceProviderFailure(
                "import.provider_unavailable", "Danbooru pool metadata could not be loaded."
            ) from error
        except (requests.RequestException, ValueError) as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "Danbooru pool metadata could not be loaded."
            ) from error
        if not isinstance(metadata, dict):
            raise SourceProviderFailure(
                "import.danbooru_pool_invalid", "Danbooru returned invalid pool metadata."
            )
        raw_ids = metadata.get("post_ids") or []
        if not isinstance(raw_ids, (list, tuple)):
            raise SourceProviderFailure(
                "import.danbooru_pool_invalid", "Danbooru returned invalid pool post IDs."
            )
        post_ids = tuple(str(value) for value in raw_ids if str(value).isdigit())
        if not post_ids:
            raise SourceProviderFailure(
                "import.danbooru_pool_empty", "The Danbooru pool is empty."
            )
        raw_name = str(metadata.get("name") or pool_id)
        shortname = raw_name
        name = raw_name.replace("_", " ").strip() or raw_name

        posts_by_id: dict[str, dict] = {}
        page = 1
        while len(posts_by_id) < len(set(post_ids)):
            try:
                page_response = requests.get(
                    f"https://{domain}/posts.json",
                    params={
                        **auth_params,
                        "tags": f"pool:{pool_id} status:any",
                        "limit": self.page_limit,
                        "page": page,
                    },
                    headers={"User-Agent": "Jiffle/2.0", "Accept": "application/json"},
                    timeout=15,
                )
                page_response.raise_for_status()
                raw_posts = page_response.json()
            except requests.HTTPError as error:
                failure = _http_auth_failure(error, "Danbooru")
                if failure:
                    raise failure from error
                raise SourceProviderFailure(
                    "import.provider_unavailable", "Danbooru pool posts could not be loaded."
                ) from error
            except (requests.RequestException, ValueError) as error:
                raise SourceProviderFailure(
                    "import.provider_unavailable", "Danbooru pool posts could not be loaded."
                ) from error
            if not isinstance(raw_posts, list):
                raise SourceProviderFailure(
                    "import.danbooru_pool_invalid", "Danbooru returned invalid pool posts."
                )
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
                try:
                    source = self._fetch_post(canonical, allow_missing=True)
                    if source.direct_media_url or source.content_md5:
                        posts.append(source)
                    else:
                        issues.append(
                            SetPostIssue(
                                post_id,
                                canonical,
                                "import.source_media_missing",
                                "The post has no downloadable media.",
                            )
                        )
                except SourceProviderFailure:
                    issues.append(
                        SetPostIssue(
                            post_id,
                            canonical,
                            "import.source_post_unavailable",
                            "The post was not returned by Danbooru while loading the pool.",
                        )
                    )
                continue
            source = self._post_to_source(post, domain, post_id, allow_missing=True)
            if source.direct_media_url or source.content_md5:
                posts.append(source)
            else:
                issues.append(
                    SetPostIssue(
                        post_id,
                        canonical,
                        "import.source_media_missing",
                        "The post has no downloadable media.",
                    )
                )
        return SourceSet(
            canonical_url=f"https://{domain}/pools/{pool_id}",
            provider=self.provider_name,
            remote_id=pool_id,
            name=name,
            shortname=shortname,
            post_ids=post_ids,
            posts=tuple(posts),
            issues=tuple(issues),
        )

    def search_by_md5(self, digest: str) -> list[dict[str, object]]:
        digest = _valid_md5(digest)
        if digest is None:
            return []
        parameters = {"tags": f"md5:{digest}", "limit": 100}
        if self.login and self.api_key:
            parameters.update({"login": self.login, "api_key": self.api_key})
        try:
            response = requests.get(
                "https://danbooru.donmai.us/posts.json",
                params=parameters,
                headers={"User-Agent": "Jiffle/2.0", "Accept": "application/json"},
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as error:
            failure = _http_auth_failure(error, "Danbooru")
            if failure:
                raise failure from error
            raise SourceProviderFailure(
                "import.provider_unavailable", "Danbooru search could not be loaded."
            ) from error
        except (requests.RequestException, ValueError) as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "Danbooru search could not be loaded."
            ) from error
        matches: list[dict[str, object]] = []
        for post in payload:
            if not isinstance(post, dict) or not str(post.get("id", "")).isdigit():
                continue
            direct_url = post.get("file_url") or post.get("large_file_url")
            if isinstance(direct_url, str) and direct_url.startswith("//"):
                direct_url = "https:" + direct_url
            character_tags = list(
                filter(None, str(post.get("tag_string_character", "")).split())
            )
            raw_parent_id = post.get("parent_id")
            parent_id = (
                str(raw_parent_id)
                if raw_parent_id not in (None, "", 0, "0")
                else None
            )
            artists = str(post.get("tag_string_artist", "")).split()
            matches.append({
                "provider": self.provider_name,
                "domain": "danbooru.donmai.us",
                "remote_id": str(post["id"]),
                "canonical_url": f"https://danbooru.donmai.us/posts/{post['id']}",
                "direct_media_url": direct_url,
                "author": ", ".join(artists) if artists else None,
                "tags": list(add_platform_tags(
                    tuple(filter(None, str(post.get("tag_string", "")).split())),
                    self.provider_name, "danbooru.donmai.us", post.get("source"),
                )),
                "character_tags": character_tags,
                "parent_id": parent_id,
                "content_md5": _valid_md5(post.get("md5")),
                "width": post.get("image_width"),
                "height": post.get("image_height"),
                "deleted": bool(post.get("is_deleted")),
            })
        return matches

    def search_similar(self, image_path):
        """Reverse-search Danbooru through its IQDB endpoint.

        Danbooru requires an authenticated account for this endpoint, so the
        lookup only runs when a login and API key are configured.
        """
        if not (self.login and self.api_key):
            return []
        preview = reverse_preview_bytes(image_path)
        if preview is None:
            return []
        try:
            response = requests.post(
                "https://danbooru.donmai.us/iqdb_queries.json",
                params={"login": self.login, "api_key": self.api_key},
                files={"search[file]": ("jiffle-preview.jpg", preview, "image/jpeg")},
                headers={
                    "User-Agent": REVERSE_SEARCH_USER_AGENT,
                    "Accept": "application/json",
                },
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as error:
            failure = _http_auth_failure(error, "Danbooru")
            if failure:
                raise failure from error
            raise SourceProviderFailure(
                "import.provider_unavailable",
                "The Danbooru reverse search could not be loaded.",
            ) from error
        except (requests.RequestException, ValueError) as error:
            raise SourceProviderFailure(
                "import.provider_unavailable",
                "The Danbooru reverse search could not be loaded.",
            ) from error
        return iqdb_query_matches(
            payload, self.provider_name, "danbooru.donmai.us",
            "https://danbooru.donmai.us/posts",
        )

    def check_connection(self) -> None:
        parameters = {}
        if self.login and self.api_key:
            parameters = {"login": self.login, "api_key": self.api_key}
        response = requests.get(
            "https://danbooru.donmai.us/posts.json",
            params={**parameters, "limit": 1},
            headers={"User-Agent": "Jiffle/2.0", "Accept": "application/json"},
            timeout=15,
        )
        response.raise_for_status()


def parse_pool_url(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in DanbooruSourceProvider.domains:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "pools":
        return None
    pool_id = parts[1][:-5] if parts[1].endswith(".json") else parts[1]
    return (parsed.hostname, pool_id) if pool_id.isdigit() else None


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


def _http_auth_failure(error, provider_name: str):
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status == 401:
        return SourceProviderFailure(
            "import.provider_auth_required", f"{provider_name} credentials were rejected."
        )
    if status == 403:
        return SourceProviderFailure(
            "import.provider_access_denied", f"{provider_name} denied access to this resource."
        )
    return None
