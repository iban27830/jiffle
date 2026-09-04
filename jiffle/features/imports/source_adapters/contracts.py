from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SourceMedia:
    canonical_url: str
    direct_media_url: str | None
    provider: str
    remote_id: str
    author: str | None
    domain: str
    tags: tuple[str, ...]
    file_extension: str
    character_tags: tuple[str, ...] = ()
    parent_id: str | None = None
    content_md5: str | None = None

    @property
    def characters(self) -> tuple[str, ...]:
        return self.character_tags


@dataclass(frozen=True)
class SourceMatch:
    """A source candidate returned by exact or perceptual lookup."""

    provider: str
    canonical_url: str
    direct_media_url: str | None = None
    remote_id: str | None = None
    author: str | None = None
    domain: str | None = None
    tags: tuple[str, ...] = ()
    content_md5: str | None = None
    match_method: str = "exact"
    confidence: float = 100.0
    preview_url: str | None = None
    width: int | None = None
    height: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "canonical_url": self.canonical_url,
            "direct_media_url": self.direct_media_url,
            "remote_id": self.remote_id,
            "author": self.author,
            "domain": self.domain,
            "tags": list(self.tags),
            "content_md5": self.content_md5,
            "match_method": self.match_method,
            "confidence": self.confidence,
            "preview_url": self.preview_url,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class SetPostIssue:
    """A post that was listed by a source set but cannot be imported."""

    remote_id: str
    canonical_url: str
    code: str
    message: str


@dataclass(frozen=True)
class SourceSet:
    """Metadata and the ordered, downloadable contents of a remote set."""

    canonical_url: str
    provider: str
    remote_id: str
    name: str
    shortname: str
    post_ids: tuple[str | int, ...]
    posts: tuple[SourceMedia, ...]
    issues: tuple[SetPostIssue, ...] = ()

    @property
    def set_id(self) -> str:
        return self.remote_id

    @property
    def short_name(self) -> str:
        return self.shortname


class SourceProvider(Protocol):
    def can_handle(self, url: str) -> bool: ...

    def fetch(self, url: str) -> SourceMedia: ...

    def fetch_metadata(self, url: str) -> SourceMedia: ...

    def search_by_md5(self, digest: str) -> list[dict[str, object]]: ...

    def search_similar(self, image_path: Path) -> list[dict[str, object]]: ...

    def can_handle_set(self, url: str) -> bool: ...

    def fetch_set(self, url: str) -> SourceSet: ...


class SourceSetProvider(Protocol):
    def can_handle_set(self, url: str) -> bool: ...

    def fetch_set(self, url: str) -> SourceSet: ...


class MediaDownloader(Protocol):
    def download(self, url: str, destination: Path, referer: str | None = None) -> None: ...
