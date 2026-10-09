"""Pawchive source and exact SHA-256 file lookup adapter.

Pawchive (https://pawchive.pw) is a public archive of Patreon, Pixiv Fanbox
and Discord posts.  A post URL such as
``https://pawchive.pw/patreon/user/187748456/post/171899046`` maps to the
documented JSON API under ``/api/v1``.

Unlike the boorus, Pawchive does not index posts by MD5.  Files are stored
under their SHA-256 and the site exposes a *Hash Lookup* (``/search_hash``)
that answers the same question as an exact copy search: give it a SHA-256 and
it returns every post that holds those exact bytes.  The provider therefore
declares ``hash_algorithm = "sha256"`` so the import workflow computes a
SHA-256 for the file it already fingerprinted (the local ``inspect_media``
step), instead of the MD5 used by the boorus.

Pawchive has no perceptual reverse-image search, so ``search_similar`` stays
empty and ``supports_reverse_search`` is ``False``.
"""

from pathlib import PurePosixPath
import time
from urllib.parse import urlparse

import requests

from jiffle.features.imports.source_adapters.contracts import SourceMedia
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure


API_BASE = "https://pawchive.pw/api/v1"
SITE_HOST = "pawchive.pw"
FILE_HOST = "file.pawchive.pw"

# The archive stores every file under ``/data`` on a numbered CDN node
# (``n<node>.pawchive.pw``); older files that predate the node split live on
# the shared ``file.pawchive.pw`` host.
MEDIA_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".webp", ".bmp",
    ".tif", ".tiff", ".avif", ".apng", ".mp4", ".webm", ".mov", ".m4v", ".mkv",
})


class PawchiveSourceProvider:
    provider_name = "pawchive"
    # Pawchive has no perceptual / reverse-image search, only the SHA-256 hash
    # lookup that backs the exact "search by file" copy check.
    supports_reverse_search = False
    # The import workflow reads ``hash_algorithm`` to know which fingerprint to
    # hand to ``search_by_md5``; every booru keeps the default "md5".
    hash_algorithm = "sha256"
    domains = {SITE_HOST, "www." + SITE_HOST}
    max_attempts = 3

    def can_handle(self, url: str) -> bool:
        parsed = urlparse(url)
        return (
            parsed.scheme in {"http", "https"}
            and (parsed.hostname or "").lower() in self.domains
            and _post_target(parsed) is not None
        )

    def fetch(self, url: str) -> SourceMedia:
        parsed = urlparse(url)
        target = _post_target(parsed)
        if target is None:
            raise SourceProviderFailure(
                "import.invalid_source_url", "The URL is not a Pawchive post URL."
            )
        service, creator_id, post_id = target
        payload = self._get_json(
            f"{API_BASE}/{service}/user/{creator_id}/post/{post_id}"
        )
        if not isinstance(payload, dict) or not _text(payload.get("id")):
            raise SourceProviderFailure(
                "import.source_post_not_found", "The Pawchive post was not found."
            )
        entry = _first_downloadable(payload)
        if entry is None:
            raise SourceProviderFailure(
                "import.source_media_missing",
                "The source has no downloadable media.",
            )
        direct_url = _media_url(entry)
        if not direct_url:
            raise SourceProviderFailure(
                "import.source_media_missing",
                "The source has no downloadable media.",
            )
        return SourceMedia(
            canonical_url=_canonical_url(service, creator_id, post_id),
            direct_media_url=direct_url,
            provider=self.provider_name,
            remote_id=str(post_id),
            author=self._creator_name(service, creator_id),
            domain=SITE_HOST,
            tags=(),
            file_extension=PurePosixPath(urlparse(direct_url).path).suffix.lower() or ".jpg",
        )

    def fetch_metadata(self, url: str) -> SourceMedia:
        return self.fetch(url)

    def search_by_md5(self, digest: str) -> list[dict[str, object]]:
        """Look a file up by its SHA-256 (the argument is not an MD5)."""
        digest = _valid_sha256(digest)
        if digest is None:
            return []
        payload = self._get_json(f"{API_BASE}/search_hash/{digest}")
        if not isinstance(payload, dict):
            return []
        matches: list[dict[str, object]] = []
        for post in payload.get("posts") or []:
            match = _match_from_post(post, digest)
            if match is not None:
                matches.append(match)
        for post in payload.get("discord_posts") or []:
            match = _match_from_discord_post(post, digest)
            if match is not None:
                matches.append(match)
        return matches

    def search_similar(self, image_path):
        return []

    def check_connection(self) -> None:
        payload = self._get_json(f"{API_BASE}/posts")
        if not isinstance(payload, list):
            raise SourceProviderFailure(
                "import.provider_unavailable", "Pawchive did not return a post list."
            )

    def _creator_name(self, service: str, creator_id: str) -> str | None:
        """Best-effort creator name; a failed profile lookup is not fatal."""
        try:
            payload = self._get_json(
                f"{API_BASE}/{service}/user/{creator_id}/profile"
            )
        except SourceProviderFailure:
            return None
        if isinstance(payload, dict):
            return _text(payload.get("name")) or None
        return None

    def _get_json(self, url: str) -> object:
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                response = requests.get(
                    url,
                    headers={"User-Agent": "Jiffle/2.0", "Accept": "application/json"},
                    timeout=15,
                )
                status = getattr(response, "status_code", None)
                try:
                    status = int(status)
                except (TypeError, ValueError):
                    status = None
                if status == 404:
                    return None
                if status in (401, 403):
                    raise SourceProviderFailure(
                        "import.provider_access_denied",
                        "Pawchive denied access to this resource.",
                    )
                if status is not None and (status == 429 or 500 <= status <= 599):
                    last_error = requests.HTTPError(f"Pawchive returned HTTP {status}")
                    if attempt + 1 < self.max_attempts:
                        time.sleep(min(2.0, 0.5 * (attempt + 1)))
                        continue
                    break
                response.raise_for_status()
                text = str(getattr(response, "text", "") or "")
                if not text.strip():
                    return None
                return response.json()
            except SourceProviderFailure:
                raise
            except (requests.Timeout, requests.ConnectionError) as error:
                last_error = error
                if attempt + 1 < self.max_attempts:
                    time.sleep(min(2.0, 0.5 * (attempt + 1)))
                    continue
                break
            except (requests.RequestException, ValueError, TypeError) as error:
                raise SourceProviderFailure(
                    "import.provider_unavailable",
                    "Pawchive metadata could not be loaded.",
                ) from error
        raise SourceProviderFailure(
            "import.provider_unavailable",
            "Pawchive metadata could not be loaded.",
        ) from last_error


def _post_target(parsed) -> tuple[str, str, str] | None:
    """Return ``(service, creator_id, post_id)`` for a post URL, or ``None``."""
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 5 or segments[3] != "post":
        return None
    service, kind, creator_id, _marker, post_id = segments
    if kind not in {"user", "server"}:
        return None
    if not all(_text(value) for value in (service, creator_id, post_id)):
        return None
    return service, creator_id, post_id


def _text(value: object) -> str:
    return str(value if value is not None else "").strip()


def _canonical_url(service: str, creator_id: str, post_id: str) -> str:
    return f"https://{SITE_HOST}/{service}/user/{creator_id}/post/{post_id}"


def _media_url(entry: object) -> str | None:
    if not isinstance(entry, dict):
        return None
    path = _text(entry.get("path"))
    if not path:
        return None
    node = entry.get("node")
    host = f"n{node}.{SITE_HOST}" if node not in (None, "", 0, "0") else FILE_HOST
    if not path.startswith("/"):
        path = "/" + path
    return f"https://{host}/data{path}"


def _path_hash(entry: object) -> str | None:
    """Pawchive names stored files by their SHA-256, so the stem is the hash."""
    if not isinstance(entry, dict):
        return None
    stem = PurePosixPath(_text(entry.get("path"))).stem.lower()
    return _valid_sha256(stem)


def _extension(entry: object) -> str:
    return PurePosixPath(_text(entry.get("path"))).suffix.lower() if isinstance(entry, dict) else ""


def _is_downloadable(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("preview_only") or entry.get("deferred"):
        return False
    return bool(_text(entry.get("path")))


def _entries(post: object) -> list[object]:
    """The primary file followed by every attachment, in archive order."""
    if not isinstance(post, dict):
        return []
    entries: list[object] = [post.get("file")]
    attachments = post.get("attachments")
    if isinstance(attachments, list):
        entries.extend(attachments)
    return [entry for entry in entries if isinstance(entry, dict)]


def _entry_for_hash(post: object, digest: str) -> dict[str, object] | None:
    for entry in _entries(post):
        if _path_hash(entry) == digest and _is_downloadable(entry):
            return entry
    return None


def _first_downloadable(post: object) -> dict[str, object] | None:
    """The first downloadable media file of a post, preferring real media."""
    fallback: dict[str, object] | None = None
    for entry in _entries(post):
        if not _is_downloadable(entry):
            continue
        if _extension(entry) in MEDIA_EXTENSIONS:
            return entry
        fallback = fallback or entry
    return fallback


def _match_from_post(post: object, digest: str) -> dict[str, object] | None:
    if not isinstance(post, dict):
        return None
    post_id = _text(post.get("id"))
    service = _text(post.get("service"))
    user = _text(post.get("user"))
    if not (post_id and service and user):
        return None
    # Prefer the exact file the hash points at; a post can hold many files.
    entry = _entry_for_hash(post, digest) or _first_downloadable(post)
    match: dict[str, object] = {
        "provider": PawchiveSourceProvider.provider_name,
        "domain": SITE_HOST,
        "remote_id": post_id,
        "canonical_url": _canonical_url(service, user, post_id),
        "direct_media_url": _media_url(entry) if entry else None,
    }
    title = _text(post.get("title"))
    if title:
        match["title"] = title
    return match


def _match_from_discord_post(post: object, digest: str) -> dict[str, object] | None:
    if not isinstance(post, dict):
        return None
    post_id = _text(post.get("id"))
    server = _text(post.get("server"))
    channel = _text(post.get("channel"))
    if not (post_id and server and channel):
        return None
    entry = _entry_for_hash(post, digest) or _first_downloadable(post)
    return {
        "provider": PawchiveSourceProvider.provider_name,
        "domain": SITE_HOST,
        "remote_id": post_id,
        "canonical_url": (
            f"https://{SITE_HOST}/discord/server/{server}/channel/{channel}/post/{post_id}"
        ),
        "direct_media_url": _media_url(entry) if entry else None,
    }


def _valid_sha256(value: object) -> str | None:
    value = _text(value).lower()
    return (
        value
        if len(value) == 64 and all(character in "0123456789abcdef" for character in value)
        else None
    )
