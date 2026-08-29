from pathlib import PurePosixPath
from urllib.parse import urlparse

import requests

from jiffle.features.imports.source_adapters.contracts import SourceMedia
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure


class E621SourceProvider:
    provider_name = "e621"
    domains = {"e621.net", "e926.net"}

    def __init__(self, login=None, api_key=None):
        self.login = login
        self.api_key = api_key

    def can_handle(self, url):
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.hostname in self.domains

    def fetch(self, url):
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[0] != "posts" or not parts[1].isdigit():
            raise SourceProviderFailure("import.invalid_source_url", "The URL is not an e621 post URL.")
        post_id = parts[1]
        domain = parsed.hostname or "e621.net"
        headers = {"User-Agent": f"Jiffle/2.0 (by {self.login or 'local-user'})"}
        auth = (self.login, self.api_key) if self.login and self.api_key else None
        try:
            response = requests.get(
                f"https://{domain}/posts/{post_id}.json",
                headers=headers, auth=auth, timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
            post = payload["post"] if isinstance(payload, dict) else payload[0]
            direct_url = post["file"]["url"] or post["sample"]["url"]
        except (requests.RequestException, KeyError, TypeError, ValueError) as error:
            raise SourceProviderFailure("import.provider_unavailable", "e621 metadata could not be loaded.") from error
        artists = post.get("tags", {}).get("artist", [])
        tags = tuple(tag for group in post.get("tags", {}).values() for tag in group)
        return SourceMedia(
            canonical_url=f"https://{domain}/posts/{post_id}", direct_media_url=direct_url,
            provider=self.provider_name, remote_id=post_id,
            author=", ".join(artists) if artists else None, domain=domain,
            tags=tags, file_extension=PurePosixPath(urlparse(direct_url).path).suffix or ".jpg",
        )

    def check_connection(self):
        headers = {"User-Agent": f"Jiffle/2.0 (by {self.login or 'local-user'})"}
        auth = (self.login, self.api_key) if self.login and self.api_key else None
        response = requests.get("https://e621.net/posts.json", params={"limit": 1}, headers=headers, auth=auth, timeout=15)
        response.raise_for_status()
