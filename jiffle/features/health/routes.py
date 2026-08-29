from flask import Blueprint, jsonify

from jiffle.infrastructure.database.connection import get_database
from jiffle.infrastructure.database.migrations import current_schema_version

health_blueprint = Blueprint("health", __name__)


@health_blueprint.get("/api/v1/health")
def get_health():
    database = get_database()
    database.execute("SELECT 1").fetchone()
    return jsonify({
        "status": "ok",
        "database": {"schema_version": current_schema_version(database)},
    })
