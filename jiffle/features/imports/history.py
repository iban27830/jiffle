import json


def create_import_history(connection, job_id: int, details: dict) -> None:
    connection.execute(
        "INSERT INTO operation_history "
        "(event_type, entity_type, entity_id, details_json) "
        "VALUES ('import.pending', 'background_job', ?, ?)",
        (job_id, json.dumps(details)),
    )


def update_import_history(connection, job_id: int, outcome: str, details: dict) -> None:
    connection.execute(
        "UPDATE operation_history SET event_type=?, details_json=? "
        "WHERE entity_type='background_job' AND entity_id=?",
        (f"import.{outcome}", json.dumps(details), job_id),
    )


_REVIEW_OUTCOME_EVENTS = {
    "merged": "import.duplicate",
    "created": "import.accepted",
    "duplicate_pending": "import.accepted",
    "rejected": "import.rejected",
}


def finalize_review_history(
    connection,
    review_id: int,
    outcome: str,
    media_item_id: int | None = None,
    duplicate_of: int | None = None,
) -> None:
    """Make an import's history entry reflect how its Review card was resolved.

    A single-file import that ended in Review kept its ``import.review`` row
    forever, so the Import list showed "Waiting for review" even after the card
    was accepted or rejected.  This rewrites that row in place.  Multi-post
    (set) imports are skipped because one job owns a single set summary row and
    one post must not overwrite it.
    """
    event_type = _REVIEW_OUTCOME_EVENTS.get(outcome)
    if event_type is None:
        return
    row = connection.execute(
        "SELECT candidate.job_id AS job_id FROM review_items review "
        "JOIN import_candidates candidate ON candidate.id=review.import_candidate_id "
        "WHERE review.id=?",
        (review_id,),
    ).fetchone()
    if row is None:
        return
    job = connection.execute(
        "SELECT job_type FROM background_jobs WHERE id=?", (row["job_id"],)
    ).fetchone()
    if job is None or job["job_type"] != "import_resolve":
        return
    history = connection.execute(
        "SELECT id, details_json FROM operation_history "
        "WHERE entity_type='background_job' AND entity_id=? "
        "ORDER BY id DESC LIMIT 1",
        (row["job_id"],),
    ).fetchone()
    if history is None:
        return
    try:
        details = json.loads(history["details_json"] or "{}")
    except (TypeError, ValueError):
        details = {}
    if not isinstance(details, dict):
        details = {}
    details["review_item_id"] = int(review_id)
    details["review_outcome"] = outcome
    if media_item_id is not None:
        details["media_item_id"] = int(media_item_id)
    if duplicate_of is not None:
        details["duplicate_of"] = int(duplicate_of)
        if outcome == "duplicate_pending":
            details["possible_duplicate_of"] = int(duplicate_of)
    if outcome == "merged":
        details["merged"] = True
    connection.execute(
        "UPDATE operation_history SET event_type=?, details_json=? WHERE id=?",
        (event_type, json.dumps(details), history["id"]),
    )
