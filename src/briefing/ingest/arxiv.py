"""arXiv API fetcher. Volume control (the pre-filter in spec 5.1) happens in cluster.py,
because it needs embeddings.

The window is on submittedDate, and papers show up in the API a day or two after submission,
so the source's `lookback_days` should be about 4 to cover weekends. Re-fetching is harmless:
items are idempotent by URL.
"""

import re
from datetime import UTC, datetime

import feedparser

from briefing.ingest.base import Fetcher
from briefing.ingest.rss import entry_time
from briefing.models import RawItem

PAGE_SIZE = 200
MAX_RESULTS = 3000
_ID_RE = re.compile(r"arxiv\.org/abs/([^v\s]+)(v\d+)?$")


def parse_arxiv_id(entry_id: str) -> str | None:
    m = _ID_RE.search(entry_id)
    return m.group(1) if m else None


class ArxivFetcher(Fetcher):
    kind = "arxiv"

    def fetch(self, source, since, known=None):
        known = known or set()
        cats = "+OR+".join(f"cat:{c}" for c in source["categories"])
        start_s = since.astimezone(UTC).strftime("%Y%m%d%H%M")
        end_s = datetime.now(UTC).strftime("%Y%m%d%H%M")
        query = f"%28{cats}%29+AND+submittedDate:%5B{start_s}+TO+{end_s}%5D"
        items: list[RawItem] = []
        start = 0
        while start < MAX_RESULTS:
            url = (
                f"{source['url']}?search_query={query}&sortBy=submittedDate"
                f"&sortOrder=descending&start={start}&max_results={PAGE_SIZE}"
            )
            feed = feedparser.parse(self.http.get(url).content)
            for e in feed.entries:
                arxiv_id = parse_arxiv_id(e.get("id", ""))
                if not arxiv_id:
                    continue
                abs_url = f"https://arxiv.org/abs/{arxiv_id}"
                if abs_url in known:
                    continue
                authors = [a.get("name") for a in e.get("authors", []) if a.get("name")]
                author = ", ".join(authors[:5]) + (" et al." if len(authors) > 5 else "")
                items.append(
                    RawItem(
                        source_id=source["id"],
                        url=abs_url,
                        title=re.sub(r"\s+", " ", e.get("title", "")).strip(),
                        item_type="paper",
                        author=author or None,
                        published_at=entry_time(e),
                        raw_text=self.clip(re.sub(r"\s+", " ", e.get("summary", ""))),
                        arxiv_id=arxiv_id,
                    )
                )
            total = int(feed.feed.get("opensearch_totalresults", 0) or 0)
            start += PAGE_SIZE
            if start >= total or not feed.entries:
                break
        return items
