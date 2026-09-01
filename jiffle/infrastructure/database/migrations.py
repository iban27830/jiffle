from collections.abc import Callable
import json
import sqlite3
from urllib.parse import urlsplit, urlunsplit

from jiffle.infrastructure.database.connection import get_database

Migration = tuple[int, Callable[[sqlite3.Connection], None]]


def migration_1(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE schema_versions ("
        "version INTEGER PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )


def migration_2(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE media_items ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "file_path TEXT NOT NULL UNIQUE, "
        "media_type TEXT NOT NULL CHECK (media_type IN ('image', 'video')), "
        "source_url TEXT, "
        "author TEXT, "
        "domain TEXT, "
        "width INTEGER CHECK (width IS NULL OR width > 0), "
        "height INTEGER CHECK (height IS NULL OR height > 0), "
        "file_size INTEGER CHECK (file_size IS NULL OR file_size >= 0), "
        "content_hash TEXT UNIQUE, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "CREATE TABLE media_tags ("
        "media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE, "
        "tag TEXT NOT NULL, "
        "PRIMARY KEY (media_item_id, tag))"
    )
    connection.execute("CREATE INDEX media_items_author_idx ON media_items(author)")
    connection.execute("CREATE INDEX media_items_domain_idx ON media_items(domain)")
    connection.execute("CREATE INDEX media_items_type_idx ON media_items(media_type)")
    connection.execute("CREATE INDEX media_tags_tag_idx ON media_tags(tag)")


def migration_3(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE background_jobs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "job_type TEXT NOT NULL, "
        "status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')), "
        "progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100), "
        "result_json TEXT, "
        "error_code TEXT, "
        "error_message TEXT, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "started_at TEXT, "
        "finished_at TEXT)"
    )
    connection.execute(
        "CREATE TABLE import_candidates ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "job_id INTEGER NOT NULL UNIQUE REFERENCES background_jobs(id), "
        "source_path TEXT NOT NULL, "
        "stored_path TEXT, "
        "original_name TEXT NOT NULL, "
        "media_type TEXT, "
        "content_hash TEXT, "
        "width INTEGER, "
        "height INTEGER, "
        "file_size INTEGER, "
        "status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'review', 'duplicate', 'failed')), "
        "media_item_id INTEGER REFERENCES media_items(id), "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "CREATE TABLE review_items ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "import_candidate_id INTEGER NOT NULL UNIQUE REFERENCES import_candidates(id), "
        "reason TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected')), "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "resolved_at TEXT)"
    )
    connection.execute(
        "CREATE INDEX background_jobs_status_idx ON background_jobs(status)"
    )
    connection.execute(
        "CREATE INDEX import_candidates_hash_idx ON import_candidates(content_hash)"
    )


def migration_4(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE media_sources ("
        "media_item_id INTEGER PRIMARY KEY REFERENCES media_items(id) ON DELETE CASCADE, "
        "canonical_url TEXT NOT NULL UNIQUE, "
        "direct_media_url TEXT, "
        "provider TEXT NOT NULL, "
        "remote_id TEXT, "
        "author TEXT, "
        "domain TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE url_import_candidates ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "job_id INTEGER NOT NULL UNIQUE REFERENCES background_jobs(id), "
        "submitted_url TEXT NOT NULL, "
        "canonical_url TEXT, "
        "provider TEXT, "
        "status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'duplicate', 'failed')), "
        "media_item_id INTEGER REFERENCES media_items(id), "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "CREATE INDEX media_sources_provider_remote_idx "
        "ON media_sources(provider, remote_id)"
    )


def migration_5(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE operation_history ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_type TEXT NOT NULL, "
        "entity_type TEXT NOT NULL, "
        "entity_id INTEGER NOT NULL, "
        "details_json TEXT NOT NULL DEFAULT '{}', "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "CREATE INDEX operation_history_entity_idx "
        "ON operation_history(entity_type, entity_id, id DESC)"
    )


def migration_6(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE media_items ADD COLUMN deleted_at TEXT")
    connection.execute(
        "CREATE TABLE media_fingerprints ("
        "media_item_id INTEGER PRIMARY KEY REFERENCES media_items(id) ON DELETE CASCADE, "
        "perceptual_hash TEXT NOT NULL, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "CREATE TABLE duplicate_matches ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "left_media_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE, "
        "right_media_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE, "
        "match_method TEXT NOT NULL CHECK (match_method IN ('exact', 'perceptual')), "
        "confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 100), "
        "status TEXT NOT NULL DEFAULT 'pending' "
        "CHECK (status IN ('pending', 'ignored', 'resolved')), "
        "resolution TEXT, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "resolved_at TEXT, "
        "CHECK (left_media_id < right_media_id), "
        "UNIQUE (left_media_id, right_media_id, match_method))"
    )
    connection.execute(
        "CREATE INDEX duplicate_matches_status_idx ON duplicate_matches(status, id)"
    )


def migration_7(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE tag_suggestions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "job_id INTEGER NOT NULL UNIQUE REFERENCES background_jobs(id), "
        "media_item_id INTEGER NOT NULL REFERENCES media_items(id), "
        "provider TEXT NOT NULL, "
        "known_tags_json TEXT NOT NULL, "
        "unknown_tags_json TEXT NOT NULL, "
        "diagnostic_json TEXT NOT NULL DEFAULT '{}', "
        "status TEXT NOT NULL DEFAULT 'pending' "
        "CHECK (status IN ('pending', 'accepted', 'rejected')), "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "resolved_at TEXT)"
    )
    connection.execute(
        "CREATE INDEX tag_suggestions_status_idx ON tag_suggestions(status, id)"
    )


def migration_8(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE collections ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL UNIQUE, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "CREATE TABLE collection_items ("
        "collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE, "
        "media_item_id INTEGER NOT NULL REFERENCES media_items(id), "
        "position INTEGER NOT NULL CHECK (position >= 0), "
        "PRIMARY KEY (collection_id, media_item_id), "
        "UNIQUE (collection_id, position))"
    )
    connection.execute(
        "CREATE TABLE export_runs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "job_id INTEGER NOT NULL UNIQUE REFERENCES background_jobs(id), "
        "collection_id INTEGER REFERENCES collections(id) ON DELETE SET NULL, "
        "collection_name TEXT NOT NULL, "
        "status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')), "
        "destination_path TEXT, "
        "item_count INTEGER NOT NULL DEFAULT 0, "
        "total_size INTEGER NOT NULL DEFAULT 0, "
        "error_code TEXT, "
        "error_message TEXT, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "finished_at TEXT)"
    )


def migration_9(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE legacy_media_map ("
        "legacy_id INTEGER PRIMARY KEY, "
        "media_item_id INTEGER NOT NULL UNIQUE REFERENCES media_items(id), "
        "migrated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "CREATE TABLE legacy_collection_map ("
        "legacy_id INTEGER PRIMARY KEY, "
        "collection_id INTEGER NOT NULL UNIQUE REFERENCES collections(id), "
        "migrated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )


def migration_10(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE blocked_media_signatures ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "source_url TEXT UNIQUE, "
        "content_hash TEXT UNIQUE, "
        "reason TEXT NOT NULL DEFAULT 'deleted', "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "CHECK (source_url IS NOT NULL OR content_hash IS NOT NULL))"
    )
    connection.execute(
        "CREATE TABLE tag_rules ("
        "tag TEXT PRIMARY KEY, "
        "disposition TEXT NOT NULL CHECK (disposition IN ('preferred', 'blocked')), "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "CREATE TABLE tag_aliases ("
        "canonical_tag TEXT NOT NULL, "
        "alias TEXT NOT NULL UNIQUE, "
        "PRIMARY KEY (canonical_tag, alias))"
    )
    connection.execute(
        "CREATE TABLE archived_exports ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL, "
        "jiggie_url TEXT, "
        "tags TEXT, "
        "reported_count INTEGER, "
        "destination_path TEXT, "
        "exported_at TEXT, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "CREATE TABLE archived_export_items ("
        "export_id INTEGER NOT NULL REFERENCES archived_exports(id) ON DELETE CASCADE, "
        "media_item_id INTEGER NOT NULL REFERENCES media_items(id), "
        "position INTEGER NOT NULL CHECK (position >= 0), "
        "PRIMARY KEY (export_id, media_item_id), "
        "UNIQUE (export_id, position))"
    )
    connection.execute(
        "CREATE TABLE legacy_export_map ("
        "legacy_id INTEGER PRIMARY KEY, "
        "archived_export_id INTEGER NOT NULL UNIQUE REFERENCES archived_exports(id), "
        "migrated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )


def migration_11(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE collections ADD COLUMN jiggie_url TEXT")


def migration_12(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE collection_items ADD COLUMN added_at TEXT")
    connection.execute(
        "UPDATE collection_items SET added_at=(SELECT created_at FROM collections "
        "WHERE collections.id=collection_items.collection_id) WHERE added_at IS NULL"
    )
    connection.execute("ALTER TABLE collections ADD COLUMN requested_count INTEGER")
    connection.execute(
        "CREATE TABLE collection_tags ("
        "collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE, "
        "tag TEXT NOT NULL, "
        "disposition TEXT NOT NULL CHECK (disposition IN ('include', 'exclude')), "
        "PRIMARY KEY (collection_id, tag, disposition))"
    )
    connection.execute(
        "CREATE TABLE collection_presets ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL UNIQUE, "
        "requested_count INTEGER NOT NULL CHECK (requested_count > 0), "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "CREATE TABLE collection_preset_tags ("
        "preset_id INTEGER NOT NULL REFERENCES collection_presets(id) ON DELETE CASCADE, "
        "tag TEXT NOT NULL, "
        "disposition TEXT NOT NULL CHECK (disposition IN ('include', 'exclude')), "
        "PRIMARY KEY (preset_id, tag, disposition))"
    )


def migration_13(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE import_candidates ADD COLUMN source_metadata_json TEXT")

    # Repair URL imports that were accepted from Review before source metadata
    # was retained on the candidate.
    rows = connection.execute(
        "SELECT candidate.media_item_id, url.submitted_url "
        "FROM import_candidates candidate "
        "JOIN url_import_candidates url ON url.job_id=candidate.job_id "
        "WHERE candidate.status='accepted' AND candidate.media_item_id IS NOT NULL"
    ).fetchall()
    for media_item_id, submitted_url in rows:
        parsed = urlsplit(submitted_url)
        canonical_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
        )
        source = connection.execute(
            "SELECT media_item_id, canonical_url, direct_media_url, provider, "
            "remote_id, author, domain FROM media_sources WHERE canonical_url=?",
            (canonical_url,),
        ).fetchone()
        if source is None or int(source[0]) == int(media_item_id):
            continue
        if connection.execute(
            "SELECT 1 FROM media_sources WHERE media_item_id=?", (media_item_id,)
        ).fetchone():
            continue
        old_media_item_id = int(source[0])
        old_item = connection.execute(
            "SELECT deleted_at FROM media_items WHERE id=?", (old_media_item_id,)
        ).fetchone()
        if old_item is None or old_item[0] is None:
            continue
        connection.execute(
            "UPDATE media_items SET source_url=?, author=?, domain=? WHERE id=?",
            (source[1], source[5], source[6], media_item_id),
        )
        connection.execute(
            "INSERT OR IGNORE INTO media_tags (media_item_id, tag) "
            "SELECT ?, tag FROM media_tags WHERE media_item_id=?",
            (media_item_id, old_media_item_id),
        )
        connection.execute(
            "UPDATE media_sources SET media_item_id=? WHERE media_item_id=?",
            (media_item_id, old_media_item_id),
        )


def migration_14(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE media_items ADD COLUMN active_revision_id INTEGER")
    connection.execute(
        "CREATE TABLE media_revisions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE, "
        "parent_revision_id INTEGER REFERENCES media_revisions(id), file_path TEXT NOT NULL UNIQUE, "
        "operation TEXT NOT NULL, width INTEGER, height INTEGER, file_size INTEGER, content_hash TEXT, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "CREATE TABLE crop_analyses ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE, "
        "revision_id INTEGER NOT NULL REFERENCES media_revisions(id) ON DELETE CASCADE, method TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','cropped','no_crop_needed','deferred','failed')), "
        "left_px INTEGER NOT NULL DEFAULT 0, top_px INTEGER NOT NULL DEFAULT 0, right_px INTEGER NOT NULL, bottom_px INTEGER NOT NULL, "
        "confidence REAL NOT NULL DEFAULT 0, removed_area REAL NOT NULL DEFAULT 0, parameters_json TEXT NOT NULL DEFAULT '{}', "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, resolved_at TEXT, UNIQUE(revision_id, method))"
    )
    connection.execute("CREATE INDEX crop_analyses_status_idx ON crop_analyses(status, confidence DESC)")
    rows = connection.execute("SELECT id, file_path, width, height, file_size, content_hash FROM media_items WHERE deleted_at IS NULL").fetchall()
    for row in rows:
        cur = connection.execute(
            "INSERT INTO media_revisions (media_item_id, file_path, operation, width, height, file_size, content_hash) VALUES (?, ?, 'original', ?, ?, ?, ?)",
            (row[0], row[1], row[2], row[3], row[4], row[5]),
        )
        connection.execute("UPDATE media_items SET active_revision_id=? WHERE id=?", (cur.lastrowid, row[0]))


def migration_15(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE crop_scan_jobs (job_id INTEGER PRIMARY KEY REFERENCES background_jobs(id) ON DELETE CASCADE, "
        "cancel_requested INTEGER NOT NULL DEFAULT 0, scanned_count INTEGER NOT NULL DEFAULT 0, "
        "candidate_count INTEGER NOT NULL DEFAULT 0, parameters_json TEXT NOT NULL DEFAULT '{}')"
    )
    connection.execute("DROP TABLE IF EXISTS tag_suggestions")
    connection.execute("DELETE FROM operation_history WHERE event_type LIKE 'ai_tags.%'")
    connection.execute("DELETE FROM background_jobs WHERE job_type='ai_tagging'")


def migration_16(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE crop_scan_results ("
        "revision_id INTEGER NOT NULL REFERENCES media_revisions(id) ON DELETE CASCADE, "
        "parameter_signature TEXT NOT NULL, candidate_found INTEGER NOT NULL, "
        "scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "PRIMARY KEY(revision_id, parameter_signature))"
    )


def migration_17(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE media_revisions ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'")
    rows = connection.execute(
        "SELECT id, file_path, width, height, file_size, content_hash FROM media_items "
        "WHERE active_revision_id IS NULL"
    ).fetchall()
    for row in rows:
        cursor = connection.execute(
            "INSERT INTO media_revisions "
            "(media_item_id, file_path, operation, width, height, file_size, content_hash) "
            "VALUES (?, ?, 'original', ?, ?, ?, ?)",
            (row[0], row[1], row[2], row[3], row[4], row[5]),
        )
        connection.execute(
            "UPDATE media_items SET active_revision_id=? WHERE id=?",
            (cursor.lastrowid, row[0]),
        )
    history = connection.execute(
        "SELECT details_json FROM operation_history WHERE event_type='crop.applied' ORDER BY id"
    ).fetchall()
    for entry in history:
        try:
            details = json.loads(entry[0])
            revision_id = int(details["revision_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        analysis = connection.execute(
            "SELECT method, revision_id FROM crop_analyses WHERE id=?",
            (details.get("analysis_id"),),
        ).fetchone()
        connection.execute(
            "UPDATE media_revisions SET details_json=? WHERE id=? AND operation='crop'",
            (json.dumps({
                "analysis_id": details.get("analysis_id"),
                "box": details.get("box"),
                "method": analysis[0] if analysis else None,
                "source_revision_id": analysis[1] if analysis else None,
            }), revision_id),
        )


def migration_18(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT id, parent_revision_id, details_json FROM media_revisions WHERE operation='crop'"
    ).fetchall()
    for row in rows:
        try:
            details = json.loads(row[2] or "{}")
        except (TypeError, json.JSONDecodeError):
            details = {}
        analysis = None
        if details.get("analysis_id") is not None:
            analysis = connection.execute(
                "SELECT id, method, revision_id FROM crop_analyses WHERE id=?",
                (details["analysis_id"],),
            ).fetchone()
        if analysis is None and row[1] is not None:
            analysis = connection.execute(
                "SELECT id, method, revision_id FROM crop_analyses "
                "WHERE revision_id=? AND status='cropped' ORDER BY id DESC LIMIT 1",
                (row[1],),
            ).fetchone()
        if analysis:
            details.update({
                "analysis_id": analysis[0],
                "method": analysis[1],
                "source_revision_id": analysis[2],
            })
            connection.execute(
                "UPDATE media_revisions SET details_json=? WHERE id=?",
                (json.dumps(details), row[0]),
            )


def migration_19(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE collections ADD COLUMN preset_id INTEGER REFERENCES collection_presets(id) ON DELETE SET NULL")
    connection.execute("ALTER TABLE collections ADD COLUMN preset_name TEXT")
    connection.execute("ALTER TABLE collections ADD COLUMN preset_query TEXT")
    connection.execute("ALTER TABLE collections ADD COLUMN preset_requested_count INTEGER")


MIGRATIONS: tuple[Migration, ...] = (
    (1, migration_1),
    (2, migration_2),
    (3, migration_3),
    (4, migration_4),
    (5, migration_5),
    (6, migration_6),
    (7, migration_7),
    (8, migration_8),
    (9, migration_9),
    (10, migration_10),
    (11, migration_11),
    (12, migration_12),
    (13, migration_13),
    (14, migration_14),
    (15, migration_15),
    (16, migration_16),
    (17, migration_17),
    (18, migration_18),
    (19, migration_19),
)


def migrate_database() -> None:
    connection = get_database()
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = {
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_versions"
            )
        } if _table_exists(connection, "schema_versions") else set()

        for version, migration in MIGRATIONS:
            if version in existing:
                continue
            migration(connection)
            connection.execute(
                "INSERT INTO schema_versions (version) VALUES (?)", (version,)
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def current_schema_version(connection: sqlite3.Connection) -> int:
    if not _table_exists(connection, "schema_versions"):
        return 0
    row = connection.execute("SELECT MAX(version) FROM schema_versions").fetchone()
    return int(row[0] or 0)


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None
