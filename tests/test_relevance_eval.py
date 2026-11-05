import copy
import json
import math

import pytest

from briefing import db
from evals import relevance

WEEK = "2026-11-03T12:00:00+00:00"  # 2026-W45


@pytest.fixture
def world(conn, sources):
    db.sync_sources(conn, sources(("lab", "rss", 1), ("blog", "rss", 2), ("agg", "rss", 3)))
    return conn


def add_ranked(conn, n, source="blog", ranked_at=WEEK, start=0):
    """n ranked clusters on the search_quality theme, theme score falling with k."""
    for k in range(start, start + n):
        iid, cid = f"i{k}", f"c{k}"
        conn.execute(
            """INSERT INTO items (id, source_id, url, canonical_url, title, fetched_at, item_type,
                 raw_text, cluster_id, status)
               VALUES (?, ?, ?, ?, ?, ?, 'paper', 'text', ?, 'ranked')""",
            (iid, source, f"https://x.test/{k}", f"https://x.test/{k}", f"Item {k}", WEEK, cid),
        )
        conn.execute(
            "INSERT INTO clusters (id, canonical_item, created_at) VALUES (?, ?, ?)",
            (cid, iid, WEEK),
        )
        conn.execute(
            """INSERT INTO rankings (cluster_id, theme_scores, significance, relevance, rationale,
                 why_it_matters, ranked_at, model, prompt_version)
               VALUES (?, ?, 0.5, 0.5, 'r', 'w', ?, 'm', 'rank_cluster@1')""",
            (cid, json.dumps({"search_quality": 1 - k / 100}), ranked_at),
        )
    conn.commit()


def test_labels_round_trip_latest_wins_and_undo(tmp_path):
    path = tmp_path / "labels.jsonl"
    row = {"item_id": "i1", "cluster_id": "c1", "title": "T", "url": "u", "source_name": "S"}
    relevance.append_label(path, row, "skip")
    relevance.append_label(path, row, "must_see")
    assert relevance.load_labels(path)["i1"]["label"] == "must_see"
    assert relevance.undo_last(path)["label"] == "must_see"
    assert relevance.load_labels(path)["i1"]["label"] == "skip"
    with pytest.raises(ValueError):
        relevance.append_label(path, row, "maybe")


def test_recomputed_relevance_follows_current_weights(world, profile, settings):
    add_ranked(world, 1, source="lab")
    add_ranked(world, 1, source="agg", start=1)
    base = relevance.ranked_clusters(world, profile, settings)
    assert [c["item_id"] for c in base] == ["i0", "i1"]
    # Stored relevance 0 means the ranker excluded it; recomputing keeps it at 0.
    world.execute("UPDATE rankings SET relevance = 0 WHERE cluster_id = 'c0'")
    after = relevance.ranked_clusters(world, profile, settings)
    assert [c["item_id"] for c in after] == ["i1", "i0"] and after[1]["relevance"] == 0

    heavy_tier = copy.deepcopy(settings)
    heavy_tier["ranking"]["weights"]["source_tier"] = 0.9
    world.execute("UPDATE rankings SET relevance = 0.5")
    c = {x["item_id"]: x for x in relevance.ranked_clusters(world, profile, heavy_tier)}
    assert c["i0"]["relevance"] - c["i1"]["relevance"] > 0.5  # tier 1 vs tier 3


def test_sample_covers_weekly_top_k_then_spreads_across_strata(world, profile, settings):
    add_ranked(world, 30, source="blog")
    add_ranked(world, 30, source="agg", start=30, ranked_at="2026-10-29T12:00:00+00:00")
    clusters = relevance.ranked_clusters(world, profile, settings)
    queue = relevance.sample_for_labeling(clusters, set(), target=30, top_k=5, strata=5, seed=1)
    ids = {c["item_id"] for c in queue}
    tops = relevance._top_k_by_week(clusters, 5)
    assert len(tops) == 2
    assert all(c["item_id"] in ids for top in tops.values() for c in top)
    assert len(queue) == 30
    rel = sorted(c["relevance"] for c in queue)
    assert rel[0] < clusters[-10]["relevance"]  # reaches the bottom of the ranking

    labeled = ids
    again = relevance.sample_for_labeling(clusters, labeled, target=30, top_k=5, strata=5, seed=1)
    assert again == []


def test_metrics_on_a_known_case(world, profile, settings):
    add_ranked(world, 4)
    clusters = relevance.ranked_clusters(world, profile, settings)
    rel = {c["item_id"]: c["relevance"] for c in clusters}
    threshold = (rel["i1"] + rel["i2"]) / 2  # i0, i1 above; i2, i3 below
    labels = {
        "i0": {"label": "must_see"},
        "i1": {"label": "skip"},
        "i2": {"label": "must_see"},
        "i3": {"label": "nice"},
    }
    m = relevance.evaluate(clusters, labels, threshold, top_k=2)
    assert m["counts"] == {"must_see": 2, "nice": 1, "skip": 1}
    assert m["precision_at_k"] == 0.5
    assert m["precision_at_k_by_week"]["2026-W45"] == {"labeled": 2, "of": 2, "precision": 0.5}
    assert m["must_see_recall"] == 0.5
    assert m["precision_above_threshold"] == 0.5
    assert [x["title"] for x in m["missed_must_see"]] == ["Item 2"]
    dcg = 2 + 0 / math.log2(3) + 2 / math.log2(4) + 1 / math.log2(5)
    ideal = 2 + 2 / math.log2(3) + 1 / math.log2(4)
    assert m["ndcg"] == pytest.approx(dcg / ideal)


def test_save_run_returns_previous_for_deltas(tmp_path):
    first, prev = relevance.save_run(tmp_path, {"metrics": {"ndcg": 0.5}})
    assert prev is None and first.exists()
    first.rename(tmp_path / "relevance-20000101T000000Z.json")
    _, prev = relevance.save_run(tmp_path, {"metrics": {"ndcg": 0.6}})
    assert prev["metrics"]["ndcg"] == 0.5
