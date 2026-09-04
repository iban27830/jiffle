import json
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
import sqlite3
import time

from jiffle.configuration.settings import Settings
from jiffle.features.imports.history import create_import_history, update_import_history
from jiffle.features.imports.source_adapters.contracts import SourceMedia, SourceSet
from jiffle.features.imports.source_adapters.danbooru import SourceProviderFailure
from jiffle.features.imports.universal_import import run_universal_import_job


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

    def fetch_metadata(self, _url: str) -> SourceMedia:
        return self.source

    def can_handle(self, url: str) -> bool:
        return url == self.source.canonical_url


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
    providers=None,
) -> None:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    search_providers = tuple(providers or (provider,))
    started_at = time.perf_counter()
    try:
        _mark_running(connection, job_id)
        _save_partial(connection, job_id, "fetch_set", 0, 0, [], None, 0)
        fetch_started = time.perf_counter()
        source_set = provider.fetch_set(submitted_url)
        _store_metadata(connection, job_id, source_set)
        fetch_ms = _elapsed_ms(fetch_started)
        issues = [issue_to_dict(issue) for issue in source_set.issues]
        counts = {"accepted": 0, "duplicate": 0, "review": 0, "blocked": 0, "failed": len(issues)}
        total = len(source_set.post_ids)
        results: list[dict | None] = [None] * total
        issue_by_id = {str(issue["remote_id"]): issue for issue in issues}
        source_by_id = {str(source.remote_id): source for source in source_set.posts}
        for index, raw_post_id in enumerate(source_set.post_ids):
            post_id = str(raw_post_id)
            if post_id in issue_by_id:
                results[index] = {"post_id": post_id, "issue": issue_by_id[post_id]}
                continue
            if post_id not in source_by_id:
                issue = {
                    "remote_id": post_id,
                    "post_id": post_id,
                    "url": f"{source_set.canonical_url.split('/post_sets/')[0]}/posts/{post_id}",
                    "code": "import.source_post_unavailable",
                    "message": "The post was not returned by the source set.",
                }
                results[index] = {"post_id": post_id, "issue": issue}
                counts["failed"] += 1
        cancelled = _cancel_requested(connection, job_id)
        processed = sum(result is not None for result in results)
        _save_progress(connection, job_id, processed, counts, "importing_posts", [], None, fetch_ms, _elapsed_ms(started_at))
        active: dict[object, tuple[int, str, int, float]] = {}
        next_index = 0
        import_started = time.perf_counter()
        post_timings: list[dict[str, object]] = []
        last_completed_post_id = None
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="jiffle-set") as executor:
            while True:
                scheduled = False
                while not cancelled and len(active) < 4 and next_index < total:
                    while next_index < total and results[next_index] is not None:
                        next_index += 1
                    if next_index >= total:
                        break
                    if _cancel_requested(connection, job_id):
                        cancelled = True
                        break
                    post_id = str(source_set.post_ids[next_index])
                    source = source_by_id[post_id]
                    child_id = _create_child_job(connection, job_id, source.canonical_url)
                    connection.commit()
                    index = next_index
                    next_index += 1
                    future = executor.submit(
                        _run_child_import, database_path, settings, child_id,
                        source, search_providers, downloader,
                    )
                    active[future] = (index, post_id, child_id, time.perf_counter())
                    scheduled = True
                if scheduled:
                    _save_progress(
                        connection, job_id, processed, counts, "importing_posts",
                        [item[1] for item in active.values()], last_completed_post_id,
                        fetch_ms, _elapsed_ms(started_at),
                    )
                if _cancel_requested(connection, job_id):
                    cancelled = True
                if not active:
                    break
                done, _ = wait(tuple(active), return_when=FIRST_COMPLETED)
                for future in sorted(done, key=lambda item: active[item][0]):
                    index, post_id, child_id, child_started = active.pop(future)
                    try:
                        future.result()
                    except (sqlite3.Error, OSError) as error:
                        _fail_parent(
                            connection, job_id, "import.infrastructure_error",
                            "The set import stopped because the database or storage became unavailable.",
                        )
                        raise error
                    except Exception:
                        # The child records item-level failures on its own job.
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
                    child_result = _child_result(child)
                    if child and child["status"] == "failed":
                        code = child["error_code"] or "import.url_import_failed"
                        message = child["error_message"] or "The post could not be imported."
                        child_result["issue"] = {
                            "remote_id": post_id, "post_id": post_id,
                            "url": source_by_id[post_id].canonical_url,
                            "code": code, "message": message,
                        }
                        if code == "import.previously_deleted":
                            counts["blocked"] += 1
                            counts["failed"] = max(0, counts["failed"] - 1)
                    results[index] = child_result
                    last_completed_post_id = post_id
                    timing = {
                        "post_id": post_id,
                        "duration_ms": _elapsed_ms(child_started),
                        "outcome": outcome,
                    }
                    child_payload = child_result.get("result") or {}
                    resolution = child_payload.get("resolution") if isinstance(child_payload, dict) else None
                    if isinstance(resolution, dict) and resolution.get("resolution_method"):
                        timing["resolution_method"] = resolution["resolution_method"]
                    post_timings.append(timing)
                    processed = sum(result is not None for result in results)
                    _save_progress(
                        connection, job_id, processed, counts, "importing_posts",
                        [item[1] for item in active.values()], last_completed_post_id,
                        fetch_ms, _elapsed_ms(started_at),
                    )
        import_ms = _elapsed_ms(import_started)
        cancelled = cancelled or _cancel_requested(connection, job_id)
        remaining = sum(result is None for result in results)
        ordered_issues = [result["issue"] for result in results if result and result.get("issue")]
        outcome = "cancelled" if cancelled else ("partial" if ordered_issues or remaining else "completed")
        finalizing_started = time.perf_counter()
        phases_ms = {"fetch_set": fetch_ms, "import_posts": import_ms, "finalizing": 0}
        result = {
            "outcome": outcome,
            "set": _set_metadata(source_set),
            **counts,
            "processed": processed,
            "total": total,
            "remaining": remaining,
            "issues": ordered_issues,
            "timing": {
                "duration_ms": _elapsed_ms(started_at),
                "phases_ms": phases_ms,
                "slowest_posts": sorted(post_timings, key=lambda item: item["duration_ms"], reverse=True)[:5],
            },
        }
        _save_partial(
            connection, job_id, "finalizing", processed, total, [],
            last_completed_post_id,
            _elapsed_ms(started_at),
        )
        phases_ms["finalizing"] = _elapsed_ms(finalizing_started)
        result["timing"]["duration_ms"] = _elapsed_ms(started_at)
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
    connection.execute(
        "INSERT INTO import_candidates (job_id, source_path, original_name, status) VALUES (?, ?, ?, 'pending')",
        (child_id, submitted_url, Path(submitted_url).name or "import"),
    )
    return child_id


def _run_child_import(database_path, settings, child_id, source, search_providers, downloader):
    return run_universal_import_job(
        database_path,
        settings,
        child_id,
        source.canonical_url,
        "url",
        (_StaticProvider(source), *search_providers),
        downloader,
    )


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


def _save_progress(
    connection: sqlite3.Connection,
    job_id: int,
    processed: int,
    counts: dict,
    phase: str = "importing_posts",
    active_post_ids=None,
    last_completed_post_id: str | None = None,
    fetch_ms: int = 0,
    elapsed_ms: int = 0,
) -> None:
    total = connection.execute("SELECT total FROM source_set_imports WHERE job_id=?", (job_id,)).fetchone()[0]
    progress = int(processed * 100 / total) if total else 100
    connection.execute(
        "UPDATE source_set_imports SET processed=?, accepted=?, duplicate=?, review=?, blocked=?, failed=?, updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
        (processed, counts["accepted"], counts["duplicate"], counts["review"], counts["blocked"], counts["failed"], job_id),
    )
    connection.execute(
        "UPDATE background_jobs SET progress=?, result_json=? WHERE id=?",
        (progress, json.dumps({
            "phase": phase,
            "processed": processed,
            "total": total,
            "active_post_ids": list(active_post_ids or []),
            "last_completed_post_id": last_completed_post_id,
            "elapsed_ms": elapsed_ms,
            "timing": {"phases_ms": {"fetch_set": fetch_ms}},
        }), job_id),
    )
    connection.commit()


def _save_partial(connection, job_id: int, phase: str, processed: int, total: int,
                  active_post_ids, last_completed_post_id, elapsed_ms: int) -> None:
    connection.execute(
        "UPDATE background_jobs SET result_json=? WHERE id=?",
        (json.dumps({
            "phase": phase,
            "processed": processed,
            "total": total,
            "active_post_ids": list(active_post_ids or []),
            "last_completed_post_id": last_completed_post_id,
            "elapsed_ms": elapsed_ms,
            "timing": {"phases_ms": {}},
        }), job_id),
    )
    connection.commit()


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.perf_counter() - started) * 1000)))


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


def _child_result(row) -> dict:
    if row is None:
        return {"outcome": "failed", "result": {}}
    payload = {}
    if row["result_json"]:
        try:
            payload = json.loads(row["result_json"])
        except (TypeError, ValueError):
            payload = {}
    return {"outcome": payload.get("outcome", _child_outcome(row)), "result": payload}


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
