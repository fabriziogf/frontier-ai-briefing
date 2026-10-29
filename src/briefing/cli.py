"""Command line entry point: `briefing <command>`."""

from pathlib import Path

import typer

from briefing import db
from briefing.config import load_yaml

app = typer.Typer(help="Frontier AI briefing pipeline.", no_args_is_help=True)


def _not_yet(milestone: str) -> None:
    typer.echo(f"Not implemented yet (planned for {milestone}).")
    raise typer.Exit(code=1)


@app.command("init-db")
def init_db() -> None:
    """Create the SQLite database and all tables."""
    path = Path(load_yaml("settings.yaml")["storage"]["db_path"])
    conn = db.connect(path)
    db.init_db(conn)
    typer.echo(f"Database ready at {path}")


@app.command()
def ingest() -> None:
    """Fetch new items from all enabled sources."""
    _not_yet("M1")


@app.command()
def rank() -> None:
    """Cluster new items and score relevance."""
    _not_yet("M1")


@app.command()
def daily() -> None:
    """Write the daily digest."""
    _not_yet("M1")


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
