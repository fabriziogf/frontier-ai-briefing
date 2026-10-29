"""Fetcher interface shared by all source kinds."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any


class Fetcher(ABC):
    kind: str

    @abstractmethod
    def fetch(self, source: dict[str, Any], since: datetime) -> list[Any]:
        """Return RawItems published after `since`. Must not raise on a bad source."""
