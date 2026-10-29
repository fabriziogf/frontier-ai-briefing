"""Fetcher for sites without a feed (scrape listing page, extract with trafilatura).

Each html source sets `link_pattern`, a regex matched against link paths on the listing page.
"""

import re
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import lxml.html

from briefing.ingest.base import Fetcher
from briefing.models import RawItem, canonicalize_url


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class HtmlFetcher(Fetcher):
    kind = "html"

    def listing_links(self, source) -> list[str]:
        resp = self.http.get(source["url"])
        tree = lxml.html.fromstring(resp.text)
        pattern = re.compile(source["link_pattern"])
        base_host = urlsplit(source["url"]).netloc
        seen, links = set(), []
        for href in tree.xpath("//a/@href"):
            url = urljoin(source["url"], href)
            parts = urlsplit(url)
            if parts.netloc != base_host or not pattern.search(parts.path):
                continue
            canon = canonicalize_url(url)
            if canon not in seen:
                seen.add(canon)
                links.append(url)
        return links[: source.get("max_links", 20)]

    def fetch(self, source, since, known=None):
        known = known or set()
        items = []
        for url in self.listing_links(source):
            if canonicalize_url(url) in known:
                continue
            try:
                doc = self.article(url)
            except Exception:
                continue  # one bad article never blocks the source
            if not doc or not doc.get("title"):
                continue
            published = _parse_date(doc.get("date"))
            if published and published < since:
                continue
            items.append(
                RawItem(
                    source_id=source["id"],
                    url=url,
                    title=doc["title"].strip(),
                    item_type=source.get("item_type", "blog"),
                    author=doc.get("author"),
                    published_at=published,
                    raw_text=self.clip(doc.get("text")),
                )
            )
        return items
