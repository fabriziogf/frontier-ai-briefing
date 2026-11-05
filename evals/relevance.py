"""Relevance eval: precision@10, must_see recall, nDCG on owner labels (spec section 9.1).

Labels live in evals/data/relevance_labels.jsonl, one JSON object per line, keyed by the
cluster's canonical item id (stable across DB rebuilds). A later line for the same item wins.

Relevance is recomputed from the stored ranker outputs (theme scores, significance, source
tier) with the current settings.yaml and profile.yaml, so weight and threshold changes are
measured without new LLM calls. Prompt or model changes need a re-rank first.
"""

import json
import math
import random
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from briefing.rank import blend

LABELS = ("must_see", "nice", "skip")
GAIN = {"must_see": 2, "nice": 1, "skip": 0}
RELEVANT = {"must_see", "nice"}


# Labels


def load_labels(path: Path) -> dict[str, dict[str, Any]]:
    """item_id -> latest label record."""
    out: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                out[rec["item_id"]] = rec
    return out


def append_label(path: Path, row: sqlite3.Row | dict[str, Any], label: str) -> None:
    if label not in LABELS:
        raise ValueError(f"unknown label {label!r}")
    rec = {
        "item_id": row["item_id"],
        "cluster_id": row["cluster_id"],
        "title": row["title"],
        "url": row["url"],
        "source": row["source_name"],
        "label": label,
        "labeled_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def undo_last(path: Path) -> dict[str, Any] | None:
    """Drop the last label line; return it."""
    if not path.exists():
        return None
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    if not lines:
        return None
    path.write_text("".join(ln + "\n" for ln in lines[:-1]))
    return json.loads(lines[-1])


# Ranked clusters with relevance under the current config


def ranked_clusters(
    conn: sqlite3.Connection, profile: dict[str, Any], settings: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every ranked cluster, highest recomputed relevance first."""
    cfg = settings["ranking"]
    theme_weights = {t["id"]: t["weight"] for t in profile["themes"]}
    tier_bonus = {int(k): v for k, v in cfg["tier_bonus"].items()}
    rows = conn.execute(
        """SELECT r.cluster_id, r.theme_scores, r.significance, r.relevance AS stored,
                  r.ranked_at, r.prompt_version, i.id AS item_id, i.title, i.url, i.item_type,
                  i.published_at, i.raw_text, s.name AS source_name, s.tier
           FROM rankings r
           JOIN clusters c ON c.id = r.cluster_id
           JOIN items i ON i.id = c.canonical_item
           JOIN sources s ON s.id = i.source_id"""
    ).fetchall()
    out = []
    for r in rows:
        scores = {t: s for t, s in json.loads(r["theme_scores"]).items() if t in theme_weights}
        # The ranker's is_excluded flag is stored only as relevance 0 (rank.blend).
        relevance, _ = blend(
            scores,
            theme_weights,
            r["significance"],
            r["tier"],
            r["stored"] == 0,
            cfg["weights"],
            tier_bonus,
        )
        d = dict(r)
        d["relevance"] = relevance
        d["week"] = "{}-W{:02d}".format(*datetime.fromisoformat(r["ranked_at"]).isocalendar()[:2])
        out.append(d)
    out.sort(key=lambda d: d["relevance"], reverse=True)
    return out


def _top_k_by_week(clusters: list[dict[str, Any]], k: int) -> dict[str, list[dict[str, Any]]]:
    weeks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in clusters:  # already sorted by relevance
        if len(weeks[c["week"]]) < k:
            weeks[c["week"]].append(c)
    return dict(weeks)


# Sampling for labeling


def sample_for_labeling(
    clusters: list[dict[str, Any]],
    labeled: set[str],
    target: int,
    top_k: int,
    strata: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Unlabeled clusters to show, in shuffled order.

    First each week's top_k (so precision@k is fully labeled), then an even spread across
    relevance strata, round-robin over sources inside each stratum, until `target` labels exist.
    """
    rng = random.Random(seed)
    picked: dict[str, dict[str, Any]] = {}
    for top in _top_k_by_week(clusters, top_k).values():
        for c in top:
            if c["item_id"] not in labeled:
                picked[c["item_id"]] = c

    need = target - len(labeled) - len(picked)
    pool = [c for c in clusters if c["item_id"] not in labeled and c["item_id"] not in picked]
    if need > 0 and pool:
        size = math.ceil(len(pool) / strata)
        buckets = []
        for i in range(0, len(pool), size):
            by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for c in pool[i : i + size]:
                by_source[c["source_name"]].append(c)
            for items in by_source.values():
                rng.shuffle(items)
            order = sorted(by_source)
            rng.shuffle(order)
            rr = []
            while any(by_source[s] for s in order):
                rr += [by_source[s].pop() for s in order if by_source[s]]
            buckets.append(rr)
        while need > 0 and any(buckets):
            for b in buckets:
                if b and need > 0:
                    c = b.pop(0)
                    picked[c["item_id"]] = c
                    need -= 1

    out = list(picked.values())
    rng.shuffle(out)
    return out


# Metrics


def _dcg(gains: list[int]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def evaluate(
    clusters: list[dict[str, Any]],
    labels: dict[str, dict[str, Any]],
    threshold: float,
    top_k: int,
) -> dict[str, Any]:
    """Metrics on the labeled clusters. Clusters without a label are left out."""
    lab = [(c, labels[c["item_id"]]["label"]) for c in clusters if c["item_id"] in labels]
    counts = {k: sum(1 for _, lbl in lab if lbl == k) for k in LABELS}

    weeks = {}
    for week, top in sorted(_top_k_by_week(clusters, top_k).items()):
        judged = [labels[c["item_id"]]["label"] for c in top if c["item_id"] in labels]
        weeks[week] = {
            "labeled": len(judged),
            "of": len(top),
            "precision": sum(lbl in RELEVANT for lbl in judged) / len(judged) if judged else None,
        }
    scored = [w["precision"] for w in weeks.values() if w["precision"] is not None]

    must = [c for c, lbl in lab if lbl == "must_see"]
    above = [lbl for c, lbl in lab if c["relevance"] >= threshold]
    gains = [GAIN[lbl] for _, lbl in lab]  # lab keeps the relevance order of `clusters`
    ideal = _dcg(sorted(gains, reverse=True))

    return {
        "labeled": len(lab),
        "counts": counts,
        "precision_at_k": sum(scored) / len(scored) if scored else None,
        "precision_at_k_by_week": weeks,
        "must_see_recall": sum(c["relevance"] >= threshold for c in must) / len(must)
        if must
        else None,
        "precision_above_threshold": sum(lbl in RELEVANT for lbl in above) / len(above)
        if above
        else None,
        "above_threshold": len(above),
        "ndcg": _dcg(gains) / ideal if ideal else None,
        "missed_must_see": [
            {"title": c["title"], "relevance": c["relevance"]}
            for c in must
            if c["relevance"] < threshold
        ],
    }


def config_snapshot(profile: dict[str, Any], settings: dict[str, Any], clusters) -> dict:
    cfg = settings["ranking"]
    return {
        "daily_threshold": cfg["thresholds"]["daily"],
        "weights": cfg["weights"],
        "tier_bonus": cfg["tier_bonus"],
        "theme_weights": {t["id"]: t["weight"] for t in profile["themes"]},
        "prompt_versions": sorted({c["prompt_version"] for c in clusters}),
    }


def save_run(runs_dir: Path, result: dict[str, Any]) -> tuple[Path, dict[str, Any] | None]:
    """Write this run; return its path and the previous run, for deltas."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    previous = sorted(runs_dir.glob("relevance-*.json"))
    prev = json.loads(previous[-1].read_text()) if previous else None
    path = runs_dir / f"relevance-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return path, prev
