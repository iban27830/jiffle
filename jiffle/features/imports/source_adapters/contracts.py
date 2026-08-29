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


class SourceProvider(Protocol):
    def can_handle(self, url: str) -> bool: ...

    def fetch(self, url: str) -> SourceMedia: ...


class MediaDownloader(Protocol):
    def download(self, url: str, destination: Path, referer: str | None = None) -> None: ...
