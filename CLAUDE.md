# Frontier AI Briefing

The spec is `frontier-ai-briefing-spec.md`. Build in milestone order (spec section 12); don't start a milestone until the previous one's acceptance criteria pass.

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
