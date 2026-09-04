import sqlite3

from flask import current_app, g

from jiffle.configuration.settings import Settings


def connect_database(
    database_path,
    *,
    row_factory=sqlite3.Row,
) -> sqlite3.Connection:
    """Open a SQLite connection with the settings used by all app workers."""
    connection = sqlite3.connect(database_path, timeout=60)
    if row_factory is not None:
        connection.row_factory = row_factory
    connection.execute("PRAGMA busy_timeout = 60000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def get_database() -> sqlite3.Connection:
    if "jiffle_database" not in g:
        settings: Settings = current_app.config["JIFFLE_SETTINGS"]
        g.jiffle_database = connect_database(settings.database_path)
    return g.jiffle_database


def close_database(_error: BaseException | None = None) -> None:
    connection = g.pop("jiffle_database", None)
    if connection is not None:
        connection.close()
