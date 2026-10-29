"""Daily markdown digest grouped by theme (spec section 5.6).

Feedback links (spec 8) are added in M4.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from briefing.db import iso, now_iso
from briefing.rank import _clamp


def _primary_theme(scores: dict[str, float], weights: dict[str, float]) -> str:
    return max(scores, key=lambda t: _clamp(scores[t]) * weights.get(t, 0))


def select_clusters(
    conn: sqlite3.Connection, digest_id: str, threshold: float, max_items: int, max_age_days: int
) -> tuple[list[sqlite3.Row], str]:
    """Clusters ranked since the previous daily digest and never sent before."""
    prev = conn.execute(
        """SELECT created_at FROM digests WHERE kind = 'daily' AND id != ?
           ORDER BY created_at DESC LIMIT 1""",
        (digest_id,),
    ).fetchone()
    window_start = prev["created_at"] if prev else iso(datetime.now(UTC) - timedelta(hours=36))
    sent: set[str] = set()
    for d in conn.execute(
        "SELECT cluster_ids FROM digests WHERE kind = 'daily' AND id != ?", (digest_id,)
    ):
        sent.update(json.loads(d["cluster_ids"]))
    oldest = iso(datetime.now(UTC) - timedelta(days=max_age_days))
    rows = conn.execute(
        """SELECT r.cluster_id, r.relevance, r.theme_scores, r.why_it_matters,
                  i.title, i.url, i.item_type, s.name AS source_name
           FROM rankings r
           JOIN clusters c ON c.id = r.cluster_id
           JOIN items i ON i.id = c.canonical_item
           JOIN sources s ON s.id = i.source_id
           WHERE r.ranked_at >= ? AND r.relevance >= ?
             AND COALESCE(i.published_at, i.fetched_at) >= ?
           ORDER BY r.relevance DESC""",
        (window_start, threshold, oldest),
    ).fetchall()
    return [r for r in rows if r["cluster_id"] not in sent][:max_items], window_start


def scan_counts(conn: sqlite3.Connection, window_start: str) -> tuple[int, int]:
    """(items scanned, items folded into an existing cluster) since window_start."""
    scanned = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE fetched_at >= ?", (window_start,)
    ).fetchone()["n"]
    dups = conn.execute(
        """SELECT COUNT(*) AS n FROM items i JOIN clusters c ON c.id = i.cluster_id
           WHERE i.fetched_at >= ? AND c.canonical_item != i.id""",
        (window_start,),
    ).fetchone()["n"]
    return scanned, dups


def render(
    rows: list[sqlite3.Row],
    profile: dict[str, Any],
    day: datetime,
    scanned: int,
    dups: int,
    notes: list[str],
) -> str:
    weights = {t["id"]: t["weight"] for t in profile["themes"]}
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(_primary_theme(json.loads(r["theme_scores"]), weights), []).append(r)
    lines = [f"# AI Briefing: {day:%a %b} {day.day}", ""]
    if not rows:
        lines += ["Nothing cleared the bar today.", ""]
    for theme in profile["themes"]:
        items = groups.get(theme["id"])
        if not items:
            continue
        lines += [f"## {theme['name']}", ""]
        for r in items:
            lines.append(f"**[{r['title']}]({r['url']})**: {r['source_name']} · {r['item_type']}  ")
            lines.append(f"Why it matters: {r['why_it_matters']}")
            lines.append("")
    footer = f"{dups} items dropped as duplicates · {scanned} items scanned"
    lines += ["---", footer]
    lines += notes
    return "\n".join(lines).rstrip() + "\n"


def write_daily(
    conn: sqlite3.Connection,
    profile: dict[str, Any],
    settings: dict[str, Any],
    output_dir: Path,
    notes: list[str] | None = None,
) -> tuple[Path, int]:
    tz = ZoneInfo(settings["schedule"]["timezone"])
    day = datetime.now(tz)
    digest_id = f"daily-{day:%Y-%m-%d}"
    cfg = settings["digest"]
    rows, window_start = select_clusters(
        conn,
        digest_id,
        settings["ranking"]["thresholds"]["daily"],
        cfg["max_items"],
        cfg["max_age_days"],
    )
    scanned, dups = scan_counts(conn, window_start)
    markdown = render(rows, profile, day, scanned, dups, notes or [])
    conn.execute(
        """INSERT OR REPLACE INTO digests (id, kind, markdown, cluster_ids, created_at)
           VALUES (?, 'daily', ?, ?, ?)""",
        (digest_id, markdown, json.dumps([r["cluster_id"] for r in rows]), now_iso()),
    )
    conn.commit()
    path = output_dir / "daily" / f"{day:%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown)
    return path, len(rows)
