# Frontier AI Briefing

The spec is `frontier-ai-briefing-spec.md`. Build in milestone order (spec section 12); don't start a milestone until the previous one's acceptance criteria pass.

**Owner-approved exception (2026-11-05):** M2's labeling CLI and relevance eval were built while M1 collects its 7 consecutive weekday runs (run 1 was Nov 5; expected to pass Fri Nov 13). They only read the DB and `evals/`. Until M1 passes, don't change `ranking.thresholds`, `ranking.weights`, or profile theme weights, and don't add steps to `daily.yml` (e.g. email delivery): any of these would restart the run count.

## Commands

- `uv sync --extra embed`: install (drop `--extra embed` to skip PyTorch)
- `uv run pytest -q`: tests
- `uv run ruff check .`: lint
- `uv run briefing --help`: CLI

## Rules

- **Public repo.** No employer-confidential information, no secrets, no DB, no generated output, no podcast feed token in git.
- Prompts live in `src/briefing/prompts/` as `.md` files with a version header. Bump the version on any prompt change.
- Model names, thresholds, and weights come from `config/settings.yaml`. Don't hardcode them.
- Generated text for the owner (digests, explanations, scripts) has no em dashes and no hype words.
- Each stage is idempotent and resumable by `status`. A failing source never blocks a run.
- Before enabling a source in `config/sources.yaml`, confirm its fetcher parses real items from it.
