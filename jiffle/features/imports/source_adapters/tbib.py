"""TBIB source and exact MD5 lookup adapter."""

import time
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from jiffle.features.imports.source_adapters.contracts import SourceMedia
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure


class TbibSourceProvider:
    provider_name = "tbib"
    domains = {"tbib.org", "www.tbib.org"}
    api_hosts = ("tbib.org", "www.tbib.org")
    max_attempts = 3

    def can_handle(self, url: str) -> bool:
        parsed = urlparse(url)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in self.domains
            and _post_id(parsed) is not None
        )

    def fetch(self, url: str) -> SourceMedia:
        parsed = urlparse(url)
        post_id = _post_id(parsed)
        if post_id is None:
            raise SourceProviderFailure(
                "import.invalid_source_url", "The URL is not a TBIB post URL."
            )
        domain = parsed.hostname or "tbib.org"
        post = self._get_post(domain, post_id)
        try:
            directory = str(post["directory"])
            filename = str(post["image"])
            if not directory or not filename:
                raise ValueError("media path missing")
            direct_url = f"https://{domain}/images/{directory}/{filename}"
            raw_parent_id = post.get("parent_id")
            parent_id = (
                str(raw_parent_id)
                if raw_parent_id not in (None, "", 0, "0")
                else None
            )
            tags = tuple(str(post.get("tags", "")).split())
            extension = PurePosixPath(urlparse(direct_url).path).suffix.lower() or ".jpg"
            return SourceMedia(
                canonical_url=_canonical_url(domain, post_id),
                direct_media_url=direct_url,
                provider=self.provider_name,
                remote_id=str(post_id),
                author=str(post.get("owner") or "") or None,
                domain=domain,
                tags=tags,
                file_extension=extension,
                parent_id=parent_id,
                content_md5=_md5(post.get("hash")),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise SourceProviderFailure(
                "import.source_media_missing", "The source has no downloadable media."
            ) from error

    def fetch_metadata(self, url: str) -> SourceMedia:
        return self.fetch(url)

    def search_by_md5(self, digest: str) -> list[dict[str, object]]:
        digest = _validate_md5(digest)
        if digest is None:
            return []
        response = self._get_json(
            "https://tbib.org/index.php",
            params={
                "page": "dapi",
                "s": "post",
                "q": "index",
                "json": 1,
                "limit": 100,
                "tags": f"md5:{digest}",
            },
        )
        posts = response if isinstance(response, list) else [response]
        matches: list[dict[str, object]] = []
        for post in posts:
            if not isinstance(post, dict) or not str(post.get("id", "")).isdigit():
                continue
            post_id = str(post["id"])
            domain = "tbib.org"
            directory = str(post.get("directory") or "")
            filename = str(post.get("image") or "")
            direct_url = (
                f"https://{domain}/images/{directory}/{filename}"
                if directory and filename
                else None
            )
            matches.append(
                _match(
                    provider=self.provider_name,
                    domain=domain,
                    remote_id=post_id,
                    canonical_url=_canonical_url(domain, post_id),
                    direct_media_url=direct_url,
                    author=post.get("owner"),
                    tags=str(post.get("tags") or "").split(),
                    content_md5=_md5(post.get("hash")),
                    width=post.get("width"),
                    height=post.get("height"),
                )
            )
        return matches

    def search_similar(self, image_path):
        return []

    def check_connection(self) -> None:
        self._get_json(
            "https://tbib.org/index.php",
            params={
                "page": "dapi",
                "s": "post",
                "q": "index",
                "json": 1,
                "limit": 1,
            },
        )

    def _get_post(self, domain: str, post_id: str) -> dict[str, object]:
        payload = self._get_json(
            f"https://{domain}/index.php",
            params={
                "page": "dapi",
                "s": "post",
                "q": "index",
                "json": 1,
                "id": post_id,
            },
        )
        posts = payload if isinstance(payload, list) else [payload]
        post = posts[0] if posts else None
        if not isinstance(post, dict):
            raise SourceProviderFailure(
                "import.source_post_not_found", "The TBIB post was not found."
            )
        return post

    @staticmethod
    def _get_json(url: str, params: dict[str, object]) -> object:
        """Load DAPI JSON with transient retries and a host fallback.

        The fallback is only for the API request.  Post and media URLs built
        from the response keep their canonical TBIB host and are not rewritten.
        """
        parsed = urlparse(url)
        hosts = [parsed.hostname] if parsed.hostname in TbibSourceProvider.api_hosts else [None]
        if parsed.hostname in TbibSourceProvider.api_hosts:
            hosts.append(next(host for host in TbibSourceProvider.api_hosts if host != parsed.hostname))
        last_error: Exception | None = None
        for host in hosts:
            request_url = url
            if host:
                request_url = parsed._replace(netloc=host).geturl()
            for attempt in range(TbibSourceProvider.max_attempts):
                try:
                    response = requests.get(
                        request_url,
                        params=params,
                        headers={"User-Agent": "Jiffle/2.0", "Accept": "application/json"},
                        timeout=15,
                    )
                    status = getattr(response, "status_code", None)
                    try:
                        status = int(status)
                    except (TypeError, ValueError):
                        status = None
                    if status in (401, 403):
                        raise SourceProviderFailure(
                            "import.provider_auth_required",
                            "TBIB requires authorization for this request.",
                        )
                    if status is not None and 500 <= status <= 599:
                        last_error = requests.HTTPError(f"TBIB returned HTTP {status}")
                        if attempt + 1 < TbibSourceProvider.max_attempts:
                            time.sleep(0.05 * (attempt + 1))
                            continue
                        break
                    response.raise_for_status()
                    return response.json()
                except SourceProviderFailure:
                    raise
                except (requests.Timeout, requests.ConnectionError) as error:
                    last_error = error
                    if attempt + 1 < TbibSourceProvider.max_attempts:
                        time.sleep(0.05 * (attempt + 1))
                        continue
                    break
                except requests.RequestException as error:
                    raise SourceProviderFailure(
                        "import.provider_unavailable", "TBIB metadata could not be loaded."
                    ) from error
                except (ValueError, TypeError) as error:
                    raise SourceProviderFailure(
                        "import.provider_unavailable", "TBIB metadata could not be loaded."
                    ) from error
        raise SourceProviderFailure(
            "import.provider_unavailable", "TBIB metadata could not be loaded."
        ) from last_error


def _post_id(parsed) -> str | None:
    if parsed.path.rstrip("/") != "/index.php":
        return None
    value = parse_qs(parsed.query).get("id", [None])[0]
    return value if value and str(value).isdigit() else None


def _canonical_url(domain: str, post_id: str) -> str:
    return "https://" + domain + "/index.php?" + urlencode(
        {"page": "post", "s": "view", "id": post_id}
    )


def _md5(value) -> str | None:
    value = str(value or "").lower()
    return value if _validate_md5(value) else None


def _validate_md5(value: str | None) -> str | None:
    value = str(value or "").strip().lower()
    return value if len(value) == 32 and all(c in "0123456789abcdef" for c in value) else None


def _match(**values) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}
