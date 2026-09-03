from dataclasses import dataclass
from enum import StrEnum


class MediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


@dataclass(frozen=True)
class MediaItem:
    id: int
    file_path: str
    media_type: MediaType
    source_url: str | None
    author: str | None
    domain: str | None
    width: int | None
    height: int | None
    file_size: int | None
    content_hash: str | None
    active_revision_id: int | None
    edit_operations: tuple[str, ...]
    created_at: str
    tags: tuple[str, ...]
    character_tags: tuple[str, ...] = ()
    parent_id: str | None = None
    parent_media_id: int | None = None
    remote_id: str | None = None
    parent_url: str | None = None
    family_id: int | None = None
    relatives: tuple[int, ...] = ()

    @property
    def characters(self) -> tuple[str, ...]:
        return self.character_tags


@dataclass(frozen=True)
class LibraryQuery:
    limit: int = 20
    offset: int = 0
    tag: str | None = None
    exclude_tag: str | None = None
    author: str | None = None
    domain: str | None = None
    media_type: MediaType | None = None
    text: str | None = None
    media_id: int | None = None
    tags: tuple[str, ...] = ()
    excluded_tags: tuple[str, ...] = ()
    parent_id: str | None = None
    remote_id: str | None = None


@dataclass(frozen=True)
class LibraryPage:
    items: tuple[MediaItem, ...]
    total: int
    limit: int
    offset: int
