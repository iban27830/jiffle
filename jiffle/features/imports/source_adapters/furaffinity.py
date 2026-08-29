import re
from urllib.parse import urlparse

import requests

from jiffle.features.imports.source_adapters.contracts import SourceMedia
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure


class FurAffinitySourceProvider:
    provider_name = "furaffinity"
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
        except (requests.RequestException, ValueError) as error:
            raise SourceProviderFailure("import.provider_unavailable", "FurAffinity submission could not be loaded.") from error
        direct_url = "https:" + direct.group(1)
        title = re.search(r'<title>.*? by ([^<]+)</title>', response.text, re.IGNORECASE | re.DOTALL)
        tags = tuple(re.findall(r'/search/@keywords/([^/"?]+)', response.text))
        return SourceMedia(
            canonical_url=f"https://www.furaffinity.net/view/{match.group(1)}/",
            direct_media_url=direct_url, provider=self.provider_name,
            remote_id=match.group(1), author=title.group(1).strip() if title else None,
            domain="furaffinity.net", tags=tags,
            file_extension="." + direct_url.rsplit(".", 1)[-1].split("?", 1)[0].lower(),
        )

    def check_connection(self):
        if not self.cookies:
            raise ValueError("FurAffinity cookies are not configured")
        response = requests.get("https://www.furaffinity.net/", cookies=self.cookies, headers={"User-Agent": "Mozilla/5.0 Jiffle/2.0"}, timeout=15)
        response.raise_for_status()
        if "logout" not in response.text.lower() and "log out" not in response.text.lower():
            raise ValueError("FurAffinity cookies were rejected")
