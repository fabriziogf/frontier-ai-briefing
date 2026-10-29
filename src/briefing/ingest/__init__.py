"""Fetchers, one per source kind (spec section 5.2), and the ingest stage that runs them."""

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from briefing import db
from briefing.http import Http
from briefing.ingest.arxiv import ArxivFetcher
from briefing.ingest.base import Fetcher
from briefing.ingest.github import GithubTrendingFetcher
from briefing.ingest.hf_papers import HfPapersFetcher
from briefing.ingest.hn import HnFetcher
from briefing.ingest.html import HtmlFetcher
from briefing.ingest.rss import RssFetcher

FETCHERS: dict[str, type[Fetcher]] = {
    f.kind: f
    for f in (
        RssFetcher,
        HtmlFetcher,
        ArxivFetcher,
        HfPapersFetcher,
        HnFetcher,
        GithubTrendingFetcher,
    )
}


def run_ingest(
    conn: sqlite3.Connection,
    http: Http,
    sources_cfg: dict[str, Any],
    settings: dict[str, Any],
    only: set[str] | None = None,
) -> dict[str, Any]:
    """Fetch every enabled source. A failed source is reported, never fatal."""
    ingest_cfg = settings["ingest"]
    db.sync_sources(conn, sources_cfg["sources"])
    report: dict[str, Any] = {}
    with db.track_run(conn, "ingest") as stats:
        for source in sources_cfg["sources"]:
            if not source.get("enabled") or (only and source["id"] not in only):
                continue
            fetcher = FETCHERS[source["kind"]](
                http,
                max_chars=ingest_cfg["raw_text_max_chars"],
                ai_keywords=sources_cfg.get("ai_keywords"),
            )
            lookback = source.get("lookback_days", ingest_cfg["lookback_days"])
            since = datetime.now(UTC) - timedelta(days=lookback)
            first_run = not db.has_items(conn, source["id"])
            try:
                items = fetcher.fetch(source, since, known=db.known_urls(conn, source["id"]))
            except Exception as e:
                report[source["id"]] = {"error": f"{type(e).__name__}: {e}"}
                continue
            new = 0
            for item in items:
                # Undated items on a source's first run are a baseline, not news.
                status = "dropped" if first_run and item.published_at is None else "new"
                new += db.insert_item(conn, item, status=status)
            conn.commit()
            stats.items_in += len(items)
            stats.items_out += new
            report[source["id"]] = {"fetched": len(items), "new": new}
        errors = [k for k, v in report.items() if "error" in v]
        if errors:
            stats.error = "failed sources: " + ", ".join(errors)
    return report
