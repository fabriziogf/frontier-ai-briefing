from datetime import UTC, datetime, timedelta

from briefing import db
from briefing.ingest import run_ingest
from briefing.ingest.arxiv import parse_arxiv_id
from briefing.ingest.rss import RssFetcher
from briefing.models import canonicalize_url, item_id
from tests.conftest import FakeHttp

LONG = "Body text. " * 60


def rss(*entries):
    items = "".join(
        f"<item><title>{t}</title><link>{link}</link>"
        + (f"<pubDate>{d:%a, %d %b %Y %H:%M:%S} +0000</pubDate>" if d else "")
        + f"<description>{LONG}</description></item>"
        for t, link, d in entries
    )
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{items}</channel></rss>'
    )


def test_canonicalize_url_strips_tracking_and_trailing_slash():
    a = canonicalize_url("http://www.Example.com/post/?utm_source=x&b=2&a=1#top")
    b = canonicalize_url("https://example.com/post?a=1&b=2")
    assert a == b == "https://example.com/post?a=1&b=2"
    assert item_id(a) == item_id(b)


def test_parse_arxiv_id_drops_version():
    assert parse_arxiv_id("http://arxiv.org/abs/2610.12345v2") == "2610.12345"


def test_rss_fetcher_skips_old_and_known_entries():
    now = datetime.now(UTC)
    http = FakeHttp(
        {
            "https://blog.test/feed": rss(
                ("New post", "https://blog.test/new", now),
                ("Old post", "https://blog.test/old", now - timedelta(days=30)),
                ("Seen post", "https://blog.test/seen", now),
            )
        }
    )
    src = {"id": "blog", "url": "https://blog.test/feed"}
    items = RssFetcher(http).fetch(src, now - timedelta(days=2), known={"https://blog.test/seen"})
    assert [i.title for i in items] == ["New post"]
    assert items[0].raw_text.startswith("Body text.")


def test_ingest_is_idempotent_and_survives_a_dead_source(conn, settings, sources):
    now = datetime.now(UTC)
    feed = rss(("A", "https://blog.test/a", now), ("B", "https://blog.test/b?utm_source=rss", now))
    cfg = {"sources": sources(("blog", "rss", 2), ("dead", "rss", 1))}
    cfg["sources"][0]["url"] = "https://blog.test/feed"
    http = FakeHttp({"https://blog.test/feed": feed})

    first = run_ingest(conn, http, cfg, settings)
    second = run_ingest(conn, http, cfg, settings)

    assert first["blog"] == {"fetched": 2, "new": 2}
    assert second["blog"]["new"] == 0
    assert "error" in first["dead"]
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    run = conn.execute("SELECT error FROM runs WHERE stage='ingest' ORDER BY id DESC").fetchone()
    assert "dead" in run["error"]


def test_undated_items_on_first_run_are_baseline(conn, settings, sources):
    feed = rss(("Undated", "https://blog.test/u", None))
    cfg = {"sources": sources(("blog", "rss", 2))}
    cfg["sources"][0]["url"] = "https://blog.test/feed"
    run_ingest(conn, FakeHttp({"https://blog.test/feed": feed}), cfg, settings)
    assert conn.execute("SELECT status FROM items").fetchone()["status"] == "dropped"

    feed2 = rss(("Undated 2", "https://blog.test/u2", None))
    run_ingest(conn, FakeHttp({"https://blog.test/feed": feed2}), cfg, settings)
    statuses = {r["title"]: r["status"] for r in conn.execute("SELECT title, status FROM items")}
    assert statuses["Undated 2"] == "new"


def test_spend_since_sums_run_costs(conn):
    with db.track_run(conn, "rank") as s:
        s.cost_usd = 0.25
    with db.track_run(conn, "rank") as s:
        s.cost_usd = 0.5
    assert db.spend_since(conn, "2000-01-01") == 0.75
