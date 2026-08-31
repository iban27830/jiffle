import argparse
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
from urllib.parse import urlsplit

from jiffle import create_app
from jiffle.configuration.settings import Settings, persist_settings


@dataclass
class MigrationReport:
    legacy_media_rows: int = 0
    migrated_media: int = 0
    skipped_existing_media: int = 0
    missing_media_files: int = 0
    duplicate_source_urls: int = 0
    invalid_source_urls: int = 0
    legacy_collections: int = 0
    migrated_collections: int = 0
    missing_collection_members: int = 0
    migrated_deleted_signatures: int = 0
    migrated_preferred_tags: int = 0
    migrated_blocked_tags: int = 0
    migrated_tag_aliases: int = 0
    migrated_collection_presets: int = 0
    skipped_collection_presets: int = 0
    migrated_archived_exports: int = 0
    discarded_pending_uploads: int = 0
    discarded_unrecognized_uploads: int = 0
    backup_path: str | None = None
    dry_run: bool = True


def migrate_legacy(
    source_database: Path,
    source_media: Path,
    target_root: Path,
    dry_run: bool,
    source_config: Path | None = None,
    preferred_tags_file: Path | None = None,
    blocked_tags_file: Path | None = None,
) -> MigrationReport:
    source_database = source_database.resolve()
    source_media = source_media.resolve()
    target_root = target_root.resolve()
    target_database = target_root / "jiffle-v2.db"
    if source_database == target_database:
        raise ValueError("Source and target databases must be different files.")
    if not source_database.is_file():
        raise ValueError("Source database does not exist.")
    if not source_media.is_dir():
        raise ValueError("Source media directory does not exist.")

    source = sqlite3.connect(
        f"file:{source_database.as_posix()}?mode=ro", uri=True
    )
    source.row_factory = sqlite3.Row
    try:
        report = _inspect(source, source_media)
        report.dry_run = dry_run
        if dry_run:
            return report
        target_root.mkdir(parents=True, exist_ok=True)
        report.backup_path = str(_backup_database(source, target_root, source_database.name))
        source_config_path = source_config or source_database.parent / "config.json"
        legacy_export_root = _legacy_export_root(source_config_path, source_database.parent)
        settings = Settings(
            database_path=target_database,
            media_path=source_media,
            thumbnail_path=target_root / "thumbnails",
            import_staging_path=target_root / "import-staging",
            export_path=legacy_export_root or target_root / "exports",
        )
        settings = _legacy_runtime_settings(settings, source_config_path)
        create_app(settings)
        persist_settings(settings)
        _copy_data(source, source_media, settings, report, legacy_export_root)
        _copy_deleted_signatures(source, settings, report)
        _copy_tag_configuration(
            settings,
            report,
            source_config_path,
            preferred_tags_file or source_database.parent / "user_tags.txt",
            blocked_tags_file or source_database.parent / "blacklisted_tags.txt",
        )
        report_path = target_root / "migration-report.json"
        report_path.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
        return report
    finally:
        source.close()


def _inspect(source, source_media):
    report = MigrationReport()
    images = source.execute("SELECT id, source_url, file_path FROM images ORDER BY id").fetchall()
    report.legacy_media_rows = len(images)
    seen_urls = set()
    for row in images:
        path = _safe_source_path(source_media, row["file_path"])
        if path is None or not path.is_file():
            report.missing_media_files += 1
        url = row["source_url"]
        if url:
            if url in seen_urls:
                report.duplicate_source_urls += 1
            seen_urls.add(url)
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                report.invalid_source_urls += 1
    collections = source.execute("SELECT id, image_ids FROM collections").fetchall()
    report.legacy_collections = len(collections)
    image_ids = {int(row["id"]) for row in images}
    for collection in collections:
        for legacy_id in _legacy_ids(collection["image_ids"]):
            if legacy_id not in image_ids:
                report.missing_collection_members += 1
    report.discarded_pending_uploads = _table_count(source, "pending_uploads")
    report.discarded_unrecognized_uploads = _table_count(source, "unrecognized_uploads")
    return report


def _backup_database(source, target_root, source_name):
    backup_root = target_root / "migration-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    counter = 1
    destination = backup_root / f"{source_name}.backup-{counter}"
    while destination.exists():
        counter += 1
        destination = backup_root / f"{source_name}.backup-{counter}"
    backup = sqlite3.connect(destination)
    try:
        source.backup(backup)
    finally:
        backup.close()
    return destination


def _copy_data(source, source_media, settings, report, legacy_export_root):
    target = sqlite3.connect(settings.database_path)
    target.row_factory = sqlite3.Row
    target.execute("PRAGMA foreign_keys = ON")
    try:
        for row in source.execute("SELECT * FROM images ORDER BY id"):
            mapped = target.execute(
                "SELECT media_item_id FROM legacy_media_map WHERE legacy_id=?", (row["id"],)
            ).fetchone()
            if mapped:
                report.skipped_existing_media += 1
                continue
            source_path = _safe_source_path(source_media, row["file_path"])
            if source_path is None or not source_path.is_file():
                continue
            relative_path = source_path.relative_to(source_media).as_posix()
            try:
                with source_path.open("rb") as file:
                    digest = sha256()
                    for chunk in iter(lambda: file.read(1024 * 1024), b""):
                        digest.update(chunk)
                existing_hash = target.execute(
                    "SELECT id FROM media_items WHERE content_hash=?", (digest.hexdigest(),)
                ).fetchone()
                if existing_hash:
                    target.execute(
                        "INSERT INTO legacy_media_map (legacy_id, media_item_id) VALUES (?, ?)",
                        (row["id"], existing_hash[0]),
                    )
                    target.commit()
                    report.skipped_existing_media += 1
                    continue
                media_type = "video" if source_path.suffix.lower() in {".mp4", ".webm"} else "image"
                cursor = target.execute(
                    "INSERT INTO media_items "
                    "(file_path, media_type, source_url, author, domain, width, height, "
                    "file_size, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        relative_path, media_type, _row_value(row, "source_url"),
                        _row_value(row, "author"), _row_value(row, "domain"),
                        _positive_int(_row_value(row, "width")),
                        _positive_int(_row_value(row, "height")),
                        source_path.stat().st_size, digest.hexdigest(),
                    ),
                )
                media_id = int(cursor.lastrowid)
                target.execute(
                    "INSERT INTO legacy_media_map (legacy_id, media_item_id) VALUES (?, ?)",
                    (row["id"], media_id),
                )
                target.executemany(
                    "INSERT OR IGNORE INTO media_tags (media_item_id, tag) VALUES (?, ?)",
                    ((media_id, tag) for tag in str(_row_value(row, "tags") or "").split()),
                )
                source_url = _row_value(row, "source_url")
                if source_url and _valid_url(source_url):
                    target.execute(
                        "INSERT OR IGNORE INTO media_sources "
                        "(media_item_id, canonical_url, provider, author, domain) "
                        "VALUES (?, ?, 'legacy', ?, ?)",
                        (
                            media_id, source_url, _row_value(row, "author"),
                            _row_value(row, "domain") or "unknown",
                        ),
                    )
                target.commit()
                report.migrated_media += 1
            except Exception:
                target.rollback()
                raise
        _copy_collections(source, target, report)
        _copy_archived_exports(source, target, report, legacy_export_root)
    finally:
        target.close()


def _copy_collections(source, target, report):
    for row in source.execute("SELECT * FROM collections ORDER BY id"):
        mapped = target.execute(
            "SELECT collection_id FROM legacy_collection_map WHERE legacy_id=?", (row["id"],)
        ).fetchone()
        if mapped:
            continue
        base_name = str(row["name"] or f"Legacy collection {row['id']}").strip()
        name = base_name
        suffix = 2
        while target.execute("SELECT 1 FROM collections WHERE name=?", (name,)).fetchone():
            name = f"{base_name} ({suffix})"
            suffix += 1
        created_at = _row_value(row, "date_created")
        cursor = target.execute(
            "INSERT INTO collections (name, jiggie_url, created_at, updated_at) "
            "VALUES (?, ?, COALESCE(?, CURRENT_TIMESTAMP), COALESCE(?, CURRENT_TIMESTAMP))",
            (name, _row_value(row, "jiggie_url"), created_at, created_at),
        )
        collection_id = int(cursor.lastrowid)
        target.execute(
            "INSERT INTO legacy_collection_map (legacy_id, collection_id) VALUES (?, ?)",
            (row["id"], collection_id),
        )
        position = 0
        for legacy_id in _legacy_ids(row["image_ids"]):
            media = target.execute(
                "SELECT media_item_id FROM legacy_media_map WHERE legacy_id=?", (legacy_id,)
            ).fetchone()
            if media is None:
                continue
            target.execute(
                "INSERT OR IGNORE INTO collection_items "
                "(collection_id, media_item_id, position, added_at) "
                "VALUES (?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
                (collection_id, media[0], position, created_at),
            )
            position += 1
        target.commit()
        report.migrated_collections += 1


def _copy_archived_exports(source, target, report, legacy_export_root):
    if not _table_exists(source, "collections"):
        return
    columns = {row[1] for row in source.execute("PRAGMA table_info(collections)")}
    for row in source.execute("SELECT * FROM collections ORDER BY id"):
        if target.execute(
            "SELECT 1 FROM legacy_export_map WHERE legacy_id=?", (row["id"],)
        ).fetchone():
            continue
        name = str(_row_value(row, "name") or f"Legacy export {row['id']}").strip()
        destination = str((legacy_export_root / name).resolve()) if legacy_export_root else None
        cursor = target.execute(
            "INSERT INTO archived_exports "
            "(name, jiggie_url, tags, reported_count, destination_path, exported_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                name,
                _row_value(row, "jiggie_url") if "jiggie_url" in columns else None,
                _row_value(row, "tags") if "tags" in columns else None,
                _row_value(row, "count") if "count" in columns else None,
                destination,
                _row_value(row, "date_created") if "date_created" in columns else None,
            ),
        )
        export_id = int(cursor.lastrowid)
        target.execute(
            "INSERT INTO legacy_export_map (legacy_id, archived_export_id) VALUES (?, ?)",
            (row["id"], export_id),
        )
        position = 0
        for legacy_id in _legacy_ids(_row_value(row, "image_ids")):
            media = target.execute(
                "SELECT media_item_id FROM legacy_media_map WHERE legacy_id=?", (legacy_id,)
            ).fetchone()
            if media is None:
                continue
            target.execute(
                "INSERT OR IGNORE INTO archived_export_items "
                "(export_id, media_item_id, position) VALUES (?, ?, ?)",
                (export_id, media[0], position),
            )
            position += 1
        target.commit()
        report.migrated_archived_exports += 1


def _copy_deleted_signatures(source, settings, report):
    if not _table_exists(source, "deleted_images"):
        return
    target = sqlite3.connect(settings.database_path)
    try:
        for row in source.execute("SELECT source_url, file_hash FROM deleted_images"):
            source_url = row["source_url"] or None
            content_hash = row["file_hash"] or None
            if source_url:
                cursor = target.execute(
                    "INSERT OR IGNORE INTO blocked_media_signatures "
                    "(source_url, reason) VALUES (?, 'legacy_deleted')", (source_url,)
                )
                report.migrated_deleted_signatures += cursor.rowcount
            if content_hash:
                cursor = target.execute(
                    "INSERT OR IGNORE INTO blocked_media_signatures "
                    "(content_hash, reason) VALUES (?, 'legacy_deleted')", (content_hash,)
                )
                report.migrated_deleted_signatures += cursor.rowcount
        target.commit()
    finally:
        target.close()


def _copy_tag_configuration(settings, report, source_config, preferred_file, blocked_file):
    target = sqlite3.connect(settings.database_path)
    try:
        for disposition, path in (("preferred", preferred_file), ("blocked", blocked_file)):
            if not path.is_file():
                continue
            for tag in _tag_lines(path):
                existing = target.execute(
                    "SELECT disposition FROM tag_rules WHERE tag=?", (tag,)
                ).fetchone()
                if existing is None:
                    target.execute(
                        "INSERT INTO tag_rules (tag, disposition) VALUES (?, ?)",
                        (tag, disposition),
                    )
                    if disposition == "preferred":
                        report.migrated_preferred_tags += 1
                    else:
                        report.migrated_blocked_tags += 1
                elif existing[0] != disposition:
                    target.execute(
                        "UPDATE tag_rules SET disposition=? WHERE tag=?", (disposition, tag)
                    )
                    if disposition == "preferred":
                        report.migrated_preferred_tags += 1
                    else:
                        report.migrated_blocked_tags += 1
        if source_config.is_file():
            payload = json.loads(source_config.read_text(encoding="utf-8"))
            aliases = payload.get("aliases", {}) if isinstance(payload, dict) else {}
            if isinstance(aliases, dict):
                for canonical, values in aliases.items():
                    if not isinstance(canonical, str) or not isinstance(values, list):
                        continue
                    for alias in values:
                        if not isinstance(alias, str) or not alias.strip():
                            continue
                        cursor = target.execute(
                            "INSERT OR IGNORE INTO tag_aliases (canonical_tag, alias) VALUES (?, ?)",
                            (canonical.strip(), alias.strip()),
                        )
                        report.migrated_tag_aliases += cursor.rowcount
            presets = payload.get("presets", {}) if isinstance(payload, dict) else {}
            if isinstance(presets, dict):
                for name, expression in presets.items():
                    parsed = _legacy_preset(name, expression)
                    if parsed is None:
                        report.skipped_collection_presets += 1
                        continue
                    included, excluded = parsed
                    cursor = target.execute(
                        "INSERT OR IGNORE INTO collection_presets (name, requested_count) VALUES (?, 10)",
                        (name.strip(),),
                    )
                    if not cursor.rowcount:
                        continue
                    preset_id = int(cursor.lastrowid)
                    target.executemany(
                        "INSERT INTO collection_preset_tags (preset_id, tag, disposition) "
                        "VALUES (?, ?, ?)",
                        ((preset_id, tag, disposition) for disposition, values in
                         (("include", included), ("exclude", excluded)) for tag in values),
                    )
                    report.migrated_collection_presets += 1
        target.commit()
    finally:
        target.close()


def _tag_lines(path):
    return {
        line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _legacy_preset(name, expression):
    if not isinstance(name, str) or not name.strip() or not isinstance(expression, str):
        return None
    included = []
    excluded = []
    for raw in expression.split():
        value = raw[1:] if raw.startswith("-") else raw
        if not value or ":" in value:
            return None
        target = excluded if raw.startswith("-") else included
        normalized = value.strip().lower()
        if normalized not in target:
            target.append(normalized)
    return (included, excluded) if included else None


def _legacy_export_root(config_path, base_path):
    if not config_path.is_file():
        return None
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    raw = (payload.get("paths") or {}).get("collections") if isinstance(payload, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(raw)
    return path.resolve() if path.is_absolute() else (base_path / path).resolve()


def _legacy_runtime_settings(settings, config_path):
    if not config_path.is_file():
        return settings
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return settings
    auth = payload.get("auth") if isinstance(payload.get("auth"), dict) else {}
    danbooru = auth.get("danbooru.donmai.us", {})
    e621 = auth.get("e621.net / e926.net", {})
    gelbooru = auth.get("gelbooru.com", {})
    furaffinity = auth.get("furaffinity.net", {})
    values = {
        "max_items_per_author": _positive_int(payload.get("max_per_author")) or settings.max_items_per_author,
        "max_image_export_size_bytes": 50 * 1024 * 1024,
        "max_video_export_size_bytes": 50 * 1024 * 1024,
        "export_format_rules": {"gif": "mp4", "webm": "mp4"},
        "danbooru_login": _auth_value(danbooru, "login"),
        "danbooru_api_key": _auth_value(danbooru, "api_key"),
        "e621_login": _auth_value(e621, "login"),
        "e621_api_key": _auth_value(e621, "api_key"),
        "gelbooru_user_id": _auth_value(gelbooru, "login"),
        "gelbooru_api_key": _auth_value(gelbooru, "api_key"),
        "furaffinity_cookie_a": _auth_value(furaffinity, "login"),
        "furaffinity_cookie_b": _auth_value(furaffinity, "api_key"),
    }
    return replace(settings, **values)


def _auth_value(values, key):
    return _string_or_none(values.get(key)) if isinstance(values, dict) else None


def _string_or_none(value):
    return value if isinstance(value, str) and value.strip() else None


def _table_exists(connection, table_name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone() is not None


def _table_count(connection, table_name):
    if not _table_exists(connection, table_name):
        return 0
    return int(connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def _safe_source_path(root_path, stored_path):
    if not stored_path:
        return None
    root = root_path.resolve()
    candidate = (root / stored_path).resolve()
    return candidate if candidate.is_relative_to(root) else None


def _legacy_ids(raw):
    values = []
    for value in str(raw or "").split(","):
        value = value.strip()
        if value.isdigit():
            values.append(int(value))
    return values


def _valid_url(url):
    parsed = urlsplit(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _row_value(row, key):
    return row[key] if key in row.keys() else None


def _positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def main():
    parser = argparse.ArgumentParser(description="Migrate a copied legacy Jiffle library")
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--source-media", required=True, type=Path)
    parser.add_argument("--target-root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="Apply migration; default is dry-run")
    arguments = parser.parse_args()
    report = migrate_legacy(
        arguments.source_db, arguments.source_media,
        arguments.target_root, dry_run=not arguments.apply,
    )
    print(json.dumps(asdict(report), indent=2))


if __name__ == "__main__":
    main()
