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
