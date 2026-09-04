from pathlib import PurePosixPath
from urllib.parse import urlparse

import requests

from jiffle.features.imports.source_adapters.contracts import SourceMedia


class SourceProviderFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class DanbooruSourceProvider:
    provider_name = "danbooru"
    domains = {"danbooru.donmai.us", "safebooru.donmai.us"}

    def __init__(self, login: str | None = None, api_key: str | None = None):
        self.login = login
        self.api_key = api_key

    def can_handle(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.hostname in self.domains

    def fetch(self, url: str) -> SourceMedia:
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
        except (requests.RequestException, ValueError) as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "Danbooru metadata could not be loaded."
            ) from error
        direct_url = payload.get("file_url") or payload.get("large_file_url")
        if not isinstance(direct_url, str) or not direct_url:
            raise SourceProviderFailure(
                "import.source_media_missing", "The source has no downloadable media."
            )
        if direct_url.startswith("//"):
            direct_url = "https:" + direct_url
        tags = tuple(filter(None, str(payload.get("tag_string", "")).split()))
        character_tags = tuple(filter(None, str(payload.get("tag_string_character", "")).split()))
        raw_parent_id = payload.get("parent_id")
        parent_id = str(raw_parent_id) if raw_parent_id not in (None, "", 0, "0") else None
        artists = str(payload.get("tag_string_artist", "")).split()
        extension = PurePosixPath(urlparse(direct_url).path).suffix.lower() or ".jpg"
        return SourceMedia(
            canonical_url=f"https://{domain}/posts/{post_id}",
            direct_media_url=direct_url,
            provider=self.provider_name,
            remote_id=str(post_id),
            author=artists[0] if artists else None,
            domain=domain,
            tags=tags,
            file_extension=extension,
            character_tags=character_tags, parent_id=parent_id,
            content_md5=_valid_md5(payload.get("md5")),
        )

    def fetch_metadata(self, url: str) -> SourceMedia:
        return self.fetch(url)

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
        except (requests.RequestException, ValueError) as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "Danbooru search could not be loaded."
            ) from error
        return [
            {
                "provider": self.provider_name,
                "domain": "danbooru.donmai.us",
                "remote_id": str(post["id"]),
                "canonical_url": f"https://danbooru.donmai.us/posts/{post['id']}",
                "direct_media_url": post.get("file_url") or post.get("large_file_url"),
                "author": str(post.get("tag_string_artist", "")).split()[0]
                if str(post.get("tag_string_artist", "")).split()
                else None,
                "tags": str(post.get("tag_string", "")).split(),
                "content_md5": _valid_md5(post.get("md5")),
                "width": post.get("image_width"),
                "height": post.get("image_height"),
                "deleted": bool(post.get("is_deleted")),
            }
            for post in payload
            if isinstance(post, dict) and str(post.get("id", "")).isdigit()
        ]

    def search_similar(self, image_path):
        return []

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


def _post_id(path: str) -> int | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "posts" and parts[1].isdigit():
        return int(parts[1])
    return None


def _valid_md5(value: str | None) -> str | None:
    value = str(value or "").strip().lower()
    return value if len(value) == 32 and all(c in "0123456789abcdef" for c in value) else None
