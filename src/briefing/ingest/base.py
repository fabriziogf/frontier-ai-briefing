"""Fetcher interface shared by all source kinds, plus text helpers."""

import re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import lxml.html
import trafilatura

from briefing.http import Http
from briefing.models import RawItem

# Feed summaries shorter than this get the full article fetched instead.
MIN_TEXT_CHARS = 500


class Fetcher(ABC):
    kind: str

    def __init__(self, http: Http, max_chars: int = 20000, ai_keywords: list[str] | None = None):
        self.http = http
        self.max_chars = max_chars
        self._ai_re = _keyword_regex(ai_keywords or [])

    @abstractmethod
    def fetch(
        self, source: dict[str, Any], since: datetime, known: set[str] | None = None
    ) -> list[RawItem]:
        """Return items published after `since`. `known` holds canonical URLs already stored,
        so fetchers can skip expensive per-item requests."""

    def clip(self, text: str | None) -> str | None:
        if not text:
            return None
        text = re.sub(r"\s+\n", "\n", text).strip()
        return text[: self.max_chars] or None

    def is_ai(self, text: str) -> bool:
        return bool(self._ai_re and self._ai_re.search(text))

    def article(self, url: str) -> dict[str, Any] | None:
        """Fetch a page and extract main text plus metadata with trafilatura."""
        resp = self.http.get(url)
        doc = trafilatura.bare_extraction(resp.text, url=url, with_metadata=True, as_dict=True)
        return doc or None


def html_to_text(html: str | None) -> str:
    if not html or not html.strip():
        return ""
    try:
        return lxml.html.fromstring(html).text_content().strip()
    except (lxml.etree.ParserError, ValueError):
        return html


def _keyword_regex(words: list[str]) -> re.Pattern | None:
    if not words:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)
