"""TTS providers behind one interface (spec section 7.2)."""

from typing import Protocol


class TTSProvider(Protocol):
    def synthesize(self, text: str, voice: str) -> bytes:
        """Return audio bytes for `text`. Callers chunk at sentence boundaries."""
        ...
