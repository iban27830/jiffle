from dataclasses import dataclass
import json
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3

from jiffle.configuration.settings import Settings


class CollectionFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


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


def build_collection_preview(
    connection: sqlite3.Connection,
    included_tags: tuple[str, ...],
    excluded_tags: tuple[str, ...],
    requested_count: int,
    max_items_per_author: int,
    excluded_ids: tuple[int, ...] = (),
    variant_count: int = 32,
) -> CollectionComposition:
    if not included_tags:
        raise CollectionFailure("collections.tags_required", "At least one included tag is required.")
    if not 1 <= requested_count <= 1000:
        raise CollectionFailure("collections.invalid_count", "Requested count must be from 1 to 1000.")
    candidates = _matching_candidates(connection, included_tags, excluded_tags, excluded_ids)
    available_count = len(candidates)
    target = min(requested_count, available_count)
    histories = _collection_histories(connection, included_tags, excluded_tags)
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


def replace_collection_items(
    connection: sqlite3.Connection, collection_id: int, media_ids: list[int]
) -> None:
    _collection(connection, collection_id)
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
    if total_size > settings.max_export_size_bytes:
        violations.append("export_size_exceeded")
    if not rows:
        violations.append("collection_empty")
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


def run_export_job(
    database_path: Path, settings: Settings, job_id: int, collection_id: int
) -> None:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
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
        preview = preview_export(connection, settings, collection_id)
        if preview.violations:
            raise CollectionFailure(
                "exports.constraints_failed", ", ".join(preview.violations)
            )
        collection = _collection(connection, collection_id)
        rows = _collection_media(connection, collection_id)
        export_root = settings.resolved_export_path
        staging_root = export_root.parent / "export-staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        staging = staging_root / f"run-{job_id}.part"
        staging.mkdir()
        for index, row in enumerate(rows, start=1):
            source = _media_path(settings.media_path, row["file_path"])
            if source is None or not source.is_file():
                raise CollectionFailure(
                    "exports.media_missing", f"Media item {row['id']} is unavailable."
                )
            destination = staging / f"{index:04d}-{source.name}"
            shutil.copy2(source, destination)
            progress = 10 + int(80 * index / len(rows))
            connection.execute(
                "UPDATE background_jobs SET progress=? WHERE id=?", (progress, job_id)
            )
        manifest = {
            "collection_id": collection_id,
            "name": collection["name"],
            "items": [{
                "position": row["position"], "media_item_id": row["id"],
                "source_url": row["source_url"],
            } for row in rows],
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        export_root.mkdir(parents=True, exist_ok=True)
        destination = export_root / f"{collection_id}-{_safe_name(collection['name'])}-run-{job_id}"
        os.replace(staging, destination)
        staging = None
        result = json.dumps({
            "export_run_id": _export_run_id(connection, job_id),
            "destination": str(destination), "item_count": preview.item_count,
            "total_size": preview.total_size,
        })
        connection.execute(
            "UPDATE export_runs SET status='completed', destination_path=?, item_count=?, "
            "total_size=?, finished_at=CURRENT_TIMESTAMP WHERE job_id=?",
            (str(destination), preview.item_count, preview.total_size, job_id),
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
            shutil.rmtree(staging)
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


def _matching_candidates(connection, included_tags, excluded_tags, excluded_ids):
    aliases = {}
    for row in connection.execute("SELECT canonical_tag, alias FROM tag_aliases"):
        aliases.setdefault(row["canonical_tag"].lower(), set()).add(row["alias"].lower())

    def expanded(tag):
        value = str(tag).strip().lower()
        return {value, *aliases.get(value, set())}

    clauses = ["item.deleted_at IS NULL"]
    parameters: list[object] = []
    for tag in included_tags:
        values = expanded(tag)
        clauses.append("EXISTS (SELECT 1 FROM media_tags mt WHERE mt.media_item_id=item.id AND LOWER(mt.tag) IN (" + ",".join("?" for _ in values) + "))")
        parameters.extend(values)
    for tag in excluded_tags:
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM media_tags mt WHERE mt.media_item_id=item.id AND mt.tag=?)"
        )
        parameters.append(tag)
    if excluded_ids:
        clauses.append(f"item.id NOT IN ({','.join('?' for _ in excluded_ids)})")
        parameters.extend(excluded_ids)
    return [CollectionCandidate(
        id=row["id"], media_type=row["media_type"], author=row["author"],
        domain=row["domain"], width=row["width"], height=row["height"],
        usage_count=int(row["usage_count"]), last_used_at=row["last_used_at"],
    ) for row in connection.execute(
        "SELECT item.id, item.media_type, item.author, item.domain, item.width, item.height, "
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
