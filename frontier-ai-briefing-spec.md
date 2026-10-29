# Frontier AI Briefing System: Build Spec

A personal pipeline that tracks frontier AI news and research, filters it for relevance to a PM working on search, AI overviews, trust systems, and agentic platforms, explains papers at the right depth, and delivers a weekly audio briefing.

This document is the full spec. A coding agent should be able to build the system end to end from it. Where a decision is left open, it is marked **DECISION** with a recommended default.

---

## 1. Goals and non-goals

### Goals
1. **Filter, not firehose.** Out of ~200 to 500 items per week, surface the 5 to 15 that matter for the owner's problem space, each with a short "why this matters" note.
2. **Explain at the right depth.** For papers that clear the relevance bar, produce a three-tier explanation (PM takeaway, intuitive mechanism, technical core) that builds on concepts the owner already knows.
3. **Deliver in a usable format.** A daily text digest, a weekly written briefing, and a weekly audio episode (15 to 20 min) consumable during training.
4. **Be measurable.** Relevance and explanation quality are evaluated, not assumed. Owner feedback flows back into ranking.

### Non-goals
- No employer-confidential information anywhere in the system. The relevance profile describes public themes only.
- Not a general-purpose news reader or social feed.
- No multi-user support in v1.

### Owner profile (for tone and calibration)
- Senior PM with substantial applied AI experience (personalization, causal inference, ML governance, agentic systems).
- Does **not** have a deep stats/math background. Wants the level between "equation-heavy" and "too simple": intuition first, the key idea of the math in words, and an option to go deeper.
- Prefers direct, concise, warm prose. No hedging. No em dashes in generated text.

---

## 2. System overview

```
 ┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌───────────────┐
 │  Ingestion  │──▶│ Normalize +  │──▶│  Relevance   │──▶│  Enrichment   │
 │  (sources)  │   │  Dedup/Cluster│  │  Ranking     │   │ (notes, tiers)│
 └─────────────┘   └──────────────┘   └──────────────┘   └───────┬───────┘
                                                                   │
        ┌──────────────────────────────────────────────────────────┤
        ▼                          ▼                               ▼
 ┌─────────────┐          ┌────────────────┐             ┌────────────────┐
 │ Daily digest│          │ Weekly briefing│────────────▶│ Audio episode  │
 │  (email/md) │          │  (markdown/web)│  script gen │ (TTS + Spotify)│
 └─────────────┘          └────────────────┘             └────────────────┘
        │                          │                               │
        └──────────────┬───────────┴───────────────────────────────┘
                       ▼
               ┌───────────────┐        ┌────────────────┐
               │   Feedback    │───────▶│ Concept map +  │
               │  (👍/👎, notes)│        │ ranking weights│
               └───────────────┘        └────────────────┘
```

Three components, one shared data layer:
- **Component A: Briefing backbone** (ingestion, dedup, relevance, digests)
- **Component B: Layered paper explainer** (three-tier explanations + concept map)
- **Component C: Audio briefing** (script generation, TTS, delivery to the owner's Spotify library)

---

## 3. Tech stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | |
| Package mgmt | `uv` | |
| Orchestration | LangGraph | Owner has prior LangGraph experience; use it for the enrichment and script-gen graphs. Plain functions for ingestion. |
| LLM | Anthropic API via `anthropic` SDK | Use a fast/cheap model (e.g. `claude-haiku-5-5`) for classification and short notes; a stronger model (e.g. `claude-sonnet-5-5`) for paper explanations and weekly synthesis. Model names live in config. |
| Embeddings | **DECISION**: `sentence-transformers` local model (default: `BAAI/bge-small-en-v1.5`) | Local keeps cost near zero. Swap for an API embedding model if quality is insufficient. |
| Storage | SQLite + `sqlite-vec` extension | Single file, easy backup. Upgrade path: Postgres + pgvector. |
| PDF parsing | `pymupdf` | Fallback to arXiv HTML (ar5iv) when available; it parses more cleanly. |
| TTS | **DECISION**: default OpenAI TTS or ElevenLabs via API; local fallback Kokoro | Abstract behind a `TTSProvider` interface. |
| Audio post | `pydub` + `ffmpeg` | Concatenate segments, normalize loudness to -16 LUFS. |
| Audio delivery | Save to Spotify CLI ([`spotify/save-to-spotify`](https://github.com/spotify/save-to-spotify), Apache-2.0, beta) | Uploads each episode to the owner's library as a private Personal Podcast, with chapters. See §7.3. |
| Web/feed hosting | Static site (GitHub Pages or Cloudflare Pages) for briefing archive; fallback podcast RSS hosted at an unguessable URL | Fallback feed only: random path token, no index. |
| Email | **DECISION**: Resend or SMTP | For the daily digest. Optional in v1 (markdown file is acceptable). |
| Scheduling | GitHub Actions cron (default) or a small VPS with cron | Actions is simplest; DB stored as an artifact or in a private repo/branch, or move DB to a hosted SQLite (Turso) if Actions. |
| Config | `pydantic-settings` + YAML files | |
| Testing | `pytest`, plus an `evals/` suite (see §9) | |

---

## 4. Data model (SQLite)

```sql
-- Raw items from any source
CREATE TABLE items (
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

CREATE TABLE sources (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  kind        TEXT NOT NULL,                 -- 'rss' | 'arxiv' | 'hf_papers' | 'github' | 'html'
  url         TEXT NOT NULL,
  tier        INTEGER NOT NULL,              -- 1 = primary lab/official, 2 = strong secondary, 3 = aggregator
  enabled     BOOLEAN NOT NULL DEFAULT 1
);

-- Near-duplicate coverage of the same event grouped together
CREATE TABLE clusters (
  id              TEXT PRIMARY KEY,
  canonical_item  TEXT NOT NULL,             -- the best item in the cluster (highest source tier, earliest)
  summary         TEXT,
  created_at      TIMESTAMP NOT NULL
);

CREATE VIRTUAL TABLE item_embeddings USING vec0(
  item_id TEXT PRIMARY KEY,
  embedding FLOAT[384]
);

-- Relevance scores per cluster
CREATE TABLE rankings (
  cluster_id      TEXT PRIMARY KEY,
  theme_scores    JSON NOT NULL,             -- {"search_quality": 0.82, "agentic_platforms": 0.4, ...}
  significance    REAL NOT NULL,             -- 0..1, how big a deal for the field
  relevance       REAL NOT NULL,             -- 0..1, final blended score
  rationale       TEXT NOT NULL,             -- LLM's short justification
  why_it_matters  TEXT,                      -- 2-line note shown to owner
  ranked_at       TIMESTAMP NOT NULL,
  model           TEXT NOT NULL
);

-- Layered paper explanations
CREATE TABLE explanations (
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
CREATE TABLE concepts (
  slug            TEXT PRIMARY KEY,          -- 'kv-cache', 'dpo', 'test-time-compute'
  name            TEXT NOT NULL,
  short_def       TEXT NOT NULL,             -- one-sentence definition
  explainer       TEXT,                      -- ~200 word explainer at the owner's level
  prerequisites   JSON NOT NULL DEFAULT '[]',-- list of slugs
  familiarity     TEXT NOT NULL DEFAULT 'new', -- 'new' | 'seen' | 'familiar' | 'known'
  first_seen_at   TIMESTAMP,
  times_seen      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE feedback (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  target_type TEXT NOT NULL,                 -- 'cluster' | 'explanation' | 'concept' | 'episode'
  target_id   TEXT NOT NULL,
  signal      TEXT NOT NULL,                 -- 'up' | 'down' | 'more_like_this' | 'too_basic' | 'too_technical' | 'known'
  note        TEXT,
  created_at  TIMESTAMP NOT NULL
);

CREATE TABLE digests (
  id          TEXT PRIMARY KEY,              -- 'daily-2026-10-29' | 'weekly-2026-W44'
  kind        TEXT NOT NULL,                 -- 'daily' | 'weekly'
  markdown    TEXT NOT NULL,
  cluster_ids JSON NOT NULL,
  created_at  TIMESTAMP NOT NULL
);

CREATE TABLE episodes (
  id            TEXT PRIMARY KEY,            -- 'ep-2026-W44'
  digest_id     TEXT NOT NULL,
  script        TEXT NOT NULL,
  audio_path    TEXT NOT NULL,
  duration_sec  INTEGER NOT NULL,
  published_at  TIMESTAMP NOT NULL
);

CREATE TABLE runs (                          -- observability
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  stage       TEXT NOT NULL,
  started_at  TIMESTAMP NOT NULL,
  finished_at TIMESTAMP,
  items_in    INTEGER, items_out INTEGER,
  tokens_in   INTEGER, tokens_out INTEGER,
  cost_usd    REAL,
  error       TEXT
);
```

---

## 5. Component A: Briefing backbone

### 5.1 Sources (`config/sources.yaml`)

Seed list. The agent should verify each URL resolves and has a working feed before enabling it; drop or replace any that do not.

| Tier | Source | Kind |
|---|---|---|
| 1 | Anthropic news | html/rss |
| 1 | OpenAI blog | rss |
| 1 | Google DeepMind blog | rss |
| 1 | Google Research blog | rss |
| 1 | Meta AI blog | html |
| 1 | Mistral news | html |
| 1 | Hugging Face blog | rss |
| 1 | arXiv cs.CL, cs.IR, cs.AI, cs.LG (filtered, see below) | arxiv API |
| 1 | Hugging Face Daily Papers | hf_papers |
| 1 | Spotify Research | rss |
| 2 | Spotify Engineering | rss |
| 2 | Simon Willison's weblog | rss |
| 2 | Interconnects (Nathan Lambert) | rss |
| 2 | Latent Space | rss |
| 2 | The Batch (DeepLearning.AI) | rss |
| 2 | Import AI (Jack Clark) | rss |
| 2 | Lilian Weng's blog | rss |
| 2 | Eugene Yan | rss |
| 3 | Hacker News front page, AI-filtered | api |
| 3 | GitHub trending (Python, AI topics) | html |

**arXiv volume control:** arXiv cs.* produces hundreds of papers daily. Do not rank them all with an LLM. Pre-filter:
1. Keep papers that appear on HF Daily Papers, or
2. Keep papers whose embedding similarity to any theme in the relevance profile exceeds a threshold (start at 0.45, tune), or
3. Keep papers from a configurable list of watched labs/authors.

Target: ≤ 40 arXiv papers per day reach LLM ranking.

### 5.2 Ingestion

- One fetcher per `kind`, all implementing `Fetcher.fetch(source, since) -> list[RawItem]`.
- Respect robots.txt and rate limits. Set a descriptive User-Agent. Cache HTTP responses (`requests-cache`, 6h TTL).
- For blogs, extract main text with `trafilatura`.
- For papers, store abstract in `raw_text` at ingest. Full text is fetched only at enrichment time for papers that pass ranking.
- Canonicalize URLs (strip UTM params, trailing slashes, `www.`). `id = sha256(canonical_url)`.
- Idempotent: re-running ingestion never creates duplicates.

### 5.3 Dedup and clustering

Same event covered by five outlets should appear once.
1. Embed `title + first 500 chars of raw_text`.
2. For each new item, find nearest neighbors among items from the last 7 days.
3. If cosine similarity ≥ 0.85 (tune), join that cluster. Otherwise start a new cluster.
4. Cluster canonical item = highest tier source, ties broken by earliest publish time.
5. Optional LLM check for borderline pairs (0.75 to 0.85): "Are these about the same announcement/paper? yes/no."

### 5.4 Relevance profile (`config/profile.yaml`)

This is the heart of personalization. Public themes only.

```yaml
owner_context: >
  PM working on consumer search and discovery for a large audio streaming platform.
  Areas: search quality and relevance, AI-generated search overviews/answers,
  trust and safety of AI outputs, agentic platforms and tooling.
  Strong applied ML background; not a math/stats specialist.

themes:
  - id: search_quality
    name: Search & retrieval quality
    description: >
      Ranking, retrieval (dense/sparse/hybrid), query understanding, reranking,
      relevance evaluation, RAG retrieval components, recommendation-search overlap.
    weight: 1.0
  - id: ai_overviews
    name: AI-generated answers & overviews
    description: >
      Generative search, answer engines, summarization grounded in sources,
      citation/attribution, answer UX, hallucination in summaries.
    weight: 1.0
  - id: trust_safety
    name: Trust, grounding & evaluation of AI outputs
    description: >
      Factuality, grounding, calibration, LLM-as-judge, eval methodology,
      red-teaming, content policy, interpretability with product implications.
    weight: 0.9
  - id: agentic_platforms
    name: Agentic systems & platforms
    description: >
      Agent architectures, tool use, MCP and protocols, orchestration frameworks,
      agent evaluation, memory, multi-agent systems, developer platforms.
    weight: 0.9
  - id: audio_media
    name: Audio, voice & media AI
    description: >
      Speech models, TTS, voice agents, music/audio generation and understanding,
      podcast/audio search, multimodal models handling audio.
    weight: 0.7
  - id: frontier_models
    name: Frontier model releases & capabilities
    description: >
      Major model launches, capability jumps, pricing changes, context length,
      reasoning/test-time compute, open-weight releases.
    weight: 0.6

exclude_topics:
  - Pure theory with no near-term product implication
  - Crypto/web3 AI
  - Funding rounds without product or research substance
  - Robotics hardware (unless agentic software is central)
```

### 5.5 Ranking stage

For each new cluster (canonical item), one LLM call with structured output.

**Input:** profile YAML, item title, source + tier, publish date, first ~3k chars of text.

**Output schema (enforce via tool-use / JSON schema):**
```json
{
  "theme_scores": {"search_quality": 0.0, "...": 0.0},
  "significance": 0.0,
  "is_excluded": false,
  "rationale": "1-2 sentences, internal",
  "why_it_matters": "2 lines max, written to the owner, concrete implication for their problem space"
}
```

**Blended score:**
```
theme_component = max_t(theme_score_t * weight_t)
relevance = 0.55 * theme_component
          + 0.30 * significance
          + 0.10 * source_tier_bonus     # tier1=1.0, tier2=0.6, tier3=0.3
          + 0.05 * feedback_affinity     # see §8
if is_excluded: relevance = 0
```
Weights live in config.

**Thresholds:**
- `relevance ≥ 0.60` → daily digest
- Top 8 to 12 of the week by relevance (with theme diversity, max 4 per theme) → weekly briefing
- Papers with `relevance ≥ 0.65` → Component B

**`why_it_matters` writing rules** (put in the prompt):
- Concrete and specific to the owner's areas. Bad: "This could be important for search." Good: "Shows reranking with a small model matches a large one on long queries, which could cut overview latency."
- No hype words ("groundbreaking", "game-changer", "revolutionary").
- If the implication is speculative, say what would need to be true.
- No em dashes.

### 5.6 Daily digest

Generated each morning (default 7:00 America/New_York). Markdown, grouped by theme:

```markdown
# AI Briefing: Thu Oct 29

## Search & retrieval quality
**[Title](url)**: Source · paper
Why it matters: ...

## Agentic systems & platforms
...

---
3 items dropped as duplicates · 214 items scanned
```

Each item has feedback links (see §8). Deliver via email and write to `output/daily/`.

### 5.7 Weekly briefing

Generated Saturday morning. A stronger model writes:
1. **The week in one paragraph:** the 2 to 3 shifts that matter.
2. **Top items** (8 to 12), each with a 3 to 5 sentence summary and the `why_it_matters` note, refined with full-week context.
3. **Papers worth your time:** links to Component B explanations.
4. **Concepts you met this week:** new concepts from the concept map with one-line definitions.
5. **One question to think about:** a product-strategy question raised by the week's news.

Published to the static site and used as input to Component C.

---

## 6. Component B: Layered paper explainer

Runs on papers passing the threshold, up to a configurable daily cap (default 3).

### 6.1 Pipeline (LangGraph)

```
fetch_full_text → extract_structure → identify_concepts → lookup_concept_map
      → write_tiers → self_critique → (revise if needed) → update_concept_map → store
```

1. **fetch_full_text:** prefer arXiv HTML; fall back to PDF via `pymupdf`. Strip references and appendices beyond the first 2 pages of appendix.
2. **extract_structure:** LLM extracts problem, method, key results (with numbers), limitations, and what's genuinely new vs. prior work.
3. **identify_concepts:** LLM lists technical concepts the paper relies on, as slugs. Normalize against existing `concepts` table (embedding match + LLM confirm) to avoid duplicates like `kv-cache` vs `key-value-cache`.
4. **lookup_concept_map:** split concepts into `known/familiar` (can reference without explaining), `seen` (one-clause reminder), and `new` (needs explanation).
5. **write_tiers:** generate three tiers:

| Tier | Length | Content |
|---|---|---|
| 1. PM takeaway | 80 to 120 words | What they did, did it work (numbers), what it changes for products like the owner's, maturity (research toy vs. production-ready). |
| 2. Intuition | 250 to 400 words | How it works using analogies and plain language. Introduce new concepts inline with a short definition. No equations. |
| 3. Technical core | 400 to 700 words | The key mechanism precisely. Equations allowed only if each is followed by a plain-English reading of what every term means and why it is there. Explicit "what's actually novel" and "where it's weak" sections. |

6. **self_critique:** a second LLM pass checks: Are numbers faithful to the paper? Any claim not supported by the text? Is Tier 2 free of jargon not defined? Is Tier 3 understandable without a stats background? Any em dashes? Returns pass/fail with fixes; one revision loop max.
7. **update_concept_map:** for `new` concepts, generate `short_def` and a ~200 word `explainer` at the owner's level, set `familiarity = 'seen'`, increment `times_seen`. Concepts auto-promote `seen → familiar` after appearing in 4 explanations, unless feedback says otherwise.

### 6.2 Output

Rendered as one HTML page per paper on the static site with collapsible tiers (Tier 1 open by default), concept chips that link to concept pages, and feedback buttons including `too_basic` / `too_technical`.

### 6.3 Depth calibration

Track the ratio of `too_basic` vs. `too_technical` feedback per tier. Inject a calibration line into the `write_tiers` prompt, e.g. "Recent feedback: Tier 3 has been too technical 3 of the last 5 times; lean more on intuition and define every symbol." Keep a rolling window of 10.

---

## 7. Component C: Audio briefing

### 7.1 Script generation (LangGraph)

Input: the weekly briefing markdown plus Tier 1 summaries of the featured papers.

```
outline → draft_script → spoken_language_pass → length_check → final_script
```

- **Format:** single host, conversational, 15 to 20 minutes (~2,200 to 3,000 words at ~150 wpm).
- **Structure:** cold open (one sentence hook), the week in brief, 4 to 6 segments, one "concept of the week" mini-explainer, closing question.
- **Spoken-language rules:** short sentences, no URLs, no parenthetical asides, spell out acronyms on first use, numbers rounded and spoken naturally ("about forty percent"), clear verbal transitions between segments, no markdown, no em dashes.
- **Segment markers:** script is a list of segments `{id, title, text}` so audio can be generated and cached per segment and chapters can be emitted.

### 7.2 TTS and assembly

- `TTSProvider` interface: `synthesize(text: str, voice: str) -> bytes`. Implement at least one API provider and stub a local one.
- Chunk long segments at sentence boundaries to respect provider limits.
- Assemble with `pydub`: short silence between segments (600 ms), optional intro/outro sting (royalty-free or self-generated only), loudness normalize to -16 LUFS, export MP3 128 kbps mono.
- Emit chapter markers from segment boundaries: as a Save to Spotify timeline (§7.3) and as ID3 CHAP tags in the MP3, so the fallback feed has them too.

### 7.3 Delivery

The owner listens in Spotify. The Spotify app does not subscribe to arbitrary private RSS URLs, so the primary path is the Save to Spotify CLI, which saves audio to the owner's own library as a private Personal Podcast. Episodes are not searchable by other listeners and never enter the public catalog. The CLI only uploads; TTS and assembly (§7.2) stay as specced.

**Primary: Save to Spotify** (`audio.delivery: save_to_spotify`)
- Call the CLI from Python via `subprocess` with `--json`. Pin the CLI version in config; it is beta.
- One show for all episodes, found by title (`shows --json`) and created once if missing. Keep the show ID out of the repo.
- Per episode: `upload <mp3> --title "<week title>" --summary "<show notes>" --show-id <id> --image <cover>`, then `episodes status <id> --wait` until processing finishes.
- Then `timeline set --episode-id <id> --from-file timeline.json`: one `chapter` per segment (at least 2, first at 0 ms), plus a `link` to each segment's main source. Links need a positive `duration_ms` and must not overlap.
- Show notes = the week-in-one-paragraph summary plus the top item titles. The full briefing with links stays on the static site.
- **Auth:** the owner runs `save-to-spotify auth login` once locally. `token.json` and `dpop_key.json` go into CI secrets as base64 and are restored before each run; the CLI refreshes the access token itself. Run `auth status` as a pre-flight check. Do not use `SAVE_TO_SPOTIFY_AUTH_TOKEN` for scheduled runs; that token expires in about an hour.
- **Failure:** if `auth status` fails or the upload errors, publish to the fallback feed (if enabled) and send the failure notice (§11).

**Fallback: private RSS feed** (`audio.delivery: rss` or `both`)
- Generate a valid podcast RSS 2.0 feed with iTunes namespace tags.
- Host MP3s and `feed.xml` under a random path token, e.g. `/pod/<32-char-token>/feed.xml`. No links to it from the public site; `noindex`.
- Owner subscribes by URL in any podcast app that supports private RSS feeds.
- Show notes = the weekly briefing with links.

---

## 8. Feedback loop

### 8.1 Capture
- Every digest item, explanation, and episode has signed feedback links: `https://<site>/fb?t=<type>&id=<id>&s=<signal>&sig=<hmac>`.
- Simplest backend: a tiny serverless function (Cloudflare Worker or similar) that validates the HMAC and appends to a queue/table, synced into SQLite on the next run. **DECISION**: alternatively, reply-to-email parsing; the Worker is simpler.
- Optional free-text note field.

### 8.2 Use
- **feedback_affinity** for ranking: for a new cluster, compute similarity to clusters with `up`/`more_like_this` minus similarity to clusters with `down`, over the last 60 days. Squash to 0..1.
- **Theme weight drift:** monthly, propose adjustments to theme weights based on up/down rates per theme. Write proposals to a file for the owner to approve; never auto-apply.
- **Concept familiarity:** `known` signal sets familiarity directly.
- **Depth calibration:** see §6.3.

---

## 9. Evaluation

Build evals before tuning. Store in `evals/`.

### 9.1 Relevance eval
- Owner labels ~150 items (mix of sources, themes, and obvious non-relevant) as `must_see` / `nice` / `skip`. Provide a small CLI or notebook to do this quickly.
- Metrics: precision@10 on weekly selection, recall of `must_see` items at the daily threshold, nDCG.
- Run on every change to prompts, weights, or thresholds. Report deltas.

### 9.2 Explanation eval
- Faithfulness: LLM-as-judge with the paper text checks every numeric claim and every factual claim in Tiers 1 to 3. Track unsupported-claim rate; target < 2%.
- Readability: judge rates Tier 2 for undefined jargon and Tier 3 for "understandable by a PM without a stats background" on a 1 to 5 scale. Calibrate the judge against ~20 owner-rated examples.
- Style checks (deterministic): no em dashes, length bands respected, no banned hype words.

### 9.3 Audio eval
- Deterministic: duration within 14 to 21 minutes, no URLs or markdown symbols in script, loudness within ±1 LU of target, generated `timeline.json` passes the Save to Spotify timeline rules.
- Judge pass on script for spoken naturalness and factual consistency with the written briefing.

### 9.4 Dedup eval
- Hand-label ~50 item pairs as same/different event. Track precision and recall of clustering.

---

## 10. Repository layout

```
frontier-briefing/
├── README.md
├── pyproject.toml
├── config/
│   ├── settings.yaml         # models, thresholds, weights, schedule, providers
│   ├── sources.yaml
│   └── profile.yaml
├── src/briefing/
│   ├── __init__.py
│   ├── cli.py                # `briefing ingest|rank|enrich|daily|weekly|audio|eval|label`
│   ├── db.py                 # schema, migrations, queries
│   ├── models.py             # pydantic models
│   ├── llm.py                # Anthropic client wrapper: retries, structured output, token/cost logging
│   ├── embed.py
│   ├── ingest/
│   │   ├── base.py
│   │   ├── rss.py
│   │   ├── arxiv.py
│   │   ├── hf_papers.py
│   │   ├── html.py
│   │   └── github.py
│   ├── cluster.py
│   ├── rank.py
│   ├── digest/
│   │   ├── daily.py
│   │   └── weekly.py
│   ├── explain/
│   │   ├── graph.py          # LangGraph pipeline
│   │   ├── fulltext.py
│   │   └── concepts.py
│   ├── audio/
│   │   ├── script_graph.py
│   │   ├── tts.py
│   │   ├── assemble.py
│   │   ├── publish.py        # Save to Spotify upload + timeline
│   │   └── feed.py           # fallback private RSS feed
│   ├── feedback.py
│   ├── site/                 # static site generator (Jinja2 templates)
│   └── prompts/              # one .md file per prompt, versioned
├── worker/                   # feedback endpoint (Cloudflare Worker)
├── evals/
│   ├── data/                 # labeled sets
│   ├── relevance.py
│   ├── explanations.py
│   ├── audio.py
│   └── dedup.py
├── tests/
└── .github/workflows/
    ├── daily.yml             # ingest → cluster → rank → enrich → daily digest
    ├── weekly.yml            # weekly briefing → audio → publish
    └── ci.yml                # tests + fast evals on PR
```

Prompts are files, not inline strings, and each has a version header so eval results can be tied to prompt versions.

---

## 11. Operations

- **Schedule:** daily run 6:30 America/New_York (digest ready by 7:00). Weekly run Saturday 6:00.
- **Secrets:** `ANTHROPIC_API_KEY`, TTS provider key, email provider key, feedback HMAC secret, hosting tokens, Save to Spotify credentials (`STS_TOKEN_B64`, `STS_DPOP_KEY_B64`), and the podcast path token if the fallback feed is on. Stored as CI secrets. Never logged.
- **Cost guardrails:** log tokens and cost per stage in `runs`. Hard daily budget in config (default $3/day, $10/weekly run). If exceeded, skip enrichment and audio, still send the digest, and note it in the digest footer.
- **Expected cost (rough):** ranking ~50 to 100 clusters/day with a small model, 1 to 3 paper explanations/day, 1 weekly synthesis and script, ~20 min TTS/week. Should land in the low tens of dollars per month. Verify against real usage in the first two weeks.
- **Failure handling:** each stage is idempotent and resumable by `status`. A failed source never blocks the run. Send a short failure notice if a scheduled run errors.
- **Backups:** nightly copy of the SQLite file to private storage.
- **Copyright hygiene:** store and display only titles, short excerpts, and original summaries. Do not republish full articles. Audio scripts summarize in original words and attribute sources by name.

---

## 12. Build plan

Each milestone ends with something usable. Do not start the next until the acceptance criteria pass.

### M1: Ingestion and daily digest (week 1)
- Sources, fetchers, DB, dedup/clustering, ranking, daily markdown digest written to disk.
- **Accept:** 7 consecutive daily runs succeed; digest has 5 to 15 items; no duplicate events in a digest; cost logged.

### M2: Relevance eval and tuning (week 2)
- Labeling CLI, relevance eval, threshold and weight tuning. Email delivery.
- **Accept:** precision@10 ≥ 0.7 and `must_see` recall ≥ 0.8 on the labeled set.

### M3: Paper explainer (weeks 3 to 4)
- Full-text fetch, LangGraph explainer, concept map, static site pages.
- **Accept:** unsupported-claim rate < 2% on 15 papers; owner rates ≥ 12 of 15 as "right depth" at Tier 2.

### M4: Weekly briefing and feedback (week 5)
- Weekly synthesis, feedback Worker, affinity in ranking, depth calibration.
- **Accept:** feedback links work end to end; a down-voted theme measurably shifts the following week's ranking in a test.

### M5: Audio (week 6)
- Script graph, TTS, assembly, Save to Spotify delivery. Fallback RSS feed optional.
- **Accept:** a scheduled run puts the episode in the owner's Spotify library with no manual step; duration and loudness within spec; chapters visible in the Spotify player.

### M6: Hardening (ongoing)
- Cost guardrails, failure notices, backups, monthly theme-weight proposal report.

---

## 13. Open decisions for the owner

1. Embedding model: local (free, slightly lower quality) vs. API.
2. TTS provider and voice. Generate a 60-second sample from two or three options before committing.
3. Hosting for site and feedback endpoint, plus the feed if the fallback is on (default: Cloudflare Pages + Worker).
4. Daily digest channel: email, markdown in a repo, or both.
5. Whether to add a second "co-host" voice later (adds conversational texture, roughly doubles TTS complexity).

---

## 14. Future extensions (out of scope for v1)

- **Hype filter:** for major claims, gather evidence (benchmark details, replications, critiques) and attach a confidence rating.
- **Competitive answer tracker:** weekly snapshots of public AI answer products on a fixed query set, diffed over time. Check each product's terms of service first.
- **Prediction ledger:** owner logs forecasts about the field; system tracks related news and resolves them, reporting calibration.
- **Ask-the-archive:** chat over the accumulated briefing and explanation archive.
