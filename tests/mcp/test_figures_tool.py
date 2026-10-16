"""The `figures` tool: a recurring report's number, its period, the one before, and its cell.

Numbers were the owner's costliest questions and answers ended in "give me your best
estimate"; the tool answers from the registry with the edition, sheet and cell cited, and says
"not held; latest held: <period>" rather than leave a gap to be filled by a guess.
"""

import sqlite3

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src import mcp_server
from tests import seeded_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = seeded_store.build(tmp_path / "brain.db")
    seeded_store.serve(path, monkeypatch)
    return path


def test_a_metric_answers_with_its_latest_and_previous_period_and_its_cell(store):
    out = mcp_server.figures(query="okapi sightings")

    assert out["data_as_of"] is not None and out["mode"] == "figures"
    (row,) = out["result"]
    assert (row["latest"]["value"], row["latest"]["period"]) == (42, "2026-09")
    assert (row["previous"]["value"], row["previous"]["period"]) == (40, "2026-08")
    assert row["latest"]["source"] == "attachment:1" and row["latest"]["cell"] == "R2C3"
    assert (row["kind"], row["ref"]) == ("attachment", 1)
    assert row["series"] == "Okapi report"


def test_a_period_held_is_answered_and_one_not_held_says_so(store):
    held = mcp_server.figures(query="okapi", period="2026-08")
    missing = mcp_server.figures(query="okapi", period="Οκτώβριος 2026")

    assert held["result"][0]["held"] is True and held["result"][0]["latest"]["value"] == 40
    assert missing["reason"] == "2026-10 not held; latest held: 2026-09"
    assert missing["result"][0]["latest"]["period"] == "2026-09"


def test_the_series_mode_lists_the_reports_held(store):
    out = mcp_server.figures(mode="series")

    (row,) = out["result"]
    assert (row["series"], row["editions"], row["latest_period"]) == ("Okapi report", 3, "2026-09")
    assert row["kind"] == "attachment"


def test_nothing_matched_says_why_and_what_the_registry_holds(store):
    out = mcp_server.figures(query="zebra crossings")

    assert out["result"] == [] and "No figure matched" in out["reason"]
    assert out["coverage"]["figures"]["series"] == 1
    assert "attachments" in out["coverage"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [({}, "query is required"), ({"query": "okapi", "period": "soon"}, "period must name")],
)
def test_a_call_that_cannot_be_answered_says_what_to_change(store, arguments, message):
    with pytest.raises(ToolError, match=message):
        mcp_server.figures(**arguments)


def test_a_replica_older_than_the_registry_says_how_it_comes(store):
    conn = sqlite3.connect(store)
    for table in ("figure_metrics_fts", "figure_restated", "figure_values"):
        conn.execute(f"DROP TABLE {table}")
    conn.commit()
    conn.close()

    out = mcp_server.figures(query="okapi")

    assert out["result"] == [] and "figures-backfill" in out["reason"]


def test_the_instructions_route_numbers_to_figures():
    assert "`figures` first" in mcp_server._instructions()
