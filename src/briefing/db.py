"""SQLite connection, schema setup, and queries (spec section 4)."""

import sqlite3
from importlib.resources import files
from pathlib import Path

import sqlite_vec


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the database with the sqlite-vec extension loaded."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables. Idempotent."""
    schema = files("briefing").joinpath("schema.sql").read_text()
    conn.executescript(schema)
    conn.commit()
