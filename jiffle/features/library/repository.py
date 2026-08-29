from typing import Protocol

from jiffle.features.library.domain import LibraryPage, LibraryQuery, MediaItem


class LibraryRepository(Protocol):
    def list_media(self, query: LibraryQuery) -> LibraryPage: ...

    def get_media(self, media_id: int) -> MediaItem | None: ...
