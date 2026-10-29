"""Hugging Face Daily Papers fetcher. Queries one day at a time across the lookback window."""

from datetime import UTC, datetime, timedelta

from briefing.ingest.base import Fetcher
from briefing.models import RawItem


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class HfPapersFetcher(Fetcher):
    kind = "hf_papers"

    def fetch(self, source, since, known=None):
        known = known or set()
        items = []
        day = since.astimezone(UTC).date()
        today = datetime.now(UTC).date()
        while day <= today:
            entries = self.http.get(f"{source['url']}?date={day.isoformat()}").json()
            for e in entries:
                paper = e.get("paper", {})
                arxiv_id = paper.get("id")
                if not arxiv_id:
                    continue
                url = f"https://huggingface.co/papers/{arxiv_id}"
                if url in known:
                    continue
                authors = [a.get("name") for a in paper.get("authors", []) if a.get("name")]
                author = ", ".join(authors[:5]) + (" et al." if len(authors) > 5 else "")
                items.append(
                    RawItem(
                        source_id=source["id"],
                        url=url,
                        title=(paper.get("title") or e.get("title") or "").strip(),
                        item_type="paper",
                        author=author or None,
                        published_at=_parse_ts(paper.get("publishedAt") or e.get("publishedAt")),
                        raw_text=self.clip(paper.get("summary") or e.get("summary")),
                        arxiv_id=arxiv_id,
                    )
                )
                known.add(url)
            day += timedelta(days=1)
        return items
