import json
from pathlib import Path
import sqlite3

from jiffle.configuration.settings import Settings
from jiffle.features.imports.history import create_import_history, update_import_history
from jiffle.features.imports.source_adapters.contracts import SourceMedia, SourceSet
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.features.imports.url_import import run_url_import_job


class ActiveSetImportError(Exception):
    def __init__(self, job_id: int):
        super().__init__("A set import is already running.")
        self.job_id = job_id


class _StaticProvider:
    def __init__(self, source: SourceMedia):
        self.source = source
        self.provider_name = source.provider

    def fetch(self, _url: str) -> SourceMedia:
        return self.source


def create_set_import_job(connection: sqlite3.Connection, submitted_url: str, provider) -> int:
    active = connection.execute(
        "SELECT b.id FROM background_jobs b JOIN source_set_imports s ON s.job_id=b.id "
        "WHERE b.status IN ('pending','running') ORDER BY b.id DESC LIMIT 1"
    ).fetchone()
    if active:
        raise ActiveSetImportError(int(active[0]))
    provider_name = getattr(provider, "provider_name", "e621")
    cursor = connection.execute(
        "INSERT INTO background_jobs (job_type, status) VALUES ('source_set_import', 'pending')"
    )
    job_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO source_set_imports (job_id, submitted_url, provider) VALUES (?, ?, ?)",
        (job_id, submitted_url, provider_name),
    )
    create_import_history(connection, job_id, {"url": submitted_url, "provider": provider_name, "set": True})
    connection.commit()
    return job_id


def run_set_import_job(
    database_path: Path,
    settings: Settings,
    job_id: int,
    submitted_url: str,
    provider,
    downloader,
) -> None:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        _mark_running(connection, job_id)
        source_set = provider.fetch_set(submitted_url)
        _store_metadata(connection, job_id, source_set)
        issues = [issue_to_dict(issue) for issue in source_set.issues]
        counts = {"accepted": 0, "duplicate": 0, "review": 0, "blocked": 0, "failed": len(issues)}
        processed = len(issues)
        _save_progress(connection, job_id, processed, counts)
        issue_by_id = {issue["remote_id"]: issue for issue in issues}
        source_by_id = {source.remote_id: source for source in source_set.posts}
        cancelled = _cancel_requested(connection, job_id)

        for raw_post_id in source_set.post_ids:
            post_id = str(raw_post_id)
            if post_id in issue_by_id:
                continue
            if cancelled or _cancel_requested(connection, job_id):
                cancelled = True
                break
            source = source_by_id.get(post_id)
            if source is None:
                issue = {
                    "remote_id": str(post_id),
                    "post_id": str(post_id),
                    "url": f"{source_set.canonical_url.split('/post_sets/')[0]}/posts/{post_id}",
                    "code": "import.source_post_unavailable",
                    "message": "The post was not returned by the source set.",
                }
                issues.append(issue)
                counts["failed"] += 1
                processed += 1
                _save_progress(connection, job_id, processed, counts)
                continue
            child_id = _create_child_job(connection, job_id, source.canonical_url)
            connection.commit()
            try:
                run_url_import_job(
                    database_path, settings, child_id, source.canonical_url,
                    _StaticProvider(source), downloader,
                )
            except (sqlite3.Error, OSError) as error:
                _fail_parent(
                    connection, job_id, "import.infrastructure_error",
                    "The set import stopped because the database or storage became unavailable.",
                )
                raise error
            except Exception:
                # The single-post worker records source/download/media errors on
                # the child job. They are item-level failures and do not stop a set.
                pass
            child = connection.execute(
                "SELECT status, error_code, error_message, result_json FROM background_jobs WHERE id=?",
                (child_id,),
            ).fetchone()
            outcome = _child_outcome(child)
            if outcome in counts:
                counts[outcome] += 1
            elif outcome == "failed":
                counts["failed"] += 1
            if child and child["status"] == "failed":
                code = child["error_code"] or "import.url_import_failed"
                message = child["error_message"] or "The post could not be imported."
                issue = {
                    "remote_id": source.remote_id, "post_id": source.remote_id,
                    "url": source.canonical_url, "code": code, "message": message,
                }
                issues.append(issue)
                if code == "import.previously_deleted":
                    counts["blocked"] += 1
                    counts["failed"] = max(0, counts["failed"] - 1)
            processed += 1
            _save_progress(connection, job_id, processed, counts)

        if not cancelled and processed < len(source_set.post_ids):
            cancelled = _cancel_requested(connection, job_id)
        remaining = max(0, len(source_set.post_ids) - processed)
        outcome = "cancelled" if cancelled else ("partial" if issues or remaining else "completed")
        result = {
            "outcome": outcome,
            "set": _set_metadata(source_set),
            **counts,
            "processed": processed,
            "total": len(source_set.post_ids),
            "remaining": remaining,
            "issues": issues,
        }
        _complete_parent(connection, job_id, result)
    except SourceProviderFailure as error:
        _fail_parent(connection, job_id, error.code, error.message)
    except (sqlite3.Error, OSError):
        try:
            _fail_parent(
                connection, job_id, "import.infrastructure_error",
                "The set import stopped because the database or storage became unavailable.",
            )
        except Exception:
            pass
        raise
    except Exception:
        _fail_parent(connection, job_id, "import.set_import_failed", "The set could not be imported safely.")
    finally:
        connection.close()


def request_set_import_cancel(connection: sqlite3.Connection, job_id: int) -> bool:
    row = connection.execute(
        "SELECT b.status FROM background_jobs b JOIN source_set_imports s ON s.job_id=b.id WHERE b.id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        return False
    if row[0] in {"pending", "running"}:
        connection.execute("UPDATE source_set_imports SET cancel_requested=1, updated_at=CURRENT_TIMESTAMP WHERE job_id=?", (job_id,))
        connection.commit()
    return True


def active_set_job(connection: sqlite3.Connection):
    row = connection.execute(
        "SELECT b.*, s.processed, s.total, s.cancel_requested, s.name, s.shortname, "
        "s.set_id, s.provider, s.submitted_url, "
        "s.accepted, s.duplicate, s.review, s.blocked, s.failed "
        "FROM background_jobs b JOIN source_set_imports s ON s.job_id=b.id "
        "WHERE b.status IN ('pending','running') ORDER BY b.id DESC LIMIT 1"
    ).fetchone()
    return _job_payload(row) if row else None


def _job_payload(row):
    if row is None:
        return None
    result = json.loads(row["result_json"]) if row["result_json"] else None
    payload = {
        "id": row["id"], "type": row["job_type"], "status": row["status"],
        "progress": row["progress"], "result": result,
        "error": ({"code": row["error_code"], "message": row["error_message"], "details": {}}
                  if row["error_code"] else None),
        "created_at": row["created_at"], "started_at": row["started_at"], "finished_at": row["finished_at"],
    }
    if row["job_type"] == "source_set_import":
        payload.update({
            "processed": row["processed"], "total": row["total"],
            "cancel_requested": bool(row["cancel_requested"]),
            "set": {
                "id": row["set_id"], "name": row["name"],
                "shortname": row["shortname"], "provider": row["provider"],
                "url": row["submitted_url"],
            },
        })
    return payload


def _create_child_job(connection, parent_id: int, submitted_url: str) -> int:
    cursor = connection.execute(
        "INSERT INTO background_jobs (job_type, status, parent_job_id) VALUES ('url_import', 'pending', ?)",
        (parent_id,),
    )
    child_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO url_import_candidates (job_id, submitted_url, status) VALUES (?, ?, 'pending')",
        (child_id, submitted_url),
    )
    return child_id


def _mark_running(connection, job_id: int) -> None:
    connection.execute("UPDATE background_jobs SET status='running', started_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
    connection.commit()


def _store_metadata(connection, job_id: int, source_set: SourceSet) -> None:
    metadata = _set_metadata(source_set)
    connection.execute(
        "UPDATE source_set_imports SET provider=?, set_id=?, shortname=?, name=?, metadata_json=?, total=?, updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
        (source_set.provider, source_set.remote_id, source_set.shortname, source_set.name, json.dumps(metadata), len(source_set.post_ids), job_id),
    )
    connection.commit()


def _save_progress(connection, job_id: int, processed: int, counts: dict) -> None:
    total = connection.execute("SELECT total FROM source_set_imports WHERE job_id=?", (job_id,)).fetchone()[0]
    progress = int(processed * 100 / total) if total else 100
    connection.execute(
        "UPDATE source_set_imports SET processed=?, accepted=?, duplicate=?, review=?, blocked=?, failed=?, updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
        (processed, counts["accepted"], counts["duplicate"], counts["review"], counts["blocked"], counts["failed"], job_id),
    )
    connection.execute("UPDATE background_jobs SET progress=? WHERE id=?", (progress, job_id))
    connection.commit()


def _complete_parent(connection, job_id: int, result: dict) -> None:
    connection.execute(
        "UPDATE background_jobs SET status='completed', progress=100, result_json=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
        (json.dumps(result), job_id),
    )
    update_import_history(connection, job_id, f"set_{result['outcome']}", result)
    connection.commit()


def _fail_parent(connection, job_id: int, code: str, message: str) -> None:
    connection.rollback()
    connection.execute(
        "UPDATE background_jobs SET status='failed', error_code=?, error_message=?, result_json=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
        (code, message, json.dumps({"outcome": "failed", "code": code, "message": message}), job_id),
    )
    update_import_history(connection, job_id, "failed", {"outcome": "failed", "code": code, "message": message})
    connection.commit()


def _cancel_requested(connection, job_id: int) -> bool:
    row = connection.execute("SELECT cancel_requested FROM source_set_imports WHERE job_id=?", (job_id,)).fetchone()
    return bool(row and row[0])


def _child_outcome(row) -> str:
    if row is None:
        return "failed"
    if row["result_json"]:
        try:
            return json.loads(row["result_json"]).get("outcome", "failed")
        except (TypeError, ValueError):
            pass
    return "failed" if row["status"] == "failed" else row["status"]


def issue_to_dict(issue) -> dict:
    return {
        "remote_id": issue.remote_id, "post_id": issue.remote_id,
        "url": issue.canonical_url, "code": issue.code, "message": issue.message,
    }


def _set_metadata(source_set: SourceSet) -> dict:
    return {
        "url": source_set.canonical_url, "provider": source_set.provider,
        "id": source_set.remote_id, "name": source_set.name,
        "shortname": source_set.shortname,
    }
