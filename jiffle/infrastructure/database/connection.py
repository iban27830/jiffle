import sqlite3

from flask import current_app, g

from jiffle.configuration.settings import Settings


def get_database() -> sqlite3.Connection:
    if "jiffle_database" not in g:
        settings: Settings = current_app.config["JIFFLE_SETTINGS"]
        connection = sqlite3.connect(settings.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        g.jiffle_database = connection
    return g.jiffle_database


def close_database(_error: BaseException | None = None) -> None:
    connection = g.pop("jiffle_database", None)
    if connection is not None:
        connection.close()
