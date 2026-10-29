import pytest
from typer.testing import CliRunner

from briefing import db
from briefing.cli import app
from briefing.config import load_yaml

EXPECTED_TABLES = {
    "items", "sources", "clusters", "item_embeddings", "rankings", "explanations",
    "concepts", "feedback", "digests", "episodes", "runs",
}


def test_schema_creates_all_tables_and_is_idempotent():
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.init_db(conn)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= names


def test_config_files_parse():
    settings = load_yaml("settings.yaml")
    profile = load_yaml("profile.yaml")
    sources = load_yaml("sources.yaml")
    assert sum(settings["ranking"]["weights"].values()) == pytest.approx(1.0)
    assert {t["id"] for t in profile["themes"]}
    assert all({"id", "kind", "url", "tier"} <= s.keys() for s in sources["sources"])


def test_cli_help():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "ingest" in result.output
