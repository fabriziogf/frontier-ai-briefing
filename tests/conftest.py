import json

import numpy as np
import pytest

from briefing import db
from briefing.config import load_yaml


class FakeResponse:
    def __init__(self, body: str | bytes | dict | list):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        self.content = body.encode() if isinstance(body, str) else body
        self.text = self.content.decode()

    def json(self):
        return json.loads(self.text)


class FakeHttp:
    """Maps URL prefixes to canned bodies. Unknown URLs raise, like a dead host."""

    def __init__(self, routes: dict[str, str | bytes | dict | list]):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str):
        self.calls.append(url)
        for prefix, body in self.routes.items():
            if url.startswith(prefix):
                return FakeResponse(body)
        raise ConnectionError(url)


class FakeEmbedder:
    """Deterministic vectors: texts sharing a key word land on the same axis."""

    dims = 8

    def __init__(self, axes: dict[str, int]):
        self.axes = axes

    def encode(self, texts, query=False):
        out = np.zeros((len(texts), self.dims), dtype=np.float32)
        for row, text in enumerate(texts):
            for word, axis in self.axes.items():
                if word in text.lower():
                    out[row, axis] += 1.0
            if not out[row].any():
                out[row, self.dims - 1] = 1.0
            out[row] /= np.linalg.norm(out[row])
        return out


class FakeLLM:
    """Returns a scripted output per title; counts cost like the real wrapper."""

    def __init__(self, outputs: dict[str, dict]):
        self.outputs = outputs
        self.cost_usd = 0.0
        self.tokens_in = self.tokens_out = 0
        self.calls: list[str] = []

    def parse(self, *, model, system, user, schema, max_tokens, effort=None):
        title = user.split("\n", 1)[0].removeprefix("Title: ")
        self.calls.append(title)
        self.cost_usd += 0.001
        return schema.model_validate(self.outputs[title])


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    # Small vec table for the fake 8-dim embedder.
    c.execute("DROP TABLE item_embeddings")
    c.execute(
        "CREATE VIRTUAL TABLE item_embeddings"
        " USING vec0(item_id TEXT PRIMARY KEY, embedding FLOAT[8])"
    )
    return c


@pytest.fixture
def profile():
    return load_yaml("profile.yaml")


@pytest.fixture
def settings():
    return load_yaml("settings.yaml")


@pytest.fixture
def sources():
    def make(*specs):
        return [
            {
                "id": sid,
                "name": sid.title(),
                "kind": kind,
                "url": f"https://{sid}.test/feed",
                "tier": tier,
                "enabled": True,
            }
            for sid, kind, tier in specs
        ]

    return make
