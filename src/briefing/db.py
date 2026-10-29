"""SQLite connection, schema setup, and queries (spec section 4).

Timestamps are stored as ISO 8601 UTC strings, so string comparison orders them correctly.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

import sqlite_vec

from briefing.models import RawItem


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def now_iso() -> str:
    return iso(datetime.now(UTC))


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


def sync_sources(conn: sqlite3.Connection, sources: list[dict[str, Any]]) -> None:
    """Mirror config/sources.yaml into the sources table."""
    for s in sources:
        conn.execute(
            """INSERT INTO sources (id, name, kind, url, tier, enabled) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, kind=excluded.kind,
                 url=excluded.url, tier=excluded.tier, enabled=excluded.enabled""",
            (s["id"], s["name"], s["kind"], s["url"], s["tier"], int(s.get("enabled", False))),
        )
    conn.commit()


def insert_item(conn: sqlite3.Connection, item: RawItem, status: str = "new") -> bool:
    """Insert if unseen. Returns True when a new row was written."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO items (id, source_id, url, canonical_url, title, author,
             published_at, fetched_at, item_type, raw_text, arxiv_id, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item.id,
            item.source_id,
            item.url,
            item.canonical_url,
            item.title,
            item.author,
            iso(item.published_at) if item.published_at else None,
            now_iso(),
            item.item_type,
            item.raw_text,
            item.arxiv_id,
            status,
        ),
    )
    return cur.rowcount == 1


def known_urls(conn: sqlite3.Connection, source_id: str) -> set[str]:
    rows = conn.execute("SELECT canonical_url FROM items WHERE source_id = ?", (source_id,))
    return {r["canonical_url"] for r in rows}


def has_items(conn: sqlite3.Connection, source_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM items WHERE source_id = ? LIMIT 1", (source_id,)).fetchone()
    return row is not None


@dataclass
class RunStats:
    stage: str
    items_in: int = 0
    items_out: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    error: str | None = None


@contextmanager
def track_run(conn: sqlite3.Connection, stage: str):
    """Record one stage run in `runs`. Errors are logged and re-raised."""
    stats = RunStats(stage)
    run_id = conn.execute(
        "INSERT INTO runs (stage, started_at) VALUES (?, ?)", (stage, now_iso())
    ).lastrowid
    conn.commit()
    try:
        yield stats
    except Exception as e:
        stats.error = f"{type(e).__name__}: {e}"
        raise
    finally:
        conn.execute(
            """UPDATE runs SET finished_at=?, items_in=?, items_out=?, tokens_in=?,
                 tokens_out=?, cost_usd=?, error=? WHERE id=?""",
            (
                now_iso(),
                stats.items_in,
                stats.items_out,
                stats.tokens_in,
                stats.tokens_out,
                round(stats.cost_usd, 6),
                stats.error,
                run_id,
            ),
        )
        conn.commit()


def compact(conn: sqlite3.Connection) -> None:
    """Reclaim space after deletes, so the DB committed to the data repo stays small."""
    conn.commit()
    conn.execute("VACUUM")


def spend_since(conn: sqlite3.Connection, since_iso: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM runs WHERE started_at >= ?", (since_iso,)
    ).fetchone()
    return float(row["c"])
