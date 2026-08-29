from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class MediaSample:
    content: bytes
    mime_type: str


@dataclass(frozen=True)
class TaggingResult:
    tags: tuple[str, ...]
    diagnostic: dict[str, object]


class ImageTaggingProvider(Protocol):
    provider_name: str

    def suggest_tags(self, samples: tuple[MediaSample, ...]) -> TaggingResult: ...
