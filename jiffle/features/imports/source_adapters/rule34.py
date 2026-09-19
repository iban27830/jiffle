"""Rule34.xxx source and exact MD5 lookup adapter.

Rule34.xxx exposes a Gelbooru-compatible DAPI, but unlike Gelbooru it now
requires a personal user ID and API key for every API request, including exact
MD5 lookups.  Both values are created from the site account options page and
are stored in Jiffle settings.  Without them the provider reports that it is
not configured instead of failing the whole import.
"""

from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from jiffle.features.imports.source_adapters.contracts import SourceMedia
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure

API_URL = "https://api.rule34.xxx/index.php"
SITE_URL = "https://rule34.xxx/index.php"
DOMAIN = "rule34.xxx"


class Rule34SourceProvider:
    provider_name = "rule34"
    domains = {"rule34.xxx", "www.rule34.xxx", "api.rule34.xxx"}

    def __init__(self, user_id=None, api_key=None):
        self.user_id = user_id
        self.api_key = api_key

    @property
    def is_configured(self) -> bool:
        """Rule34 answers only authenticated API calls."""
        return bool(self.user_id and self.api_key)

    def can_handle(self, url: str) -> bool:
        parsed = urlparse(url)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in self.domains
            and _post_id(parse_qs(parsed.query)) is not None
        )

    def fetch(self, url: str) -> SourceMedia:
        parsed = urlparse(url)
        post_id = _post_id(parse_qs(parsed.query))
        if post_id is None:
            raise SourceProviderFailure(
                "import.invalid_source_url", "The URL is not a Rule34 post URL."
            )
        payload = self._get_json(
            {"page": "dapi", "s": "post", "q": "index", "id": post_id, "json": 1}
        )
        post = _first_post(payload)
        if post is None:
            raise SourceProviderFailure(
                "import.source_post_not_found", "The Rule34 post was not found."
            )
        return _source_from_post(post, post_id)

    def fetch_metadata(self, url: str) -> SourceMedia:
        return self.fetch(url)

    def search_by_md5(self, digest: str) -> list[dict[str, object]]:
        digest = _valid_md5(digest)
        if digest is None:
            return []
        payload = self._get_json(
            {
                "page": "dapi",
                "s": "post",
                "q": "index",
                "json": 1,
                "limit": 100,
                "tags": f"md5:{digest}",
            }
        )
        matches: list[dict[str, object]] = []
        for post in _posts(payload):
            match = _match_from_post(post)
            if match is not None:
                matches.append(match)
        return matches

    def search_similar(self, image_path):
        return []

    def check_connection(self) -> None:
        self._get_json(
            {"page": "dapi", "s": "post", "q": "index", "json": 1, "limit": 1}
        )

    def _get_json(self, params: dict[str, object]) -> object:
        if not self.is_configured:
            raise SourceProviderFailure(
                "import.provider_auth_required",
                "Rule34 requires a User ID and API key. Add them in Settings \u2192 Sources.",
            )
        request_params = dict(params)
        request_params["user_id"] = self.user_id
        request_params["api_key"] = self.api_key
        try:
            response = requests.get(
                API_URL,
                params=request_params,
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
                "import.provider_unavailable", "Rule34 data could not be loaded."
            ) from error
        except (requests.RequestException, ValueError) as error:
            raise SourceProviderFailure(
                "import.provider_unavailable", "Rule34 data could not be loaded."
            ) from error
        if isinstance(payload, str):
            raise SourceProviderFailure(
                "import.provider_auth_required",
                "Rule34 rejected the saved User ID or API key.",
            )
        return payload


def _post_id(query: dict[str, list[str]]) -> str | None:
    value = query.get("id", [None])[0]
    return value if value and str(value).isdigit() else None


def _canonical_url(post_id: str) -> str:
    return SITE_URL + "?" + urlencode(
        {"page": "post", "s": "view", "id": post_id}
    )


def _media_url(post: dict[str, object]) -> str | None:
    value = post.get("file_url")
    return value if isinstance(value, str) and value else None


def _posts(payload: object) -> list[dict[str, object]]:
    """Return post dicts from the DAPI JSON shape (list or {"post": [...]})."""
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


def _parent_id(post: dict[str, object]) -> str | None:
    raw = post.get("parent_id")
    return str(raw) if raw not in (None, "", 0, "0") else None


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _source_from_post(post: dict[str, object], fallback_id: str) -> SourceMedia:
    post_id = str(post.get("id") or fallback_id)
    direct_url = _media_url(post)
    if not direct_url:
        raise SourceProviderFailure(
            "import.source_media_missing", "The source has no downloadable media."
        )
    return SourceMedia(
        canonical_url=_canonical_url(post_id),
        direct_media_url=direct_url,
        provider=Rule34SourceProvider.provider_name,
        remote_id=post_id,
        author=str(post.get("owner") or "") or None,
        domain=DOMAIN,
        tags=tuple(str(post.get("tags") or "").split()),
        file_extension=PurePosixPath(urlparse(direct_url).path).suffix.lower() or ".jpg",
        parent_id=_parent_id(post),
        content_md5=_valid_md5(post.get("hash") or post.get("md5")),
    )


def _match_from_post(post: dict[str, object]) -> dict[str, object] | None:
    post_id = post.get("id")
    if not str(post_id or "").isdigit():
        return None
    post_id = str(post_id)
    match: dict[str, object] = {
        "provider": Rule34SourceProvider.provider_name,
        "domain": DOMAIN,
        "remote_id": post_id,
        "canonical_url": _canonical_url(post_id),
        "direct_media_url": _media_url(post),
        "author": str(post.get("owner") or "") or None,
        "tags": str(post.get("tags") or "").split(),
        "content_md5": _valid_md5(post.get("hash") or post.get("md5")),
        "width": _int_or_none(post.get("width")),
        "height": _int_or_none(post.get("height")),
        "deleted": str(post.get("status") or "").lower() == "deleted",
    }
    preview = post.get("preview_url")
    if isinstance(preview, str) and preview:
        match["preview_url"] = preview
    return match


def _valid_md5(value: object) -> str | None:
    value = str(value or "").strip().lower()
    return value if len(value) == 32 and all(c in "0123456789abcdef" for c in value) else None


def _http_auth_failure(error):
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status == 401:
        return SourceProviderFailure(
            "import.provider_auth_required", "Rule34 credentials were rejected."
        )
    if status == 403:
        return SourceProviderFailure(
            "import.provider_access_denied", "Rule34 denied access to this resource."
        )
    return None
