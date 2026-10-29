"""Local embeddings with sentence-transformers (default BAAI/bge-small-en-v1.5, 384 dims).

Install with `uv sync --extra embed`.
"""

from typing import Protocol

import numpy as np

# bge models expect this prefix on the query side of asymmetric retrieval.
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder(Protocol):
    dims: int

    def encode(self, texts: list[str], query: bool = False) -> np.ndarray:
        """Return L2-normalized float32 vectors, one row per text."""
        ...


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str, dims: int):
        from sentence_transformers import SentenceTransformer  # heavy import, keep lazy

        self.model = SentenceTransformer(model_name)
        self.dims = dims
        self.query_prefix = BGE_QUERY_PREFIX if "bge" in model_name.lower() else ""

    def encode(self, texts: list[str], query: bool = False) -> np.ndarray:
        if query:
            texts = [self.query_prefix + t for t in texts]
        vecs = self.model.encode(texts, normalize_embeddings=True, batch_size=64)
        return np.asarray(vecs, dtype=np.float32)


def item_text(title: str, raw_text: str | None) -> str:
    """What gets embedded for an item (spec 5.3): title plus the first 500 chars."""
    return f"{title}\n{(raw_text or '')[:500]}"
