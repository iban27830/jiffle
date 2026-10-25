from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlparse

import requests

from jiffle.features.imports.source_adapters.contracts import SourceMedia
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.features.imports.source_adapters.platform_tags import add_platform_tags


class GelbooruSourceProvider:
    provider_name = "gelbooru"
    supports_reverse_search = False
    credentials_improve_results = True
    domains = {"gelbooru.com", "safebooru.org"}

    def __init__(self, user_id=None, api_key=None):
        self.user_id = user_id
        self.api_key = api_key

    def can_handle(self, url):
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.hostname in self.domains

    def fetch(self, url):
        return self._fetch_post(url, allow_missing=False)

    def fetch_metadata(self, url):
        return self._fetch_post(url, allow_missing=True)

    def metadata_md5(self, url: str) -> str | None:
        return self.fetch_metadata(url).content_md5

    def _fetch_post(self, url, allow_missing: bool = False):
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
            payload = _json_payload(response)
            post = _first_post(payload)
        except requests.HTTPError as error:
            failure = _http_auth_failure(error, bool(self.user_id and self.api_key))
            if failure:
                raise failure from error
            raise SourceProviderFailure("import.provider_unavailable", "Gelbooru metadata could not be loaded.") from error
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as error:
            raise SourceProviderFailure("import.provider_unavailable", "Gelbooru metadata could not be loaded.") from error
        if not isinstance(post, dict):
            raise SourceProviderFailure("import.provider_unavailable", "Gelbooru metadata could not be loaded.")
        direct_url = post.get("file_url")
        if not isinstance(direct_url, str) or not direct_url:
            direct_url = None
        if not direct_url and not allow_missing:
            raise SourceProviderFailure("import.source_media_missing", "The source has no downloadable media.")
        raw_parent_id = post.get("parent_id")
        parent_id = str(raw_parent_id) if raw_parent_id not in (None, "", 0, "0") else None
        raw_characters = post.get("character") or post.get("tag_string_character") or ""
        character_tags = tuple(str(raw_characters).split()) if isinstance(raw_characters, str) else tuple(str(value) for value in raw_characters)
        extension = (PurePosixPath(urlparse(direct_url).path).suffix.lower() if direct_url else "") or ".jpg"
        return SourceMedia(
            canonical_url=f"https://{domain}/index.php?page=post&s=view&id={post_id}",
            direct_media_url=direct_url, provider=self.provider_name,
            remote_id=str(post_id), author=post.get("owner") or None, domain=domain,
            tags=add_platform_tags(
                tuple(str(post.get("tags", "")).split()),
                self.provider_name, domain, post.get("source"),
            ),
            file_extension=extension,
            character_tags=character_tags, parent_id=parent_id,
            content_md5=_valid_md5(post.get("hash") or post.get("md5")),
        )

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
            payload = _json_payload(response)
        except requests.HTTPError as error:
            failure = _http_auth_failure(error, bool(self.user_id and self.api_key))
            if failure:
                raise failure from error
            raise SourceProviderFailure(
                "import.provider_unavailable", "Gelbooru search could not be loaded."
            ) from error
        except (requests.RequestException, ValueError) as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "Gelbooru search could not be loaded."
            ) from error
        matches: list[dict[str, object]] = []
        for post in _posts(payload):
            if not isinstance(post, dict) or not str(post.get("id", "")).isdigit():
                continue
            post_id = str(post["id"])
            direct_url = post.get("file_url")
            domain = "gelbooru.com"
            raw_parent_id = post.get("parent_id")
            parent_id = str(raw_parent_id) if raw_parent_id not in (None, "", 0, "0") else None
            raw_characters = post.get("character") or post.get("tag_string_character") or ""
            character_tags = (
                list(str(raw_characters).split())
                if isinstance(raw_characters, str)
                else [str(value) for value in raw_characters]
            )
            match: dict[str, object] = {
                "provider": self.provider_name,
                "domain": domain,
                "remote_id": post_id,
                "canonical_url": (
                    f"https://{domain}"
                    f"/index.php?page=post&s=view&id={post_id}"
                ),
                "direct_media_url": direct_url,
                "author": post.get("owner") or None,
                "tags": list(add_platform_tags(
                    str(post.get("tags", "")).split(),
                    self.provider_name, domain, post.get("source"),
                )),
                "character_tags": character_tags,
                "parent_id": parent_id,
                "content_md5": _valid_md5(post.get("hash") or post.get("md5")),
                "width": _int_or_none(post.get("width")),
                "height": _int_or_none(post.get("height")),
                "deleted": bool(str(post.get("status", "")).lower() == "deleted"),
            }
            preview = post.get("preview_url")
            if isinstance(preview, str) and preview:
                match["preview_url"] = preview
            matches.append(match)
        return matches

    def search_similar(self, image_path):
        return []

    def check_connection(self):
        params = {"page": "dapi", "s": "post", "q": "index", "limit": 1, "json": 1}
        if self.user_id and self.api_key:
            params.update({"user_id": self.user_id, "api_key": self.api_key})
        response = requests.get("https://gelbooru.com/index.php", params=params, headers={"User-Agent": "Jiffle/2.0"}, timeout=15)
        response.raise_for_status()


def _json_payload(response) -> object:
    text = getattr(response, "text", None)
    if isinstance(text, str) and not text.strip():
        return []
    return response.json()


def _posts(payload: object) -> list[dict[str, object]]:
    """Return post dicts from the Gelbooru DAPI JSON shape (list or {"post": [...]})."""
    if isinstance(payload, list):
        return [post for post in payload if isinstance(post, dict)]
    if isinstance(payload, dict):
        posts = payload.get("post")
        if isinstance(posts, list):
            return [post for post in posts if isinstance(post, dict)]
        if isinstance(posts, dict):
            return [posts]
        if str(payload.get("id", "")).isdigit():
            return [payload]
    return []


def _first_post(payload: object) -> dict[str, object] | None:
    posts = _posts(payload)
    return posts[0] if posts else None


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _valid_md5(value: str | None) -> str | None:
    value = str(value or "").strip().lower()
    return value if len(value) == 32 and all(c in "0123456789abcdef" for c in value) else None


def _http_auth_failure(error, has_credentials: bool = True):
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status == 401:
        message = (
            "Gelbooru credentials were rejected."
            if has_credentials
            else "Gelbooru requires a User ID and API key. Add them in Settings \u2192 Sources."
        )
        return SourceProviderFailure("import.provider_auth_required", message)
    if status == 403:
        return SourceProviderFailure(
            "import.provider_access_denied", "Gelbooru denied access to this resource."
        )
    return None
