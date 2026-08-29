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


def _post_id(path: str) -> int | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "posts" and parts[1].isdigit():
        return int(parts[1])
    return None
