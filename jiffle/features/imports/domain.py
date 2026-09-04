from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ImportOutcome(StrEnum):
    ACCEPTED = "accepted"
    REVIEW = "review"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class LocalImportCommand:
    source_path: Path
    accept_without_source: bool = False


@dataclass(frozen=True)
class LocalImportResult:
    outcome: ImportOutcome
    candidate_id: int
    media_item_id: int | None = None
    review_item_id: int | None = None
    resolution_method: str | None = None
