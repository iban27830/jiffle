from dataclasses import dataclass
import json
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import subprocess
import zipfile
from uuid import uuid4

from PIL import Image, ImageSequence

from jiffle.configuration.settings import Settings
from jiffle.infrastructure.database.connection import connect_database


_OPERATOR_PREFIXES = (
    "author:",
    "-author:",
    "id:",
    "-id:",
    "parent:",
    "-parent:",
    "domain:",
    "-domain:",
    "source:",
    "-source:",
    "type:",
    "-type:",
    "has:",
    "-has:",
    "is:",
    "-is:",
    "orientation:",
    "-orientation:",
    "ratio:",
    "-ratio:",
    "width:",
    "-width:",
    "height:",
    "-height:",
    "text:",
    "-text:",
    "shelf:",
    "-shelf:",
)


@dataclass(frozen=True)
class CollectionFilters:
    authors: tuple[str, ...] = ()
    excluded_authors: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    excluded_domains: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    excluded_sources: tuple[str, ...] = ()
    shelves: tuple[str, ...] = ()
    excluded_shelves: tuple[str, ...] = ()
    media_type: str | None = None
    excluded_media_types: tuple[str, ...] = ()
    is_animated: bool | None = None
    parent_ids: tuple[str, ...] = ()
    excluded_parent_ids: tuple[str, ...] = ()
    media_ids: tuple[int, ...] = ()
    excluded_media_ids: tuple[int, ...] = ()
    has_flags: tuple[str, ...] = ()
    excluded_flags: tuple[str, ...] = ()
    orientation: str | None = None
    min_width: int | None = None
    max_width: int | None = None
    min_height: int | None = None
    max_height: int | None = None
    text_terms: tuple[str, ...] = ()

    @property
    def exclude_authors(self) -> tuple[str, ...]:
        return self.excluded_authors

    @property
    def exclude_domains(self) -> tuple[str, ...]:
        return self.excluded_domains

    @property
    def exclude_sources(self) -> tuple[str, ...]:
        return self.excluded_sources

    @property
    def exclude_shelves(self) -> tuple[str, ...]:
        return self.excluded_shelves

    @property
    def exclude_media_ids(self) -> tuple[int, ...]:
        return self.excluded_media_ids

    @property
    def exclude_flags(self) -> tuple[str, ...]:
        return self.excluded_flags

    @property
    def has_constraints(self) -> bool:
        return bool(
            self.authors
            or self.excluded_authors
            or self.domains
            or self.excluded_domains
            or self.sources
            or self.excluded_sources
            or self.shelves
            or self.excluded_shelves
            or self.media_type is not None
            or self.excluded_media_types
            or self.is_animated is not None
            or self.parent_ids
            or self.excluded_parent_ids
            or self.media_ids
            or self.excluded_media_ids
            or self.has_flags
            or self.excluded_flags
            or self.orientation is not None
            or self.min_width is not None
            or self.max_width is not None
            or self.min_height is not None
            or self.max_height is not None
            or self.text_terms
        )

    @property
    def has_positive_constraints(self) -> bool:
        return bool(
            self.authors
            or self.domains
            or self.sources
            or self.shelves
            or self.media_type is not None
            or self.is_animated is True
            or self.parent_ids
            or self.media_ids
            or self.has_flags
            or self.orientation is not None
            or self.min_width is not None
            or self.max_width is not None
            or self.min_height is not None
            or self.max_height is not None
            or self.text_terms
        )

    def __getitem__(self, key: str):
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def get(self, key: str, default=None):
        return getattr(self, key, default)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and hasattr(self, key)


def parse_collection_query(value: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(value, str):
        raise CollectionFailure("collections.invalid_query", "Query must be a string.")
    tokens = re.findall(r'(?:[^\s"]+|"[^"]*")+', value.strip())
    included: list[str] = []
    excluded: list[str] = []
    for token in tokens:
        lowered = token.lower()
        if lowered.startswith(_OPERATOR_PREFIXES):
            continue
        target = excluded if token.startswith("-") and len(token) > 1 else included
        raw_tag = token[1:] if target is excluded else token
        cleaned = raw_tag.strip().strip('"').strip("'").strip()
        if target is included and cleaned.startswith("-") and len(cleaned) > 1:
            target = excluded
            cleaned = cleaned[1:].strip().strip('"').strip("'").strip()
        tag = re.sub(r"\s+", "_", cleaned).lower().strip("_")
        if tag and tag not in target:
            target.append(tag)
    return tuple(included), tuple(excluded)


def _parse_dimension_bounds(
    raw_val: str, current_min: int | None, current_max: int | None
) -> tuple[int | None, int | None]:
    val = raw_val.strip()
    if not val:
        return current_min, current_max
    if val.startswith(">="):
        num = val[2:].strip()
        if num.isdigit():
            n = int(num)
            current_min = max(current_min, n) if current_min is not None else n
        return current_min, current_max
    if val.startswith(">"):
        num = val[1:].strip()
        if num.isdigit():
            n = int(num) + 1
            current_min = max(current_min, n) if current_min is not None else n
        return current_min, current_max
    if val.startswith("<="):
        num = val[2:].strip()
        if num.isdigit():
            n = int(num)
            current_max = min(current_max, n) if current_max is not None else n
        return current_min, current_max
    if val.startswith("<"):
        num = val[1:].strip()
        if num.isdigit():
            n = max(0, int(num) - 1)
            current_max = min(current_max, n) if current_max is not None else n
        return current_min, current_max
    if ".." in val:
        left, right = val.split("..", 1)
        if left.strip().isdigit():
            n = int(left.strip())
            current_min = max(current_min, n) if current_min is not None else n
        if right.strip().isdigit():
            n = int(right.strip())
            current_max = min(current_max, n) if current_max is not None else n
        return current_min, current_max
    if "-" in val and not val.startswith("-"):
        left, right = val.split("-", 1)
        if left.strip().isdigit() and right.strip().isdigit():
            n_min, n_max = int(left.strip()), int(right.strip())
            current_min = max(current_min, n_min) if current_min is not None else n_min
            current_max = min(current_max, n_max) if current_max is not None else n_max
            return current_min, current_max
    if val.startswith("="):
        val = val[1:].strip()
    if val.isdigit():
        n = int(val)
        current_min = max(current_min, n) if current_min is not None else n
        current_max = min(current_max, n) if current_max is not None else n
    return current_min, current_max


def parse_collection_filters(value: object) -> CollectionFilters:
    if isinstance(value, CollectionFilters):
        return value
    if not isinstance(value, str) or not value.strip():
        return CollectionFilters()
    tokens = re.findall(r'(?:[^\s"]+|"[^"]*")+', value.strip())
    authors: list[str] = []
    excluded_authors: list[str] = []
    domains: list[str] = []
    excluded_domains: list[str] = []
    sources: list[str] = []
    excluded_sources: list[str] = []
    shelves: list[str] = []
    excluded_shelves: list[str] = []
    media_type: str | None = None
    excluded_media_types: list[str] = []
    is_animated: bool | None = None
    parent_ids: list[str] = []
    excluded_parent_ids: list[str] = []
    media_ids: list[int] = []
    excluded_media_ids: list[int] = []
    has_flags: list[str] = []
    excluded_flags: list[str] = []
    orientation: str | None = None
    min_width: int | None = None
    max_width: int | None = None
    min_height: int | None = None
    max_height: int | None = None
    text_terms: list[str] = []

    for token in tokens:
        if not token.lower().startswith(_OPERATOR_PREFIXES):
            continue
        negated = token.startswith("-")
        body = token[1:] if negated else token
        op, raw_val = body.split(":", 1)
        op = op.strip().lower()
        val = raw_val.strip().strip('"').strip("'").strip()
        if not val:
            continue
        val_lower = val.lower()
        if op == "author":
            target = excluded_authors if negated else authors
            if val_lower not in target:
                target.append(val_lower)
        elif op == "domain":
            target = excluded_domains if negated else domains
            if val_lower not in target:
                target.append(val_lower)
        elif op == "source":
            target = excluded_sources if negated else sources
            if val_lower not in target:
                target.append(val_lower)
        elif op == "shelf":
            target = excluded_shelves if negated else shelves
            if val_lower not in target:
                target.append(val_lower)
        elif op == "id":
            target_ids = excluded_media_ids if negated else media_ids
            for part in val.split(","):
                part = part.strip()
                if part.isdigit() and int(part) > 0:
                    mid = int(part)
                    if mid not in target_ids:
                        target_ids.append(mid)
        elif op == "parent":
            target_parents = excluded_parent_ids if negated else parent_ids
            if val not in target_parents:
                target_parents.append(val)
        elif op == "type":
            if val_lower in {"animated", "gif"}:
                is_animated = not negated
            elif val_lower in {"motion", "moving"}:
                target_flags = excluded_flags if negated else has_flags
                if "motion" not in target_flags:
                    target_flags.append("motion")
            elif val_lower in {"image", "video"}:
                if negated:
                    if val_lower not in excluded_media_types:
                        excluded_media_types.append(val_lower)
                else:
                    media_type = val_lower
        elif op in {"has", "is"}:
            if val_lower in {"portrait", "landscape", "square"} and not negated:
                orientation = val_lower
            elif val_lower in {"image", "video"}:
                if negated:
                    if val_lower not in excluded_media_types:
                        excluded_media_types.append(val_lower)
                else:
                    media_type = val_lower
            elif val_lower in {"animated", "gif"}:
                is_animated = not negated
                target_flags = excluded_flags if negated else has_flags
                if "animated" not in target_flags:
                    target_flags.append("animated")
            elif val_lower in {"motion", "moving"}:
                target_flags = excluded_flags if negated else has_flags
                if "motion" not in target_flags:
                    target_flags.append("motion")
            else:
                target_flags = excluded_flags if negated else has_flags
                if val_lower not in target_flags:
                    target_flags.append(val_lower)
        elif op in {"orientation", "ratio"}:
            if val_lower in {"portrait", "vertical"}:
                orientation = "portrait"
            elif val_lower in {"landscape", "horizontal"}:
                orientation = "landscape"
            elif val_lower in {"square", "1:1"}:
                orientation = "square"
        elif op == "width" and not negated:
            min_width, max_width = _parse_dimension_bounds(val, min_width, max_width)
        elif op == "height" and not negated:
            min_height, max_height = _parse_dimension_bounds(val, min_height, max_height)
        elif op == "text" and not negated:
            if val not in text_terms:
                text_terms.append(val)

    return CollectionFilters(
        authors=tuple(authors),
        excluded_authors=tuple(excluded_authors),
        domains=tuple(domains),
        excluded_domains=tuple(excluded_domains),
        sources=tuple(sources),
        excluded_sources=tuple(excluded_sources),
        shelves=tuple(shelves),
        excluded_shelves=tuple(excluded_shelves),
        media_type=media_type,
        excluded_media_types=tuple(excluded_media_types),
        is_animated=is_animated,
        parent_ids=tuple(parent_ids),
        excluded_parent_ids=tuple(excluded_parent_ids),
        media_ids=tuple(media_ids),
        excluded_media_ids=tuple(excluded_media_ids),
        has_flags=tuple(has_flags),
        excluded_flags=tuple(excluded_flags),
        orientation=orientation,
        min_width=min_width,
        max_width=max_width,
        min_height=min_height,
        max_height=max_height,
        text_terms=tuple(text_terms),
    )


class CollectionFailure(Exception):
    def __init__(self, code: str, message: str, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class ExportPreview:
    item_count: int
    total_size: int
    author_counts: dict[str, int]
    violations: tuple[str, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CollectionCandidate:
    id: int
    media_type: str
    author: str | None
    domain: str | None
    width: int | None
    height: int | None
    usage_count: int
    last_used_at: str | None
    author_limit_exceeded: bool = False


@dataclass(frozen=True)
class CollectionComposition:
    items: tuple[CollectionCandidate, ...]
    requested_count: int
    available_count: int
    max_similarity: float
    most_similar_collection: dict[str, object] | None

    @property
    def can_save(self) -> bool:
        return len(self.items) == self.requested_count


def normalize_tags(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise CollectionFailure("collections.invalid_tags", "Tags must be an array.")
    normalized = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
            raise CollectionFailure("collections.invalid_tags", "Tags contain an invalid value.")
        tag = value.strip().lower()
        if tag not in normalized:
            normalized.append(tag)
    return tuple(normalized)


def build_tag_alias_map(connection: sqlite3.Connection) -> dict[str, frozenset[str]]:
    aliases: dict[str, set[str]] = {}
    for row in connection.execute("SELECT canonical_tag, alias FROM tag_aliases"):
        canonical = str(row["canonical_tag"]).strip().lower()
        alias = str(row["alias"]).strip().lower()
        if not canonical or not alias:
            continue
        aliases.setdefault(canonical, set()).update((canonical, alias))
        aliases.setdefault(alias, set()).update((canonical, alias))
    return {tag: frozenset(values) for tag, values in aliases.items()}


def expand_tag_aliases(tag: object, aliases: dict[str, frozenset[str]]) -> frozenset[str]:
    value = str(tag).strip().lower()
    return aliases.get(value, frozenset((value,)))


def build_collection_preview(
    connection: sqlite3.Connection,
    included_tags: tuple[str, ...],
    excluded_tags: tuple[str, ...],
    requested_count: int,
    max_items_per_author: int,
    excluded_ids: tuple[int, ...] = (),
    variant_count: int = 32,
    block_unreviewed: bool = False,
    block_crop: bool = False,
    query: str = "",
    extra_filters: CollectionFilters | None = None,
) -> CollectionComposition:
    filters = extra_filters if extra_filters is not None else parse_collection_filters(query)
    if not included_tags and not filters.has_positive_constraints:
        raise CollectionFailure("collections.tags_required", "At least one included tag is required.")
    if not 1 <= requested_count <= 1000:
        raise CollectionFailure("collections.invalid_count", "Requested count must be from 1 to 1000.")
    candidates = _matching_candidates(
        connection,
        included_tags,
        excluded_tags,
        excluded_ids,
        block_unreviewed=block_unreviewed,
        block_crop=block_crop,
        query=query,
        extra_filters=filters,
    )
    available_count = len(candidates)
    target = min(requested_count, available_count)
    histories = (
        _collection_histories(connection, included_tags, excluded_tags)
        if (included_tags or excluded_tags)
        else []
    )
    if not target:
        return CollectionComposition((), requested_count, available_count, 0.0, None)

    generator = random.SystemRandom()
    variants = []
    for _ in range(max(1, variant_count)):
        shuffled = list(candidates)
        generator.shuffle(shuffled)
        variants.append(_choose_variant(shuffled, target, max_items_per_author, histories))
    chosen = min(variants, key=lambda value: _variant_score(value, histories))
    similarity, similar = _similarity_summary(chosen, histories)
    return CollectionComposition(tuple(chosen), requested_count, available_count, similarity, similar)


def choose_replacement_candidate(
    connection: sqlite3.Connection,
    included_tags: tuple[str, ...],
    excluded_tags: tuple[str, ...],
    max_items_per_author: int,
    current_ids: tuple[int, ...] = (),
    rejected_ids: tuple[int, ...] = (),
    block_unreviewed: bool = False,
    block_crop: bool = False,
    query: str = "",
    extra_filters: CollectionFilters | None = None,
) -> CollectionCandidate:
    preview = build_collection_preview(
        connection,
        included_tags,
        excluded_tags,
        1,
        max_items_per_author,
        tuple(dict.fromkeys((*current_ids, *rejected_ids))),
        block_unreviewed=block_unreviewed,
        block_crop=block_crop,
        query=query,
        extra_filters=extra_filters,
    )
    if not preview.items:
        raise CollectionFailure("collections.replacement_unavailable", "No replacement is available.")
    return preview.items[0]


def replace_collection_items(
    connection: sqlite3.Connection, collection_id: int, media_ids: list[int], settings=None
) -> None:
    _collection(connection, collection_id)
    if settings is not None:
        ensure_crop_eligible(connection, settings, media_ids)
    if len(media_ids) != len(set(media_ids)):
        raise CollectionFailure(
            "collections.duplicate_item", "A collection cannot contain an item twice."
        )
    if media_ids:
        placeholders = ",".join("?" for _ in media_ids)
        found = {
            row[0] for row in connection.execute(
                f"SELECT id FROM media_items WHERE deleted_at IS NULL AND id IN ({placeholders})",
                media_ids,
            )
        }
        if found != set(media_ids):
            raise CollectionFailure(
                "collections.media_not_found", "One or more media items were not found."
            )
    existing = {
        row[0] for row in connection.execute(
            "SELECT media_item_id FROM collection_items WHERE collection_id=?", (collection_id,)
        )
    }
    wanted = set(media_ids)
    connection.executemany(
        "DELETE FROM collection_items WHERE collection_id=? AND media_item_id=?",
        ((collection_id, media_id) for media_id in existing - wanted),
    )
    connection.execute(
        "UPDATE collection_items SET position=position+1000000 WHERE collection_id=?",
        (collection_id,),
    )
    for position, media_id in enumerate(media_ids):
        if media_id in existing:
            connection.execute(
                "UPDATE collection_items SET position=? WHERE collection_id=? AND media_item_id=?",
                (position, collection_id, media_id),
            )
        else:
            connection.execute(
                "INSERT INTO collection_items (collection_id, media_item_id, position, added_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (collection_id, media_id, position),
            )
    connection.execute(
        "UPDATE collections SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (collection_id,)
    )
    connection.commit()


def preview_export(
    connection: sqlite3.Connection, settings: Settings, collection_id: int
) -> ExportPreview:
    _collection(connection, collection_id)
    rows = _collection_media(connection, collection_id)
    author_counts: dict[str, int] = {}
    total_size = 0
    for row in rows:
        author = row["author"] or "unknown"
        author_counts[author] = author_counts.get(author, 0) + 1
        total_size += int(row["file_size"] or 0)
    violations = []
    warnings = []
    if any(
        count > settings.max_items_per_author and _limited_author(author)
        for author, count in author_counts.items()
    ):
        if settings.max_items_per_author > 0:
            warnings.append("author_limit_exceeded")
    if not rows:
        violations.append("collection_empty")
    if settings.trim_block_unreviewed_export and any(
        row["trim_review_status"] == "pending" and row["derived_from_media_id"] is None
        for row in rows
    ):
        # The collection can still be exported after the waiting clips are
        # confirmed or split; this is a warning so the builder can explain why.
        warnings.append("unreviewed_trim")
    if settings.crop_review_enabled:
        from jiffle.features.crop_editor.review import blocked_media
        if blocked_media(connection, [row["id"] for row in rows]):
            violations.append("unreviewed_crop")
    return ExportPreview(len(rows), total_size, author_counts, tuple(violations), tuple(warnings))


def create_export_job(connection: sqlite3.Connection, collection_id: int) -> int:
    collection = _collection(connection, collection_id)
    cursor = connection.execute(
        "INSERT INTO background_jobs (job_type, status) VALUES ('collection_export', 'pending')"
    )
    job_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO export_runs (job_id, collection_id, collection_name, status) "
        "VALUES (?, ?, ?, 'pending')", (job_id, collection_id, collection["name"])
    )
    connection.commit()
    return job_id


def export_staging_directory(settings: Settings, label: str) -> Path:
    """Create a unique staging directory used while a collection is exported."""
    root = settings.resolved_export_path.parent / "export-staging"
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{label}-{uuid4().hex}.part"
    destination.mkdir()
    return destination


def export_collection_directory(
    connection: sqlite3.Connection,
    settings: Settings,
    collection_id: int,
    destination: Path,
    progress=None,
) -> tuple[object, ExportPreview, list[dict]]:
    """Export a collection into ``destination`` with conversions and size limits.

    Used both by the background folder export and by the browser archive
    download, so both deliveries produce exactly the same files.
    """
    ensure_crop_eligible(connection, settings, [row["id"] for row in _collection_media(connection, collection_id)])
    preview = preview_export(connection, settings, collection_id)
    if preview.violations:
        raise CollectionFailure("exports.constraints_failed", ", ".join(preview.violations))
    collection = _collection(connection, collection_id)
    rows = _collection_media(connection, collection_id)
    if settings.trim_block_unreviewed_export:
        blocked = [
            row for row in rows
            if row["trim_review_status"] == "pending" and row["derived_from_media_id"] is None
        ]
        if blocked:
            raise CollectionFailure(
                "exports.unreviewed_media",
                f"{len(blocked)} clip(s) still wait for trim review. "
                "Confirm or split them before exporting this collection.",
            )
    exported_items = []
    for index, row in enumerate(rows, start=1):
        source = _media_path(settings.media_path, row["file_path"])
        if source is None or not source.is_file():
            raise CollectionFailure(
                "exports.media_missing", f"Media item {row['id']} is unavailable."
            )
        original_size = source.stat().st_size
        exported = _export_media(source, row["media_type"], settings, destination, index)
        exported_items.append({
            "position": row["position"], "media_item_id": row["id"],
            "source_url": row["source_url"], "filename": exported.name,
            "format": exported.suffix.lower().lstrip("."),
            "file_size": exported.stat().st_size,
            "converted": (
                exported.suffix.lower() != source.suffix.lower()
                or exported.stat().st_size != original_size
            ),
        })
        if progress is not None:
            progress(10 + int(80 * index / len(rows)))
    manifest = {
        "collection_id": collection_id,
        "name": collection["name"],
        "items": exported_items,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return collection, preview, exported_items


_UNSAFE_DOWNLOAD_CHARACTERS = frozenset('/\\:*?"<>|')


def export_archive_prefix(name: object) -> str:
    """Return a download-safe name that keeps the collection's own spelling."""
    cleaned = "".join(
        character for character in str(name or "")
        if character not in _UNSAFE_DOWNLOAD_CHARACTERS and ord(character) >= 32
    ).strip().strip(".")
    return cleaned[:80] or "collection"


def export_archive_filename(name: object) -> str:
    return f"{export_archive_prefix(name)}.zip"


def iter_export_archive(destination: Path, archive_prefix: str):
    """Yield a ZIP archive of ``destination`` in chunks.

    The archive is streamed from a temporary file so memory use stays bounded,
    and both the archive and the staging directory are removed when the
    response finishes (or the client disconnects).
    """
    archive_path = destination.with_suffix(".zip")
    try:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(destination.rglob("*")):
                if path.is_file():
                    entry = f"{archive_prefix}/{path.relative_to(destination).as_posix()}"
                    archive.write(path, entry)
        with archive_path.open("rb") as file:
            while True:
                chunk = file.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
    finally:
        archive_path.unlink(missing_ok=True)
        shutil.rmtree(destination, ignore_errors=True)


def run_export_job(
    database_path: Path, settings: Settings, job_id: int, collection_id: int
) -> None:
    connection = connect_database(database_path)
    staging: Path | None = None
    try:
        connection.execute(
            "UPDATE background_jobs SET status='running', progress=5, "
            "started_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,)
        )
        connection.execute(
            "UPDATE export_runs SET status='running' WHERE job_id=?", (job_id,)
        )
        connection.commit()
        export_root = settings.resolved_export_path
        staging = export_staging_directory(settings, f"run-{job_id}")
        collection, preview, exported_items = export_collection_directory(
            connection, settings, collection_id, staging,
            progress=lambda value: connection.execute(
                "UPDATE background_jobs SET progress=? WHERE id=?", (value, job_id)
            ),
        )
        export_root.mkdir(parents=True, exist_ok=True)
        destination = export_root / f"{collection_id}-{_safe_name(collection['name'])}-run-{job_id}"
        os.replace(staging, destination)
        staging = None
        total_size = sum(item["file_size"] for item in exported_items)
        result = json.dumps({
            "export_run_id": _export_run_id(connection, job_id),
            "destination": str(destination), "item_count": preview.item_count,
            "total_size": total_size,
        })
        connection.execute(
            "UPDATE export_runs SET status='completed', destination_path=?, item_count=?, "
            "total_size=?, finished_at=CURRENT_TIMESTAMP WHERE job_id=?",
            (str(destination), preview.item_count, total_size, job_id),
        )
        connection.execute(
            "UPDATE background_jobs SET status='completed', progress=100, result_json=?, "
            "finished_at=CURRENT_TIMESTAMP WHERE id=?", (result, job_id)
        )
        connection.execute(
            "INSERT INTO operation_history "
            "(event_type, entity_type, entity_id, details_json) "
            "VALUES ('collection.exported', 'collection', ?, ?)",
            (collection_id, result),
        )
        connection.commit()
    except CollectionFailure as error:
        _fail_export(connection, job_id, error.code, error.message)
    except Exception:
        _fail_export(connection, job_id, "exports.failed", "Collection export failed.")
        raise
    finally:
        if staging and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        connection.close()


def _collection(connection, collection_id):
    row = connection.execute("SELECT * FROM collections WHERE id=?", (collection_id,)).fetchone()
    if row is None:
        raise CollectionFailure("collections.not_found", "Collection was not found.")
    return row


def _collection_media(connection, collection_id):
    return connection.execute(
        "SELECT item.*, member.position FROM collection_items member "
        "JOIN media_items item ON item.id=member.media_item_id "
        "WHERE member.collection_id=? AND item.deleted_at IS NULL "
        "ORDER BY member.position", (collection_id,)
    ).fetchall()


def _operator_filter_clauses(
    connection: sqlite3.Connection, filters: CollectionFilters
) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    parameters: list[object] = []
    if not filters.has_constraints:
        return clauses, parameters

    def col_exists(table: str, column: str) -> bool:
        return any(
            row[1] == column
            for row in connection.execute(f"PRAGMA table_info({table})")
        )

    def table_exists(table: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is not None
        )

    author_expr = (
        "(',' || LOWER(REPLACE(COALESCE(NULLIF(TRIM(item.author), ''), "
        "(SELECT author FROM media_sources source "
        "WHERE source.media_item_id = item.id AND author IS NOT NULL "
        "AND TRIM(author) <> '' LIMIT 1), ''), ', ', ',')) || ',')"
    )
    if len(filters.authors) == 1:
        clauses.append(f"{author_expr} LIKE ?")
        parameters.append(f"%,{filters.authors[0]},%")
    elif len(filters.authors) > 1:
        clauses.append(
            "(" + " OR ".join(f"{author_expr} LIKE ?" for _ in filters.authors) + ")"
        )
        parameters.extend(f"%,{a},%" for a in filters.authors)

    for ex_author in filters.excluded_authors:
        clauses.append(f"{author_expr} NOT LIKE ?")
        parameters.append(f"%,{ex_author},%")

    if filters.domains:
        dom_sub = []
        for dom in filters.domains:
            dom_sub.append(
                "(LOWER(COALESCE(item.domain, '')) = ? OR LOWER(COALESCE(item.domain, '')) LIKE ?)"
            )
            parameters.extend((dom, f"%{dom}%"))
        clauses.append("(" + " OR ".join(dom_sub) + ")")

    for ex_dom in filters.excluded_domains:
        clauses.append(
            "NOT (LOWER(COALESCE(item.domain, '')) = ? OR LOWER(COALESCE(item.domain, '')) LIKE ?)"
        )
        parameters.extend((ex_dom, f"%{ex_dom}%"))

    has_provider = table_exists("media_sources") and col_exists(
        "media_sources", "provider"
    )
    if filters.sources:
        src_sub = []
        for src in filters.sources:
            if has_provider:
                src_sub.append(
                    "(EXISTS (SELECT 1 FROM media_sources s WHERE s.media_item_id = item.id AND LOWER(s.provider) = ?) "
                    "OR LOWER(COALESCE(item.domain, '')) LIKE ?)"
                )
                parameters.extend((src, f"%{src}%"))
            else:
                src_sub.append("LOWER(COALESCE(item.domain, '')) LIKE ?")
                parameters.append(f"%{src}%")
        clauses.append("(" + " OR ".join(src_sub) + ")")

    for ex_src in filters.excluded_sources:
        if has_provider:
            clauses.append(
                "NOT (EXISTS (SELECT 1 FROM media_sources s WHERE s.media_item_id = item.id AND LOWER(s.provider) = ?) "
                "OR LOWER(COALESCE(item.domain, '')) LIKE ?)"
            )
            parameters.extend((ex_src, f"%{ex_src}%"))
        else:
            clauses.append("NOT (LOWER(COALESCE(item.domain, '')) LIKE ?)")
            parameters.append(f"%{ex_src}%")

    if table_exists("shelves") and table_exists("media_shelf_items"):
        for shelf_val in filters.shelves:
            norm_val = shelf_val.strip().lower()
            if not norm_val:
                continue
            under_val = re.sub(r"\s+", "_", norm_val)
            clauses.append(
                "EXISTS (SELECT 1 FROM media_shelf_items msi JOIN shelves s ON s.id = msi.shelf_id "
                "WHERE msi.media_item_id = item.id AND (LOWER(s.name) = ? OR LOWER(REPLACE(s.name, ' ', '_')) = ? OR CAST(s.id AS TEXT) = ?))"
            )
            parameters.extend((norm_val, under_val, norm_val))
        for ex_shelf in filters.excluded_shelves:
            norm_val = ex_shelf.strip().lower()
            if not norm_val:
                continue
            under_val = re.sub(r"\s+", "_", norm_val)
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM media_shelf_items msi JOIN shelves s ON s.id = msi.shelf_id "
                "WHERE msi.media_item_id = item.id AND (LOWER(s.name) = ? OR LOWER(REPLACE(s.name, ' ', '_')) = ? OR CAST(s.id AS TEXT) = ?))"
            )
            parameters.extend((norm_val, under_val, norm_val))

    if filters.media_type:
        clauses.append("item.media_type = ?")
        parameters.append(filters.media_type)
    for ex_type in filters.excluded_media_types:
        clauses.append("item.media_type <> ?")
        parameters.append(ex_type)

    gif_sql = (
        "(LOWER(COALESCE(item.original_filename, '')) LIKE '%.gif' OR LOWER(COALESCE(item.file_path, '')) LIKE '%.gif')"
        if col_exists("media_items", "original_filename")
        else "LOWER(COALESCE(item.file_path, '')) LIKE '%.gif'"
    )
    has_is_anim = col_exists("media_items", "is_animated")
    animated_sql = f"(item.is_animated = 1 OR {gif_sql})" if has_is_anim else gif_sql

    if filters.is_animated is True:
        clauses.append(animated_sql)
    elif filters.is_animated is False:
        if has_is_anim:
            clauses.append(
                f"(COALESCE(item.is_animated, 0) = 0 AND (item.is_animated IS NOT NULL OR NOT ({gif_sql})))"
            )
        else:
            clauses.append(f"NOT ({gif_sql})")

    if filters.parent_ids:
        parent_sub = []
        for pid in filters.parent_ids:
            parts = []
            if col_exists("media_items", "parent_id"):
                parts.append("item.parent_id = ?")
                parameters.append(pid)
            if table_exists("media_sources") and col_exists("media_sources", "remote_id") and col_exists("media_sources", "parent_id"):
                parts.append(
                    "EXISTS (SELECT 1 FROM media_sources s WHERE s.media_item_id = item.id AND (s.remote_id = ? OR s.parent_id = ?))"
                )
                parameters.extend((pid, pid))
            if parts:
                parent_sub.append("(" + " OR ".join(parts) + ")")
        if parent_sub:
            clauses.append("(" + " OR ".join(parent_sub) + ")")

    for ex_pid in filters.excluded_parent_ids:
        parts = []
        if col_exists("media_items", "parent_id"):
            parts.append("item.parent_id = ?")
            parameters.append(ex_pid)
        if table_exists("media_sources") and col_exists("media_sources", "remote_id") and col_exists("media_sources", "parent_id"):
            parts.append(
                "EXISTS (SELECT 1 FROM media_sources s WHERE s.media_item_id = item.id AND (s.remote_id = ? OR s.parent_id = ?))"
            )
            parameters.extend((ex_pid, ex_pid))
        if parts:
            clauses.append("NOT (" + " OR ".join(parts) + ")")

    if len(filters.media_ids) == 1:
        clauses.append("item.id = ?")
        parameters.append(filters.media_ids[0])
    elif len(filters.media_ids) > 1:
        clauses.append(f"item.id IN ({','.join('?' for _ in filters.media_ids)})")
        parameters.extend(filters.media_ids)

    if filters.excluded_media_ids:
        clauses.append(
            f"item.id NOT IN ({','.join('?' for _ in filters.excluded_media_ids)})"
        )
        parameters.extend(filters.excluded_media_ids)

    def flag_sql(flag_name: str) -> str | None:
        flag = flag_name.strip().lower()
        if flag == "parent":
            parts = []
            if col_exists("media_items", "parent_id"):
                parts.append(
                    "(item.parent_id IS NOT NULL AND TRIM(item.parent_id) <> '')"
                )
            if table_exists("media_sources") and col_exists("media_sources", "parent_id"):
                parts.append(
                    "EXISTS (SELECT 1 FROM media_sources s WHERE s.media_item_id = item.id AND s.parent_id IS NOT NULL AND TRIM(s.parent_id) <> '')"
                )
            return "(" + " OR ".join(parts) + ")" if parts else "0"
        if flag == "children":
            if not (table_exists("media_sources") and col_exists("media_sources", "remote_id")):
                return "0"
            child_conds = []
            if col_exists("media_items", "parent_id"):
                child_conds.append("child.parent_id = self_s.remote_id")
            if col_exists("media_sources", "parent_id"):
                child_conds.append(
                    "EXISTS (SELECT 1 FROM media_sources child_s WHERE child_s.media_item_id = child.id AND child_s.parent_id = self_s.remote_id)"
                )
            if not child_conds:
                return "0"
            return (
                "EXISTS (SELECT 1 FROM media_sources self_s "
                "WHERE self_s.media_item_id = item.id AND self_s.remote_id IS NOT NULL AND TRIM(self_s.remote_id) <> '' "
                f"AND EXISTS (SELECT 1 FROM media_items child WHERE child.deleted_at IS NULL AND ({' OR '.join(child_conds)})))"
            )
        if flag == "family":
            if not col_exists("media_items", "family_id"):
                return "0"
            return (
                "(item.family_id IS NOT NULL AND EXISTS ("
                "SELECT 1 FROM media_items fam WHERE fam.deleted_at IS NULL "
                "AND fam.family_id = item.family_id AND fam.id <> item.id))"
            )
        if flag == "source":
            parts = ["(item.source_url IS NOT NULL AND TRIM(item.source_url) <> '')"]
            if table_exists("media_sources") and col_exists("media_sources", "canonical_url"):
                parts.append(
                    "EXISTS (SELECT 1 FROM media_sources s WHERE s.media_item_id = item.id AND s.canonical_url IS NOT NULL AND TRIM(s.canonical_url) <> '')"
                )
            return "(" + " OR ".join(parts) + ")"
        if flag == "edited":
            parts = []
            if col_exists("media_items", "derived_from_media_id"):
                parts.append("item.derived_from_media_id IS NOT NULL")
            if table_exists("media_revisions") and col_exists("media_items", "active_revision_id"):
                rev_conds = []
                if col_exists("media_revisions", "revision_number"):
                    rev_conds.append("COALESCE(r.revision_number, 1) > 1")
                if col_exists("media_revisions", "operation"):
                    rev_conds.append(
                        "(r.operation IS NOT NULL AND r.operation <> 'original')"
                    )
                if col_exists("media_revisions", "parent_revision_id"):
                    rev_conds.append("r.parent_revision_id IS NOT NULL")
                if rev_conds:
                    parts.append(
                        f"EXISTS (SELECT 1 FROM media_revisions r WHERE r.id = item.active_revision_id AND ({' OR '.join(rev_conds)}))"
                    )
            return "(" + " OR ".join(parts) + ")" if parts else "0"
        if flag in {"animated", "gif"}:
            return animated_sql
        if flag in {"motion", "moving"}:
            return f"(item.media_type = 'video' OR ({animated_sql}))"
        if flag in {"shelf", "shelves"}:
            if table_exists("media_shelf_items"):
                return "EXISTS (SELECT 1 FROM media_shelf_items msi WHERE msi.media_item_id = item.id)"
            return "0"
        return None

    for flag in filters.has_flags:
        expr = flag_sql(flag)
        if expr:
            clauses.append(expr)
    for flag in filters.excluded_flags:
        expr = flag_sql(flag)
        if expr:
            clauses.append(f"NOT ({expr})")

    if filters.orientation:
        if filters.orientation == "portrait":
            clauses.append("item.height > item.width")
        elif filters.orientation == "landscape":
            clauses.append("item.width > item.height")
        elif filters.orientation == "square":
            clauses.append("item.width = item.height AND item.width IS NOT NULL")

    if filters.min_width is not None:
        clauses.append("item.width >= ?")
        parameters.append(filters.min_width)
    if filters.max_width is not None:
        clauses.append("item.width <= ?")
        parameters.append(filters.max_width)
    if filters.min_height is not None:
        clauses.append("item.height >= ?")
        parameters.append(filters.min_height)
    if filters.max_height is not None:
        clauses.append("item.height <= ?")
        parameters.append(filters.max_height)

    for term in filters.text_terms:
        pattern = f"%{term}%"
        clauses.append(
            "(item.author LIKE ? OR item.domain LIKE ? OR item.source_url LIKE ? "
            "OR EXISTS (SELECT 1 FROM media_tags text_tag "
            "WHERE text_tag.media_item_id = item.id AND text_tag.tag LIKE ?))"
        )
        parameters.extend((pattern, pattern, pattern, pattern))

    return clauses, parameters


def _matching_candidates(
    connection,
    included_tags,
    excluded_tags,
    excluded_ids,
    block_unreviewed=False,
    block_crop=False,
    query: str = "",
    extra_filters: CollectionFilters | None = None,
):
    aliases = build_tag_alias_map(connection)
    filters = extra_filters if extra_filters is not None else parse_collection_filters(query)

    clauses = ["item.deleted_at IS NULL"]
    if block_crop:
        from jiffle.features.crop_editor.review import admission_sql
        clauses.append(admission_sql())
    parameters: list[object] = []
    # A clip that was split into fragments keeps its place in the library but
    # is never picked automatically: the fragments stand in for it.
    clauses.append(
        "NOT EXISTS (SELECT 1 FROM media_items fragment "
        "WHERE fragment.derived_from_media_id=item.id AND fragment.deleted_at IS NULL)"
    )
    if block_unreviewed:
        clauses.append(
            "NOT (COALESCE(item.trim_review_status, '')='pending' "
            "AND item.derived_from_media_id IS NULL)"
        )
    for tag in included_tags:
        values = expand_tag_aliases(tag, aliases)
        clauses.append("EXISTS (SELECT 1 FROM media_tags mt WHERE mt.media_item_id=item.id AND LOWER(mt.tag) IN (" + ",".join("?" for _ in values) + "))")
        parameters.extend(values)
    for tag in excluded_tags:
        values = expand_tag_aliases(tag, aliases)
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM media_tags mt WHERE mt.media_item_id=item.id AND LOWER(mt.tag) IN (" + ",".join("?" for _ in values) + "))"
        )
        parameters.extend(values)
    if excluded_ids:
        clauses.append(f"item.id NOT IN ({','.join('?' for _ in excluded_ids)})")
        parameters.extend(excluded_ids)
    extra_clauses, extra_params = _operator_filter_clauses(connection, filters)
    clauses.extend(extra_clauses)
    parameters.extend(extra_params)

    return [CollectionCandidate(
        id=row["id"], media_type=row["media_type"], author=row["resolved_author"],
        domain=row["domain"], width=row["width"], height=row["height"],
        usage_count=int(row["usage_count"]), last_used_at=row["last_used_at"],
    ) for row in connection.execute(
        "SELECT item.id, item.media_type, "
        "COALESCE(NULLIF(TRIM(item.author), ''), (SELECT s.author FROM media_sources s WHERE s.media_item_id=item.id AND s.author IS NOT NULL AND TRIM(s.author) <> '' LIMIT 1)) AS resolved_author, "
        "item.domain, item.width, item.height, "
        "COUNT(member.collection_id) usage_count, MAX(member.added_at) last_used_at "
        "FROM media_items item LEFT JOIN collection_items member ON member.media_item_id=item.id "
        f"WHERE {' AND '.join(clauses)} GROUP BY item.id", parameters
    )]


def _collection_histories(connection, included_tags, excluded_tags):
    rows = connection.execute(
        "SELECT collection.id, collection.name, collection.created_at, member.media_item_id "
        "FROM collections collection JOIN collection_items member ON member.collection_id=collection.id "
        "WHERE NOT EXISTS (SELECT 1 FROM collection_tags stored WHERE stored.collection_id=collection.id "
        "AND ((stored.disposition='include' AND stored.tag NOT IN ({include})) "
        "OR (stored.disposition='exclude' AND stored.tag NOT IN ({exclude})))) "
        "AND (SELECT COUNT(*) FROM collection_tags stored WHERE stored.collection_id=collection.id "
        "AND stored.disposition='include')=? "
        "AND (SELECT COUNT(*) FROM collection_tags stored WHERE stored.collection_id=collection.id "
        "AND stored.disposition='exclude')=? ORDER BY datetime(collection.created_at) DESC, collection.id DESC"
        .format(
            include=','.join('?' for _ in included_tags) or "''",
            exclude=','.join('?' for _ in excluded_tags) or "''",
        ), (*included_tags, *excluded_tags, len(included_tags), len(excluded_tags))
    ).fetchall()
    histories: dict[int, dict[str, object]] = {}
    for row in rows:
        history = histories.setdefault(row["id"], {
            "id": row["id"], "name": row["name"], "created_at": row["created_at"], "ids": set(),
        })
        history["ids"].add(row["media_item_id"])
    return list(histories.values())


def _choose_variant(candidates, target, max_author, histories):
    recent_ids = set().union(*(history["ids"] for history in histories[:2])) if histories else set()
    candidates.sort(key=lambda item: (
        item.usage_count, item.id in recent_ids, item.last_used_at or "", random.random()
    ))
    selected = []
    delayed = []
    author_counts: dict[str, int] = {}
    for candidate in candidates:
        author = _limited_author(candidate.author)
        if max_author and author and author_counts.get(author, 0) >= max_author:
            delayed.append(candidate)
            continue
        selected.append(candidate)
        if author:
            author_counts[author] = author_counts.get(author, 0) + 1
        if len(selected) == target:
            break
    for candidate in delayed:
        if len(selected) == target:
            break
        selected.append(CollectionCandidate(**{
            **candidate.__dict__, "author_limit_exceeded": True,
        }))
    return selected


def _variant_score(items, histories):
    ids = {item.id for item in items}
    recent_overlap = sum(len(ids & history["ids"]) for history in histories[:2])
    maximum_overlap = max((len(ids & history["ids"]) for history in histories), default=0)
    pair_repeats = 0
    for history in histories:
        overlap = len(ids & history["ids"])
        pair_repeats += overlap * (overlap - 1) // 2
    return (
        sum(item.author_limit_exceeded for item in items),
        sum(item.usage_count for item in items),
        recent_overlap, maximum_overlap, pair_repeats,
        max((item.last_used_at or "" for item in items), default=""),
        random.random(),
    )


def _similarity_summary(items, histories):
    ids = {item.id for item in items}
    best = None
    best_similarity = 0.0
    for history in histories:
        denominator = min(len(ids), len(history["ids"]))
        similarity = len(ids & history["ids"]) / denominator if denominator else 0.0
        if similarity > best_similarity:
            best_similarity = similarity
            best = {"id": history["id"], "name": history["name"]}
    return round(best_similarity, 4), best


def _limited_author(author):
    normalized = (author or "").strip().lower()
    return None if normalized in {"", "unknown", "автор_неизвестен", "ии_теггер", "local_ai"} else normalized


def _media_path(root_path, stored_path):
    root = root_path.resolve()
    candidate = (root / stored_path).resolve()
    return candidate if candidate.is_relative_to(root) else None


def _export_media(source, media_type, settings, staging, index):
    rules = dict(settings.export_format_rules)
    source_format = source.suffix.lower().lstrip(".")
    target_format = rules.get(source_format, source_format)
    prefix = f"{index:04d}-{source.stem}"
    destination = staging / f"{prefix}.{target_format}"
    source_size = source.stat().st_size
    output_type = "video" if target_format == "mp4" else media_type
    limit = (
        settings.max_video_export_size_bytes
        if output_type == "video"
        else settings.max_image_export_size_bytes
    )
    if target_format == source_format and source_size <= limit:
        shutil.copy2(source, destination)
    elif output_type == "video":
        _transcode_video(source, destination, limit, source_size)
    else:
        _compress_image(source, destination, target_format, limit)
    size = destination.stat().st_size if destination.is_file() else 0
    if size > limit:
        destination.unlink(missing_ok=True)
        raise CollectionFailure(
            "exports.file_size_exceeded",
            f"{source.name} could not be reduced below {limit / 1048576:.0f} MB.",
        )
    return destination


def _video_export_budget(source_size, limit):
    """Return a per-file budget that avoids inflating small converted videos."""
    if source_size >= limit:
        return limit
    return min(limit, max(source_size + 256 * 1024, int(source_size * 1.10)))


def _transcode_video(source, destination, limit, source_size=None):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise CollectionFailure(
            "exports.ffmpeg_unavailable",
            "FFmpeg is required to convert this file. Install FFmpeg and add it to PATH.",
        )
    try:
        probe = subprocess.run(
            [ffmpeg, "-nostdin", "-i", str(source)], capture_output=True, text=True, timeout=60,
        )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", probe.stderr)
        if not match:
            raise ValueError("duration missing")
        hours, minutes, seconds = match.groups()
        duration = max(int(hours) * 3600 + int(minutes) * 60 + float(seconds), 0.1)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise CollectionFailure(
            "exports.media_invalid", f"Could not read {source.name} for conversion."
        ) from error
    if source_size is None:
        source_size = source.stat().st_size
    budget = _video_export_budget(source_size, limit)
    total_bitrate = min(max(int(budget * 8 * 0.90 / duration), 16_000), 50_000_000)
    has_audio = source.suffix.lower() != ".gif"
    audio_bitrate = (
        min(128_000, max(32_000, int(total_bitrate * 0.20)))
        if has_audio and total_bitrate >= 160_000
        else 0
    )
    video_bitrate = max(total_bitrate - audio_bitrate, 16_000)
    temporary = destination.with_name(f".{destination.stem}-{uuid4().hex}.mp4")
    try:
        for _ in range(8):
            command = [
                ffmpeg, "-nostdin", "-y", "-i", str(source), "-map", "0:v:0",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-b:v", str(video_bitrate), "-maxrate", str(video_bitrate),
                "-bufsize", str(video_bitrate * 2), "-movflags", "+faststart",
            ]
            if has_audio and audio_bitrate:
                command += ["-map", "0:a?", "-c:a", "aac", "-b:a", str(audio_bitrate)]
            else:
                command += ["-an"]
            command.append(str(temporary))
            try:
                subprocess.run(command, check=True, capture_output=True, timeout=1800)
            except (OSError, subprocess.SubprocessError) as error:
                raise CollectionFailure(
                    "exports.transcode_failed", f"Could not convert {source.name}."
                ) from error
            if temporary.stat().st_size <= budget:
                os.replace(temporary, destination)
                return
            temporary.unlink(missing_ok=True)
            video_bitrate = max(int(video_bitrate * 0.72), 16_000)
        raise CollectionFailure(
            "exports.file_size_exceeded",
            f"{source.name} could not be converted within its "
            f"{budget / 1048576:.1f} MB export budget.",
        )
    finally:
        temporary.unlink(missing_ok=True)


def _compress_image(source, destination, target_format, limit):
    save_format = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "gif": "GIF"}.get(target_format)
    if save_format is None:
        raise CollectionFailure(
            "exports.unsupported_conversion", f"Conversion to {target_format} is not supported."
        )
    try:
        with Image.open(source) as opened:
            frames = [frame.copy().convert("RGBA") for frame in ImageSequence.Iterator(opened)]
            durations = [frame.info.get("duration", opened.info.get("duration", 100)) for frame in ImageSequence.Iterator(opened)]
            loop = opened.info.get("loop", 0)
    except (OSError, ValueError) as error:
        raise CollectionFailure("exports.media_invalid", f"Could not read {source.name}.") from error
    scale = 1.0
    quality = 90
    temporary = destination.with_name(f".{destination.stem}-{uuid4().hex}{destination.suffix}")
    try:
        for _ in range(12):
            resized = []
            for frame in frames:
                width = max(1, int(frame.width * scale))
                height = max(1, int(frame.height * scale))
                item = frame if (width, height) == frame.size else frame.resize((width, height), Image.Resampling.LANCZOS)
                if save_format == "JPEG":
                    background = Image.new("RGB", item.size, "white")
                    background.paste(item, mask=item.getchannel("A"))
                    item = background
                resized.append(item)
            options = {"format": save_format, "optimize": True}
            if save_format in {"JPEG", "WEBP"}:
                options["quality"] = quality
            if save_format == "GIF" and len(resized) > 1:
                options.update(save_all=True, append_images=resized[1:], duration=durations, loop=loop)
            resized[0].save(temporary, **options)
            if temporary.stat().st_size <= limit:
                os.replace(temporary, destination)
                return
            temporary.unlink(missing_ok=True)
            if quality > 45 and save_format in {"JPEG", "WEBP"}:
                quality -= 10
            else:
                scale *= 0.82
        raise CollectionFailure(
            "exports.file_size_exceeded",
            f"{source.name} could not be reduced below {limit / 1048576:.0f} MB.",
        )
    finally:
        temporary.unlink(missing_ok=True)


def _safe_name(name):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return safe[:60] or "collection"


def _export_run_id(connection, job_id):
    return connection.execute(
        "SELECT id FROM export_runs WHERE job_id=?", (job_id,)
    ).fetchone()[0]


def _fail_export(connection, job_id, code, message):
    connection.rollback()
    connection.execute(
        "UPDATE export_runs SET status='failed', error_code=?, error_message=?, "
        "finished_at=CURRENT_TIMESTAMP WHERE job_id=?", (code, message, job_id)
    )
    connection.execute(
        "UPDATE background_jobs SET status='failed', error_code=?, error_message=?, "
        "finished_at=CURRENT_TIMESTAMP WHERE id=?", (code, message, job_id)
    )
    connection.commit()


def ensure_crop_eligible(connection, settings, media_ids):
    if not settings.crop_review_enabled:
        return
    from jiffle.features.crop_editor.review import blocked_media
    blocked = blocked_media(connection, media_ids)
    if blocked:
        ids = [row["id"] for row in blocked]
        raise CollectionFailure(
            "collections.crop_review_required",
            "Crop review required for media " + ", ".join(f"#{value}" for value in ids) +
            ". Apply a crop or confirm No crop needed in Editor.",
            {"media_ids": ids, "items": blocked},
        )
