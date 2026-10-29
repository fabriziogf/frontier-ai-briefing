"""RSS/Atom fetcher using feedparser; article text via trafilatura."""

import calendar
from datetime import UTC, datetime
from typing import Any

import feedparser

from briefing.ingest.base import MIN_TEXT_CHARS, Fetcher, html_to_text
from briefing.models import RawItem, canonicalize_url


def entry_time(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), UTC)
    return None


class RssFetcher(Fetcher):
    kind = "rss"

    def fetch(self, source, since, known=None):
        known = known or set()
        feed = feedparser.parse(self.http.get(source["url"]).content)
        items = []
        for entry in feed.entries:
            link = entry.get("link")
            title = (entry.get("title") or "").strip()
            if not link or not title:
                continue
            published = entry_time(entry)
            if published and published < since:
                continue
            if canonicalize_url(link) in known:
                continue
            body = ""
            if entry.get("content"):
                body = html_to_text(entry.content[0].get("value"))
            if len(body) < MIN_TEXT_CHARS:
                body = max(body, html_to_text(entry.get("summary")), key=len)
            if len(body) < MIN_TEXT_CHARS:
                try:
                    doc = self.article(link)
                    if doc and doc.get("text"):
                        body = doc["text"]
                except Exception:
                    pass  # keep the feed summary
            items.append(
                RawItem(
                    source_id=source["id"],
                    url=link,
                    title=title,
                    item_type=source.get("item_type", "blog"),
                    author=entry.get("author"),
                    published_at=published,
                    raw_text=self.clip(body),
                )
            )
        return items
