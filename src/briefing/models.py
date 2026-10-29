"""Pydantic models shared across stages (RawItem, Ranking, Explanation, ...)."""

import hashlib
import re
from datetime import datetime
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel

ItemType = Literal["paper", "blog", "news", "release", "repo"]

_TRACKING_PARAMS = re.compile(r"^(utm_|ref$|ref_src$|source$|fbclid$|gclid$|mc_)", re.I)


def canonicalize_url(url: str) -> str:
    """Strip tracking params, fragments, trailing slashes, and `www.`; force https."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k))
    )
    path = parts.path.rstrip("/") or ""
    return urlunsplit(("https", host, path, query, ""))


def item_id(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode()).hexdigest()


class RawItem(BaseModel):
    """One fetched item before it touches the database."""

    source_id: str
    url: str
    title: str
    item_type: ItemType
    author: str | None = None
    published_at: datetime | None = None
    raw_text: str | None = None
    arxiv_id: str | None = None

    @property
    def canonical_url(self) -> str:
        return canonicalize_url(self.url)

    @property
    def id(self) -> str:
        return item_id(self.canonical_url)
