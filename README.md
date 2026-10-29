# Frontier AI Briefing

A personal pipeline that tracks frontier AI news and research, keeps only what matters for my work, explains papers at the depth I want, and turns the week into a short audio episode I can listen to while training.

I built it because the volume is the problem. Each week brings a few hundred posts, papers, and releases. Maybe ten of them matter for search, AI-generated answers, trust in AI outputs, and agentic platforms. I want those ten, with a clear note on why each one matters.

The full design is in [frontier-ai-briefing-spec.md](frontier-ai-briefing-spec.md).

## How it works

1. **Ingest** posts, papers, and releases from about 20 sources (lab blogs, arXiv, Hugging Face Daily Papers, a few newsletters).
2. **Dedup** coverage of the same event into one cluster.
3. **Rank** each cluster against a relevance profile with a small model, then blend in significance, source tier, and my feedback.
4. **Explain** the papers that clear the bar in three tiers: PM takeaway, intuition, technical core. A concept map tracks what I already know, so explanations build on it.
5. **Deliver** a daily digest, a weekly written briefing, and a weekly 15 to 20 minute audio episode. The episode lands in my Spotify library as a private Personal Podcast via [Save to Spotify](https://github.com/spotify/save-to-spotify), with a private RSS feed as fallback.
6. **Measure** relevance and explanation quality with evals. Feedback links feed back into ranking.

## Status

M1 is built: ingestion from 20 sources, dedup, ranking, and a daily markdown digest. It still has to pass its acceptance test, 7 straight daily runs.

| Milestone | Scope | Status |
|---|---|---|
| M1 | Ingestion, dedup, ranking, daily digest | Built, 7-day run pending |
| M2 | Relevance eval and tuning, email | |
| M3 | Paper explainer and concept map | |
| M4 | Weekly briefing and feedback loop | |
| M5 | Audio episode and Spotify delivery | |
| M6 | Hardening | |

## Setup

Needs [uv](https://docs.astral.sh/uv/). Python 3.12 is installed by uv if missing.

```bash
uv sync --extra embed
cp .env.example .env        # then set ANTHROPIC_API_KEY
uv run pytest
uv run briefing run-daily   # ingest -> rank -> output/daily/<date>.md
```

The scheduled run is `.github/workflows/daily.yml`. It keeps the database and digests in a separate private repo, so nothing personal lands here.

`--extra embed` pulls in `sentence-transformers` and PyTorch for local embeddings. Skip it if you only need the tests.

## Layout

```
config/          settings, sources, relevance profile (YAML)
src/briefing/    the pipeline: ingest, cluster, rank, digest, explain, audio, site
  prompts/       one versioned .md file per prompt
worker/          feedback endpoint (Cloudflare Worker)
evals/           relevance, explanation, audio, and dedup evals
tests/           pytest
.github/         CI, plus daily and weekly runs (manual until M1)
```

## Privacy

This repo is public. The relevance profile describes public themes only. The database, generated output, Save to Spotify credentials, and podcast feed token never get committed; they are in `.gitignore`, and secrets live in `.env` locally and in repository secrets in CI.
