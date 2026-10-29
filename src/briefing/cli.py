"""Command line entry point: `briefing <command>`."""

import json
import os
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

import typer

from briefing import db
from briefing.config import ROOT, Secrets, load_yaml

app = typer.Typer(help="Frontier AI briefing pipeline.", no_args_is_help=True)


def _not_yet(milestone: str) -> None:
    typer.echo(f"Not implemented yet (planned for {milestone}).")
    raise typer.Exit(code=1)


def _settings() -> dict:
    return load_yaml("settings.yaml")


def _conn(settings: dict):
    conn = db.connect(ROOT / settings["storage"]["db_path"])
    db.init_db(conn)
    return conn


def _local_midnight_iso(settings: dict) -> str:
    tz = ZoneInfo(settings["schedule"]["timezone"])
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return db.iso(midnight)


@app.command("init-db")
def init_db() -> None:
    """Create the SQLite database and all tables."""
    settings = _settings()
    _conn(settings)
    typer.echo(f"Database ready at {ROOT / settings['storage']['db_path']}")


@app.command()
def ingest(
    source: Annotated[list[str] | None, typer.Option(help="Only these source ids.")] = None,
) -> None:
    """Fetch new items from all enabled sources."""
    from briefing.http import Http
    from briefing.ingest import run_ingest

    settings = _settings()
    ing, storage = settings["ingest"], settings["storage"]
    http = Http(
        ing["user_agent"],
        cache_path=ROOT / storage["http_cache_path"],
        ttl_hours=storage["http_cache_ttl_hours"],
        min_interval_s=ing["min_request_interval_s"],
        host_intervals=ing["host_intervals_s"],
        robots_exempt_hosts=set(ing.get("robots_exempt_hosts", [])),
    )
    report = run_ingest(
        _conn(settings),
        http,
        load_yaml("sources.yaml"),
        settings,
        only=set(source) if source else None,
    )
    for sid, r in report.items():
        line = r["error"] if "error" in r else f"{r['new']} new of {r['fetched']} fetched"
        typer.echo(f"{sid:20} {line}")


@app.command()
def rank() -> None:
    """Embed and cluster new items, pre-filter papers, then score relevance."""
    import anthropic

    from briefing import cluster
    from briefing.embed import SentenceTransformerEmbedder
    from briefing.llm import LLM, load_prompt
    from briefing.rank import rank_clusters

    settings = _settings()
    conn = _conn(settings)
    profile, sources_cfg = load_yaml("profile.yaml"), load_yaml("sources.yaml")
    emb_cfg, arxiv_cfg = settings["embeddings"], settings["ingest"]["arxiv"]
    clus_cfg = settings["clustering"]

    secrets = Secrets()
    if not (secrets.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")):
        typer.echo("No API key: set ANTHROPIC_API_KEY in .env (or the environment).")
        raise typer.Exit(code=1)
    client = anthropic.Anthropic(api_key=secrets.anthropic_api_key or None, max_retries=4)
    prices = settings["models"]["prices_per_mtok"]
    rank_cfg = settings["ranking"]

    with db.track_run(conn, "cluster") as stats:
        check_llm = LLM(prices, client=client)
        same_event = cluster.llm_same_event(
            check_llm, load_prompt("same_event"), settings["models"]["fast"], rank_cfg.get("effort")
        )
        embedder = SentenceTransformerEmbedder(emb_cfg["model"], emb_cfg["dims"])
        embedded = cluster.embed_new(conn, embedder)
        pre = cluster.prefilter_papers(
            conn,
            embedder,
            profile,
            sources_cfg,
            arxiv_cfg["theme_similarity_threshold"],
            arxiv_cfg["max_papers_per_day"],
        )
        clu = cluster.cluster_new(
            conn,
            clus_cfg["same_event_threshold"],
            clus_cfg["window_days"],
            borderline_low=clus_cfg["borderline_low"],
            same_event=same_event,
        )
        stats.items_in, stats.items_out = clu["clustered"], clu["new_clusters"]
        stats.tokens_in, stats.tokens_out = check_llm.tokens_in, check_llm.tokens_out
        stats.cost_usd = check_llm.cost_usd
    typer.echo(f"embedded {embedded} · papers {json.dumps(pre)} · clusters {json.dumps(clu)}")

    budget_left = settings["budget_usd"]["daily"] - db.spend_since(
        conn, _local_midnight_iso(settings)
    )
    llm = LLM(prices, client=client)
    with db.track_run(conn, "rank") as stats:
        result = rank_clusters(
            conn, llm, load_prompt("rank_cluster"), profile, settings, budget_left
        )
        stats.items_in, stats.items_out = result["clusters"], result["ranked"]
        stats.tokens_in, stats.tokens_out, stats.cost_usd = (
            llm.tokens_in,
            llm.tokens_out,
            llm.cost_usd,
        )
        if result["budget_hit"]:
            stats.error = "budget_exceeded"
        elif result["failed"]:
            stats.error = "; ".join(result["failed"])[:1000]
    typer.echo(
        f"ranked {result['ranked']}/{result['clusters']} clusters · "
        f"{llm.tokens_in} in / {llm.tokens_out} out tokens · ${llm.cost_usd:.4f}"
        + (" · BUDGET HIT" if result["budget_hit"] else "")
        + (f" · {len(result['failed'])} failed" if result["failed"] else "")
    )


@app.command()
def daily() -> None:
    """Write the daily digest."""
    from briefing.digest.daily import write_daily

    settings = _settings()
    conn = _conn(settings)
    since = _local_midnight_iso(settings)
    notes = []
    if conn.execute(
        "SELECT 1 FROM runs WHERE stage = 'rank' AND error = 'budget_exceeded' AND started_at >= ?",
        (since,),
    ).fetchone():
        notes.append("Daily budget reached: some items were not ranked today.")
    last_ingest = conn.execute(
        """SELECT error FROM runs WHERE stage = 'ingest' AND started_at >= ?
           ORDER BY id DESC LIMIT 1""",
        (since,),
    ).fetchone()
    if last_ingest and last_ingest["error"]:
        notes.append(f"Note: {last_ingest['error']}.")
    with db.track_run(conn, "daily") as stats:
        path, n = write_daily(
            conn,
            load_yaml("profile.yaml"),
            settings,
            ROOT / settings["storage"]["output_dir"],
            notes,
        )
        stats.items_out = n
    typer.echo(f"{n} items -> {path}")


@app.command("run-daily")
def run_daily() -> None:
    """ingest -> rank -> daily, in one go."""
    ingest(source=None)
    rank()
    daily()


@app.command()
def label() -> None:
    """Label items for the relevance eval."""
    _not_yet("M2")


@app.command("eval")
def run_eval() -> None:
    """Run the eval suites."""
    _not_yet("M2")


@app.command()
def enrich() -> None:
    """Write layered explanations for papers that pass the threshold."""
    _not_yet("M3")


@app.command()
def weekly() -> None:
    """Write the weekly briefing."""
    _not_yet("M4")


@app.command()
def audio() -> None:
    """Generate the weekly audio episode and update the feed."""
    _not_yet("M5")
