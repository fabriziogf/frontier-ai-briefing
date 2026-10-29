from datetime import UTC, datetime, timedelta

import pytest

from briefing import cluster, db
from briefing.digest.daily import render, select_clusters, write_daily
from briefing.llm import load_prompt
from briefing.models import RawItem
from briefing.rank import blend, rank_clusters
from tests.conftest import FakeEmbedder, FakeLLM

AXES = {"rerank": 0, "agent": 1, "voice": 2, "retrieval": 3}


def add(conn, source_id, title, text, *, arxiv_id=None, item_type="blog", hours_ago=1):
    item = RawItem(
        source_id=source_id,
        url=f"https://{source_id}.test/{title.replace(' ', '-').lower()}",
        title=title,
        item_type=item_type,
        published_at=datetime.now(UTC) - timedelta(hours=hours_ago),
        raw_text=text,
        arxiv_id=arxiv_id,
    )
    db.insert_item(conn, item)
    return item.id


@pytest.fixture
def world(conn, sources):
    db.sync_sources(
        conn,
        sources(
            ("lab", "rss", 1), ("blog", "rss", 2), ("hf", "hf_papers", 1), ("arxiv", "arxiv", 1)
        ),
    )
    return conn


def scores(**kw):
    base = {
        t: 0.0
        for t in [
            "search_quality",
            "ai_overviews",
            "trust_safety",
            "agentic_platforms",
            "audio_media",
            "frontier_models",
        ]
    }
    return base | kw


def test_blend_matches_spec_formula(settings, profile):
    w = settings["ranking"]["weights"]
    tw = {t["id"]: t["weight"] for t in profile["themes"]}
    rel, primary = blend(
        scores(search_quality=0.8, agentic_platforms=0.9),
        tw,
        0.5,
        1,
        False,
        w,
        {1: 1.0, 2: 0.6, 3: 0.3},
    )
    # max(0.8*1.0, 0.9*0.9) = 0.81 -> 0.55*0.81 + 0.30*0.5 + 0.10*1.0
    assert primary == "agentic_platforms"
    assert rel == pytest.approx(0.55 * 0.81 + 0.15 + 0.10)
    assert blend(scores(search_quality=1.0), tw, 1.0, 1, True, w, {1: 1.0})[0] == 0.0


def test_near_duplicates_share_a_cluster_with_higher_tier_canonical(world):
    emb = FakeEmbedder(AXES)
    blog = add(world, "blog", "Rerank news", "small rerank model", hours_ago=1)
    lab = add(world, "lab", "Our rerank model", "rerank release", hours_ago=2)
    other = add(world, "blog", "Agent toolkit", "agent framework")
    cluster.embed_new(world, emb)
    result = cluster.cluster_new(world, threshold=0.85, window_days=7)

    rows = {r["id"]: r["cluster_id"] for r in world.execute("SELECT id, cluster_id FROM items")}
    assert rows[blog] == rows[lab] != rows[other]
    canonical = world.execute(
        "SELECT canonical_item FROM clusters WHERE id = ?", (rows[blog],)
    ).fetchone()[0]
    assert canonical == lab
    assert result == {"clustered": 3, "joined_existing": 1, "new_clusters": 2, "llm_checks": 0}


def test_same_arxiv_id_clusters_even_when_text_differs(world):
    emb = FakeEmbedder(AXES)
    a = add(world, "hf", "Paper on HF", "voice", arxiv_id="2610.1", item_type="paper")
    b = add(world, "arxiv", "Paper on arXiv", "retrieval", arxiv_id="2610.1", item_type="paper")
    cluster.embed_new(world, emb)
    cluster.cluster_new(world, threshold=0.85, window_days=7)
    ids = world.execute("SELECT cluster_id FROM items WHERE id IN (?, ?)", (a, b)).fetchall()
    assert ids[0][0] == ids[1][0]


def test_prefilter_keeps_hf_and_on_theme_papers(world, profile):
    emb = FakeEmbedder(AXES | {"search": 3, "ranking": 3, "biology": 6})
    on_hf = add(world, "arxiv", "Voice paper", "voice", arxiv_id="1", item_type="paper")
    add(world, "hf", "Voice paper", "voice", arxiv_id="1", item_type="paper")
    on_theme = add(world, "arxiv", "Dense retrieval", "retrieval", arxiv_id="2", item_type="paper")
    off = add(world, "arxiv", "Protein folding", "biology", arxiv_id="3", item_type="paper")
    cluster.embed_new(world, emb)
    out = cluster.prefilter_papers(world, emb, profile, {}, threshold=0.45, max_papers=40)
    status = {r["id"]: r["status"] for r in world.execute("SELECT id, status FROM items")}
    assert status[on_hf] == status[on_theme] == "new"
    assert status[off] == "dropped"
    assert out["papers_in"] == 3 and out["kept"] == 2


def test_rank_then_daily_digest(world, profile, settings, tmp_path):
    emb = FakeEmbedder(AXES)
    add(world, "lab", "Rerank release", "rerank")
    add(world, "blog", "Agent toolkit", "agent")
    add(world, "blog", "Funding round", "money")
    cluster.embed_new(world, emb)
    cluster.cluster_new(world, 0.85, 7)
    llm = FakeLLM(
        {
            "Rerank release": {
                "theme_scores": scores(search_quality=0.9),
                "significance": 0.7,
                "is_excluded": False,
                "rationale": "r",
                "why_it_matters": "Cuts latency.",
            },
            "Agent toolkit": {
                "theme_scores": scores(agentic_platforms=0.9),
                "significance": 0.6,
                "is_excluded": False,
                "rationale": "r",
                "why_it_matters": "New MCP tools.",
            },
            "Funding round": {
                "theme_scores": scores(frontier_models=0.9),
                "significance": 0.9,
                "is_excluded": True,
                "rationale": "r",
                "why_it_matters": "Money.",
            },
        }
    )
    out = rank_clusters(world, llm, load_prompt("rank_cluster"), profile, settings, budget_left=1)
    assert out["ranked"] == 3 and not out["budget_hit"]
    assert world.execute("SELECT COUNT(*) FROM items WHERE status='ranked'").fetchone()[0] == 3
    version = world.execute("SELECT DISTINCT prompt_version FROM rankings").fetchone()[0]
    assert version == "rank_cluster@1"

    path, n = write_daily(world, profile, settings, tmp_path)
    md = path.read_text()
    assert n == 2
    assert md.index("## Search & retrieval quality") < md.index("## Agentic systems & platforms")
    assert "Funding round" not in md
    assert "Why it matters: Cuts latency." in md

    # A second digest the same day rebuilds it; a new day would not repeat these clusters.
    world.execute("UPDATE digests SET id = 'daily-2000-01-01', created_at = '2000-01-01'")
    rows, _ = select_clusters(world, "daily-next", 0.6, 15, 7)
    assert rows == []


def test_rank_stops_at_budget(world, profile, settings):
    emb = FakeEmbedder(AXES)
    add(world, "lab", "A rerank", "rerank")
    add(world, "lab", "B agent", "agent")
    cluster.embed_new(world, emb)
    cluster.cluster_new(world, 0.85, 7)
    llm = FakeLLM(
        {
            t: {
                "theme_scores": scores(),
                "significance": 0.1,
                "is_excluded": False,
                "rationale": "r",
                "why_it_matters": "w",
            }
            for t in ["A rerank", "B agent"]
        }
    )
    out = rank_clusters(
        world, llm, load_prompt("rank_cluster"), profile, settings, budget_left=0.001
    )
    assert out["ranked"] == 1 and out["budget_hit"]


def test_render_empty_digest(profile):
    md = render([], profile, datetime(2026, 10, 29), scanned=10, dups=2, notes=["Budget hit."])
    assert "Nothing cleared the bar today." in md
    assert "2 items dropped as duplicates · 10 items scanned" in md
    assert md.rstrip().endswith("Budget hit.")


def test_rank_prompt_renders_all_themes(profile):
    p = load_prompt("rank_cluster")
    text = p.render(
        owner_context="x", themes=profile["themes"], exclude_topics=profile["exclude_topics"]
    )
    assert all(f"`{t['id']}`" in text for t in profile["themes"])
    assert "—" not in text  # no em dashes in the prompt either


def test_different_papers_never_merge_on_similarity(world):
    emb = FakeEmbedder(AXES)
    a = add(world, "hf", "Agent bench one", "agent", arxiv_id="1", item_type="paper")
    b = add(world, "hf", "Agent bench two", "agent", arxiv_id="2", item_type="paper")
    cluster.embed_new(world, emb)
    cluster.cluster_new(world, threshold=0.85, window_days=7)
    ids = dict(world.execute("SELECT id, cluster_id FROM items").fetchall())
    assert ids[a] != ids[b]


def test_borderline_pairs_go_to_same_event_check(world):
    # [2, 0] vs [3, 2] -> cosine 0.83, inside the 0.75 to 0.85 band.
    emb = FakeEmbedder({"gemini": 0, "launch": 0, "release": 0, "leaderboard": 1, "ranking": 1})
    first = add(world, "lab", "Gemini launch", "gemini launch", hours_ago=3)
    second = add(world, "blog", "Gemini leaderboard", "launch release ranking", hours_ago=1)
    cluster.embed_new(world, emb)
    asked = []

    def same_event(a, b):
        asked.append((a["title"], b["title"]))
        return True

    out = cluster.cluster_new(world, 0.85, 7, borderline_low=0.75, same_event=same_event)
    ids = dict(world.execute("SELECT id, cluster_id FROM items").fetchall())
    assert asked == [("Gemini leaderboard", "Gemini launch")]
    assert ids[first] == ids[second] and out["llm_checks"] == 1


def test_prefilter_stores_no_vectors_for_dropped_papers(world, profile):
    emb = FakeEmbedder(AXES | {"biology": 6})
    kept = add(world, "arxiv", "Dense retrieval", "retrieval", arxiv_id="2", item_type="paper")
    dropped = add(world, "arxiv", "Protein folding", "biology", arxiv_id="3", item_type="paper")
    cluster.prefilter_papers(world, emb, profile, {}, threshold=0.45, max_papers=40)
    cluster.purge_dropped_papers(world)
    cluster.embed_new(world, emb)
    stored = {r[0] for r in world.execute("SELECT item_id FROM item_embeddings")}
    assert stored == {kept}
    text = world.execute("SELECT raw_text FROM items WHERE id = ?", (dropped,)).fetchone()[0]
    assert text is None


def test_purge_removes_legacy_vectors_and_rebuilds_table(world):
    emb = FakeEmbedder(AXES)
    keep = add(world, "blog", "Agent post", "agent")
    old = add(world, "arxiv", "Old paper", "rerank", arxiv_id="9", item_type="paper")
    cluster.embed_new(world, emb)  # the old flow stored vectors for every paper
    world.execute("UPDATE items SET status = 'dropped' WHERE id = ?", (old,))
    assert cluster.purge_dropped_papers(world) == 1
    stored = {r[0] for r in world.execute("SELECT item_id FROM item_embeddings")}
    assert stored == {keep}
    # The rebuilt table still answers nearest-neighbor queries.
    vec = emb.encode(["agent"])[0].tobytes()
    hit = world.execute(
        "SELECT item_id FROM item_embeddings WHERE embedding MATCH ? AND k = 1", (vec,)
    ).fetchone()
    assert hit[0] == keep
