"""Hacker News front page fetcher (Algolia API), filtered to AI stories by keyword."""

from datetime import UTC, datetime

from briefing.ingest.base import Fetcher
from briefing.models import RawItem, canonicalize_url


class HnFetcher(Fetcher):
    kind = "hn"

    def fetch(self, source, since, known=None):
        known = known or set()
        hits = self.http.get(f"{source['url']}&hitsPerPage=50").json().get("hits", [])
        items = []
        for h in hits:
            title = (h.get("title") or "").strip()
            if not title or not self.is_ai(title):
                continue
            hn_url = f"https://news.ycombinator.com/item?id={h['objectID']}"
            url = h.get("url") or hn_url
            if canonicalize_url(url) in known:
                continue
            published = datetime.fromtimestamp(h["created_at_i"], UTC)
            if published < since:
                continue
            text = None
            try:
                doc = self.article(url)
                text = doc.get("text") if doc else None
            except Exception:
                pass
            items.append(
                RawItem(
                    source_id=source["id"],
                    url=url,
                    title=title,
                    item_type="news",
                    author=h.get("author"),
                    published_at=published,
                    raw_text=self.clip(text),
                )
            )
        return items
