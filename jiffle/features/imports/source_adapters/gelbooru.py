from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlparse

import requests

from jiffle.features.imports.source_adapters.contracts import SourceMedia
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure


class GelbooruSourceProvider:
    provider_name = "gelbooru"
    domains = {"gelbooru.com", "rule34.xxx", "safebooru.org"}

    def __init__(self, user_id=None, api_key=None):
        self.user_id = user_id
        self.api_key = api_key

    def can_handle(self, url):
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.hostname in self.domains

    def fetch(self, url):
        parsed = urlparse(url)
        post_id = parse_qs(parsed.query).get("id", [None])[0]
        if not post_id or not str(post_id).isdigit():
            raise SourceProviderFailure("import.invalid_source_url", "The URL has no valid Gelbooru post ID.")
        domain = parsed.hostname or "gelbooru.com"
        params = {"page": "dapi", "s": "post", "q": "index", "id": post_id, "json": 1}
        if self.user_id and self.api_key:
            params.update({"user_id": self.user_id, "api_key": self.api_key})
        try:
            response = requests.get(f"https://{domain}/index.php", params=params, headers={"User-Agent": "Jiffle/2.0"}, timeout=15)
            response.raise_for_status()
            payload = response.json()
            post = payload["post"][0] if isinstance(payload, dict) else payload[0]
            direct_url = post["file_url"]
        except requests.HTTPError as error:
            failure = _http_auth_failure(error)
            if failure:
                raise failure from error
            raise SourceProviderFailure("import.provider_unavailable", "Gelbooru metadata could not be loaded.") from error
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as error:
            raise SourceProviderFailure("import.provider_unavailable", "Gelbooru metadata could not be loaded.") from error
        raw_parent_id = post.get("parent_id")
        parent_id = str(raw_parent_id) if raw_parent_id not in (None, "", 0, "0") else None
        raw_characters = post.get("character") or post.get("tag_string_character") or ""
        character_tags = tuple(str(raw_characters).split()) if isinstance(raw_characters, str) else tuple(str(value) for value in raw_characters)
        return SourceMedia(
            canonical_url=url, direct_media_url=direct_url, provider=self.provider_name,
            remote_id=str(post_id), author=post.get("owner") or None, domain=domain,
            tags=tuple(str(post.get("tags", "")).split()),
            file_extension=PurePosixPath(urlparse(direct_url).path).suffix or ".jpg",
            character_tags=character_tags, parent_id=parent_id,
            content_md5=_valid_md5(post.get("hash") or post.get("md5")),
        )

    def fetch_metadata(self, url):
        return self.fetch(url)

    def search_by_md5(self, digest: str) -> list[dict[str, object]]:
        digest = _valid_md5(digest)
        if digest is None:
            return []
        params = {
            "page": "dapi", "s": "post", "q": "index", "json": 1,
            "limit": 100, "tags": f"md5:{digest}",
        }
        if self.user_id and self.api_key:
            params.update({"user_id": self.user_id, "api_key": self.api_key})
        try:
            response = requests.get(
                "https://gelbooru.com/index.php",
                params=params,
                headers={"User-Agent": "Jiffle/2.0", "Accept": "application/json"},
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as error:
            failure = _http_auth_failure(error)
            if failure:
                raise failure from error
            raise SourceProviderFailure(
                "import.provider_unavailable", "Gelbooru search could not be loaded."
            ) from error
        except (requests.RequestException, ValueError) as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "Gelbooru search could not be loaded."
            ) from error
        posts = payload if isinstance(payload, list) else [payload]
        matches: list[dict[str, object]] = []
        for post in posts:
            if not isinstance(post, dict) or not str(post.get("id", "")).isdigit():
                continue
            post_id = str(post["id"])
            direct_url = post.get("file_url")
            domain = "gelbooru.com"
            matches.append({
                "provider": self.provider_name,
                "domain": domain,
                "remote_id": post_id,
                "canonical_url": (
                    f"https://{domain}"
                    f"/index.php?page=post&s=view&id={post_id}"
                ),
                "direct_media_url": direct_url,
                "author": post.get("owner") or None,
                "tags": str(post.get("tags", "")).split(),
                "content_md5": _valid_md5(post.get("hash") or post.get("md5")),
                "width": post.get("width"),
                "height": post.get("height"),
                "deleted": bool(str(post.get("status", "")).lower() == "deleted"),
            })
        return matches

    def search_similar(self, image_path):
        return []

    def check_connection(self):
        params = {"page": "dapi", "s": "post", "q": "index", "limit": 1, "json": 1}
        if self.user_id and self.api_key:
            params.update({"user_id": self.user_id, "api_key": self.api_key})
        response = requests.get("https://gelbooru.com/index.php", params=params, headers={"User-Agent": "Jiffle/2.0"}, timeout=15)
        response.raise_for_status()


def _valid_md5(value: str | None) -> str | None:
    value = str(value or "").strip().lower()
    return value if len(value) == 32 and all(c in "0123456789abcdef" for c in value) else None


def _http_auth_failure(error):
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status == 401:
        return SourceProviderFailure(
            "import.provider_auth_required", "Gelbooru credentials were rejected."
        )
    if status == 403:
        return SourceProviderFailure(
            "import.provider_access_denied", "Gelbooru denied access to this resource."
        )
    return None
