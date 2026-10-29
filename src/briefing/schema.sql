-- Schema from spec section 4. Applied by briefing.db.init_db(); safe to re-run.

-- Raw items from any source
CREATE TABLE IF NOT EXISTS items (
  id              TEXT PRIMARY KEY,          -- sha256 of canonical URL
  source_id       TEXT NOT NULL,             -- FK sources.id
  url             TEXT NOT NULL,
  canonical_url   TEXT NOT NULL,
  title           TEXT NOT NULL,
  author          TEXT,
  published_at    TIMESTAMP,
  fetched_at      TIMESTAMP NOT NULL,
  item_type       TEXT NOT NULL,             -- 'paper' | 'blog' | 'news' | 'release' | 'repo'
  raw_text        TEXT,                      -- abstract or article body (truncated to ~20k chars)
  arxiv_id        TEXT,
  cluster_id      TEXT,                      -- FK clusters.id after dedup
  status          TEXT NOT NULL DEFAULT 'new' -- 'new' | 'ranked' | 'enriched' | 'dropped'
);

CREATE TABLE IF NOT EXISTS sources (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  kind        TEXT NOT NULL,                 -- 'rss' | 'arxiv' | 'hf_papers' | 'github' | 'html'
  url         TEXT NOT NULL,
  tier        INTEGER NOT NULL,              -- 1 = primary lab/official, 2 = strong secondary, 3 = aggregator
  enabled     BOOLEAN NOT NULL DEFAULT 1
);

-- Near-duplicate coverage of the same event grouped together
CREATE TABLE IF NOT EXISTS clusters (
  id              TEXT PRIMARY KEY,
  canonical_item  TEXT NOT NULL,             -- the best item in the cluster (highest source tier, earliest)
  summary         TEXT,
  created_at      TIMESTAMP NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS item_embeddings USING vec0(
  item_id TEXT PRIMARY KEY,
  embedding FLOAT[384]
);

-- Relevance scores per cluster
CREATE TABLE IF NOT EXISTS rankings (
  cluster_id      TEXT PRIMARY KEY,
  theme_scores    JSON NOT NULL,             -- {"search_quality": 0.82, "agentic_platforms": 0.4, ...}
  significance    REAL NOT NULL,             -- 0..1, how big a deal for the field
  relevance       REAL NOT NULL,             -- 0..1, final blended score
  rationale       TEXT NOT NULL,             -- LLM's short justification
  why_it_matters  TEXT,                      -- 2-line note shown to owner
  ranked_at       TIMESTAMP NOT NULL,
  model           TEXT NOT NULL,
  prompt_version  TEXT NOT NULL              -- e.g. "rank_cluster@1", ties scores to a prompt version
);

-- Layered paper explanations
CREATE TABLE IF NOT EXISTS explanations (
  item_id         TEXT PRIMARY KEY,
  tier1_pm        TEXT NOT NULL,             -- PM takeaway
  tier2_intuition TEXT NOT NULL,             -- mechanism, intuitive
  tier3_technical TEXT NOT NULL,             -- technical core, math explained in words
  concepts        JSON NOT NULL,             -- list of concept slugs referenced
  new_concepts    JSON NOT NULL,             -- concepts introduced for the first time
  created_at      TIMESTAMP NOT NULL,
  model           TEXT NOT NULL
);

-- Concept map: what the owner has been exposed to / knows
CREATE TABLE IF NOT EXISTS concepts (
  slug            TEXT PRIMARY KEY,          -- 'kv-cache', 'dpo', 'test-time-compute'
  name            TEXT NOT NULL,
  short_def       TEXT NOT NULL,             -- one-sentence definition
  explainer       TEXT,                      -- ~200 word explainer at the owner's level
  prerequisites   JSON NOT NULL DEFAULT '[]',-- list of slugs
  familiarity     TEXT NOT NULL DEFAULT 'new', -- 'new' | 'seen' | 'familiar' | 'known'
  first_seen_at   TIMESTAMP,
  times_seen      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feedback (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  target_type TEXT NOT NULL,                 -- 'cluster' | 'explanation' | 'concept' | 'episode'
  target_id   TEXT NOT NULL,
  signal      TEXT NOT NULL,                 -- 'up' | 'down' | 'more_like_this' | 'too_basic' | 'too_technical' | 'known'
  note        TEXT,
  created_at  TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
  id          TEXT PRIMARY KEY,              -- 'daily-2026-10-29' | 'weekly-2026-W44'
  kind        TEXT NOT NULL,                 -- 'daily' | 'weekly'
  markdown    TEXT NOT NULL,
  cluster_ids JSON NOT NULL,
  created_at  TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
  id            TEXT PRIMARY KEY,            -- 'ep-2026-W44'
  digest_id     TEXT NOT NULL,
  script        TEXT NOT NULL,
  audio_path    TEXT NOT NULL,
  duration_sec  INTEGER NOT NULL,
  published_at  TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (                          -- observability
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  stage       TEXT NOT NULL,
  started_at  TIMESTAMP NOT NULL,
  finished_at TIMESTAMP,
  items_in    INTEGER, items_out INTEGER,
  tokens_in   INTEGER, tokens_out INTEGER,
  cost_usd    REAL,
  error       TEXT
);
