"""Anthropic client wrapper: retries, structured output, token and cost logging to `runs`.

Model names and prices come from config/settings.yaml, never hardcoded.
"""

from dataclasses import dataclass
from importlib.resources import files
from typing import Any, TypeVar

import anthropic
import jinja2
import yaml
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


@dataclass
class Prompt:
    name: str
    version: int
    model: str  # a key into settings.models, e.g. "fast"
    template: str

    @property
    def tag(self) -> str:
        return f"{self.name}@{self.version}"

    def render(self, **kwargs: Any) -> str:
        env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
        return env.from_string(self.template).render(**kwargs)


def load_prompt(name: str) -> Prompt:
    """Read prompts/<name>.md: YAML front matter (name, version, model), then a Jinja body."""
    raw = files("briefing").joinpath("prompts", f"{name}.md").read_text()
    _, front, body = raw.split("---\n", 2)
    meta = yaml.safe_load(front)
    return Prompt(
        name=meta["name"],
        version=int(meta["version"]),
        model=meta["model"],
        template=body.lstrip("\n"),
    )


class LLM:
    """Tracks tokens and dollars across calls so each stage can log them."""

    def __init__(self, prices: dict[str, dict[str, float]], client: Any = None):
        self.client = client or anthropic.Anthropic(max_retries=4)
        self.prices = prices
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0

    def _charge(self, model: str, usage: Any) -> None:
        price = self.prices[model]
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        self.tokens_in += usage.input_tokens + cache_write + cache_read
        self.tokens_out += usage.output_tokens
        self.cost_usd += (
            usage.input_tokens * price["input"]
            + cache_write * price["input"] * 1.25
            + cache_read * price["input"] * 0.1
            + usage.output_tokens * price["output"]
        ) / 1e6

    def parse(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int = 4000,
        effort: str | None = None,
    ) -> T:
        kwargs: dict[str, Any] = {}
        if effort:
            kwargs["output_config"] = {"effort": effort}
        response = self.client.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            **kwargs,
        )
        self._charge(model, response.usage)
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise LLMError(f"refusal ({category})")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"hit max_tokens={max_tokens}")
        if response.parsed_output is None:
            raise LLMError("no parsed output")
        return response.parsed_output
