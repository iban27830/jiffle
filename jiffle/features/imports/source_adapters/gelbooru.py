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
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as error:
            raise SourceProviderFailure("import.provider_unavailable", "Gelbooru metadata could not be loaded.") from error
        return SourceMedia(
            canonical_url=url, direct_media_url=direct_url, provider=self.provider_name,
            remote_id=str(post_id), author=post.get("owner") or None, domain=domain,
            tags=tuple(str(post.get("tags", "")).split()),
            file_extension=PurePosixPath(urlparse(direct_url).path).suffix or ".jpg",
        )

    def check_connection(self):
        params = {"page": "dapi", "s": "post", "q": "index", "limit": 1, "json": 1}
        if self.user_id and self.api_key:
            params.update({"user_id": self.user_id, "api_key": self.api_key})
        response = requests.get("https://gelbooru.com/index.php", params=params, headers={"User-Agent": "Jiffle/2.0"}, timeout=15)
        response.raise_for_status()
