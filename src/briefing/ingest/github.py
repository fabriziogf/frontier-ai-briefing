"""GitHub trending fetcher (Python, AI topics). Scrapes the trending page."""

from datetime import UTC, datetime
from urllib.parse import urljoin

import lxml.html

from briefing.ingest.base import Fetcher
from briefing.models import RawItem, canonicalize_url


class GithubTrendingFetcher(Fetcher):
    kind = "github"

    def fetch(self, source, since, known=None):
        known = known or set()
        tree = lxml.html.fromstring(self.http.get(f"{source['url']}?since=daily").text)
        items = []
        for row in tree.xpath("//article[contains(@class, 'Box-row')]"):
            hrefs = row.xpath(".//h2//a/@href")
            if not hrefs:
                continue
            url = urljoin("https://github.com", hrefs[0])
            name = hrefs[0].strip("/")
            desc = " ".join(t.strip() for t in row.xpath(".//p//text()")).strip()
            if not self.is_ai(f"{name} {desc}"):
                continue
            if canonicalize_url(url) in known:
                continue
            items.append(
                RawItem(
                    source_id=source["id"],
                    url=url,
                    title=name,
                    item_type="repo",
                    published_at=datetime.now(UTC),
                    raw_text=self.clip(desc),
                )
            )
        return items
