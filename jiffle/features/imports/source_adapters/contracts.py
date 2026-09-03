from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SourceMedia:
    canonical_url: str
    direct_media_url: str
    provider: str
    remote_id: str
    author: str | None
    domain: str
    tags: tuple[str, ...]
    file_extension: str


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

    def can_handle_set(self, url: str) -> bool: ...

    def fetch_set(self, url: str) -> SourceSet: ...


class SourceSetProvider(Protocol):
    def can_handle_set(self, url: str) -> bool: ...

    def fetch_set(self, url: str) -> SourceSet: ...


class MediaDownloader(Protocol):
    def download(self, url: str, destination: Path, referer: str | None = None) -> None: ...
