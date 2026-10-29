"""Relevance ranking: one structured LLM call per cluster, then the blended score.

See spec section 5.5.
"""

import json
import sqlite3
from typing import Any

from pydantic import BaseModel, Field, create_model

from briefing.db import now_iso
from briefing.llm import LLM, LLMError, Prompt


def rank_schema(themes: list[dict[str, Any]]) -> type[BaseModel]:
    """Output schema with one explicit score field per theme in the profile."""
    ThemeScores = create_model(
        "ThemeScores",
        **{t["id"]: (float, Field(description=f"0 to 1: {t['name']}")) for t in themes},
    )

    class RankOutput(BaseModel):
        theme_scores: ThemeScores  # type: ignore[valid-type]
        significance: float = Field(description="0 to 1: how big a deal for the field")
        is_excluded: bool
        rationale: str
        why_it_matters: str

    return RankOutput


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def blend(
    theme_scores: dict[str, float],
    theme_weights: dict[str, float],
    significance: float,
    tier: int,
    is_excluded: bool,
    weights: dict[str, float],
    tier_bonus: dict[int, float],
    feedback_affinity: float = 0.0,
) -> tuple[float, str]:
    """Return (relevance, primary theme id). Feedback affinity stays 0 until M4."""
    weighted = {t: _clamp(s) * theme_weights[t] for t, s in theme_scores.items()}
    primary = max(weighted, key=weighted.get)
    if is_excluded:
        return 0.0, primary
    relevance = (
        weights["theme"] * weighted[primary]
        + weights["significance"] * _clamp(significance)
        + weights["source_tier"] * tier_bonus[tier]
        + weights["feedback_affinity"] * _clamp(feedback_affinity)
    )
    return round(relevance, 4), primary


def _user_message(row: sqlite3.Row, max_chars: int) -> str:
    text = (row["raw_text"] or "")[:max_chars]
    return (
        f"Title: {row['title']}\n"
        f"Source: {row['source_name']} (tier {row['tier']})\n"
        f"Type: {row['item_type']}\n"
        f"Published: {row['published_at'] or 'unknown'}\n"
        f"URL: {row['url']}\n\n"
        f"{text}"
    )


def rank_clusters(
    conn: sqlite3.Connection,
    llm: LLM,
    prompt: Prompt,
    profile: dict[str, Any],
    settings: dict[str, Any],
    budget_left: float,
) -> dict[str, Any]:
    """Rank every unranked cluster. Stops early when the day's budget runs out."""
    cfg = settings["ranking"]
    model = settings["models"][prompt.model]
    themes = profile["themes"]
    theme_weights = {t["id"]: t["weight"] for t in themes}
    tier_bonus = {int(k): v for k, v in cfg["tier_bonus"].items()}
    schema = rank_schema(themes)
    system = prompt.render(
        owner_context=profile["owner_context"].strip(),
        themes=themes,
        exclude_topics=profile.get("exclude_topics", []),
    )
    rows = conn.execute(
        """SELECT c.id AS cluster_id, i.title, i.url, i.item_type, i.published_at, i.raw_text,
                  s.name AS source_name, s.tier
           FROM clusters c
           JOIN items i ON i.id = c.canonical_item
           JOIN sources s ON s.id = i.source_id
           WHERE c.id NOT IN (SELECT cluster_id FROM rankings) AND i.status = 'new'
           ORDER BY COALESCE(i.published_at, i.fetched_at) DESC"""
    ).fetchall()
    start_cost = llm.cost_usd
    ranked, failed, budget_hit = 0, [], False
    for r in rows:
        if llm.cost_usd - start_cost >= budget_left:
            budget_hit = True
            break
        try:
            out = llm.parse(
                model=model,
                system=system,
                user=_user_message(r, cfg["input_chars"]),
                schema=schema,
                max_tokens=cfg["max_tokens"],
                effort=cfg.get("effort"),
            )
        except LLMError as e:
            failed.append(f"{r['cluster_id'][:8]}: {e}")
            continue
        scores = out.theme_scores.model_dump()
        relevance, _ = blend(
            scores,
            theme_weights,
            out.significance,
            r["tier"],
            out.is_excluded,
            cfg["weights"],
            tier_bonus,
        )
        conn.execute(
            """INSERT INTO rankings (cluster_id, theme_scores, significance, relevance, rationale,
                 why_it_matters, ranked_at, model, prompt_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r["cluster_id"],
                json.dumps({k: round(_clamp(v), 3) for k, v in scores.items()}),
                round(_clamp(out.significance), 3),
                relevance,
                out.rationale.strip(),
                out.why_it_matters.strip(),
                now_iso(),
                model,
                prompt.tag,
            ),
        )
        conn.commit()
        ranked += 1
    # Items in ranked clusters, including late duplicates, are done.
    conn.execute(
        """UPDATE items SET status = 'ranked'
           WHERE status = 'new' AND cluster_id IN (SELECT cluster_id FROM rankings)"""
    )
    conn.commit()
    return {"clusters": len(rows), "ranked": ranked, "failed": failed, "budget_hit": budget_hit}
