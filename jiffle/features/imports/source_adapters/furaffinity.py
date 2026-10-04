import re
from urllib.parse import unquote, urlparse

import requests

from jiffle.features.imports.source_adapters.contracts import SourceMedia
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.features.imports.source_adapters.platform_tags import add_platform_tags


class FurAffinitySourceProvider:
    provider_name = "furaffinity"
    supports_reverse_search = False
    credentials_improve_results = True
    domains = {"furaffinity.net", "www.furaffinity.net"}

    def __init__(self, cookie_a=None, cookie_b=None):
        self.cookie_a = cookie_a
        self.cookie_b = cookie_b

    @property
    def cookies(self):
        return {"a": self.cookie_a, "b": self.cookie_b} if self.cookie_a and self.cookie_b else {}

    def can_handle(self, url):
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.hostname in self.domains

    def fetch(self, url):
        match = re.search(r"/view/(\d+)", urlparse(url).path)
        if not match:
            raise SourceProviderFailure("import.invalid_source_url", "The URL is not a FurAffinity submission URL.")
        if not self.cookies:
            raise SourceProviderFailure("import.provider_auth_required", "FurAffinity cookies are not configured.")
        try:
            response = requests.get(url, cookies=self.cookies, headers={"User-Agent": "Mozilla/5.0 Jiffle/2.0"}, timeout=15)
            response.raise_for_status()
            direct = re.search(r'href="(//(?:d\.furaffinity\.net|d\.facdn\.net)/art/[^"]+)"', response.text)
            if not direct:
                raise ValueError("media link missing")
        except requests.HTTPError as error:
            failure = _http_auth_failure(error)
            if failure:
                raise failure from error
            raise SourceProviderFailure(
                "import.provider_unavailable", "FurAffinity submission could not be loaded."
            ) from error
        except (requests.RequestException, ValueError) as error:
            raise SourceProviderFailure("import.provider_unavailable", "FurAffinity submission could not be loaded.") from error
        direct_url = "https:" + direct.group(1)
        # The page title is "<Artwork> by <artist> -- Fur Affinity [dot] net".
        # A greedy prefix keeps a title that itself contains " by " intact.
        title = re.search(
            r"<title>(?P<title>.*)\s+by\s+(?P<author>.+?)\s+--\s+Fur Affinity",
            response.text,
            re.IGNORECASE | re.DOTALL,
        )
        # FurAffinity links tags as /search/@keywords%20<tag> (the space is
        # sometimes literal), not as a path segment.
        tags = tuple(
            dict.fromkeys(
                unquote(value)
                for value in re.findall(
                    r"/search/@keywords(?:%20|\s+|/)([^/\"'?&<>\s]+)", response.text
                )
            )
        )
        tags = add_platform_tags(tags, self.provider_name, "furaffinity.net")
        return SourceMedia(
            canonical_url=f"https://www.furaffinity.net/view/{match.group(1)}/",
            direct_media_url=direct_url, provider=self.provider_name,
            remote_id=match.group(1),
            author=title.group("author").strip() if title else None,
            domain="furaffinity.net", tags=tags,
            file_extension="." + direct_url.rsplit(".", 1)[-1].split("?", 1)[0].lower(),
        )

    def fetch_metadata(self, url):
        return self.fetch(url)

    def search_similar(self, image_path):
        return []

    def check_connection(self):
        if not self.cookies:
            raise ValueError("FurAffinity cookies are not configured")
        response = requests.get("https://www.furaffinity.net/", cookies=self.cookies, headers={"User-Agent": "Mozilla/5.0 Jiffle/2.0"}, timeout=15)
        response.raise_for_status()
        if "logout" not in response.text.lower() and "log out" not in response.text.lower():
            raise ValueError("FurAffinity cookies were rejected")


def _http_auth_failure(error):
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status == 401:
        return SourceProviderFailure(
            "import.provider_auth_required", "FurAffinity credentials were rejected."
        )
    if status == 403:
        return SourceProviderFailure(
            "import.provider_access_denied", "FurAffinity denied access to this resource."
        )
    return None
