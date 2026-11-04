"""The sixteen read commands that mirrored the MCP tools, deprecated for one release.

Each still takes its old arguments, prints one line on stderr naming the `brain tool`
call that replaces it, and makes that call, so its answer is the tool's JSON and a
script that ran it still gets an answer and an exit code. A limit is held to the
tools' 1..200, as the server holds a caller that skips the input schema.
"""

import json
import shlex

import pytest

from src import cli, mcp_call
from src.store.schema import get_connection
from tests import seeded_store
from tests.brain_cli import brain
from tests.mcp_listing import allow_an_event_loop, registered_tool

# (the old command line, the tool it now calls, the arguments it passes)
SHIMS = [
    (["query", "thread", "1"], "read", {"ref": "email:1", "part": "thread"}),
    (["query", "thread", "1", "--limit", "0"], "read", {"ref": "email:1", "part": "thread"}),
    (
        ["query", "person", "Alice", "--limit", "5", "-v"],
        "search",
        {"filter": {"person": "Alice"}, "limit": 5},
    ),
    (["query", "topic", "okapi"], "search", {"filter": {"topic": "okapi"}, "limit": 20}),
    (["query", "keyword", "okapi plan"], "search", {"query": "okapi plan", "limit": 20}),
    (
        ["query", "date", "2026-09-01", "2026-10-01"],
        "search",
        {"filter": {"since": "2026-09-01", "until": "2026-10-01"}, "limit": 20},
    ),
    (
        ["query", "decisions", "--topic", "okapi"],
        "query_items",
        {"kind": "decisions", "filter": {"topic": "okapi"}, "limit": 20},
    ),
    (
        ["query", "decisions", "--person", "Bob", "--limit", "500"],
        "query_items",
        {"kind": "decisions", "filter": {"person": "Bob"}, "limit": 200},
    ),
    (["query", "actions"], "query_items", {"kind": "actions", "status": "open", "limit": 20}),
    (
        ["query", "actions", "--owner", "Alice", "--status", "expired"],
        "query_items",
        {"kind": "actions", "status": "expired", "filter": {"person": "Alice"}, "limit": 20},
    ),
    (
        ["query", "combined", "--person", "Alice", "--keyword", "okapi", "--start", "2026-09-01"],
        "search",
        {"query": "okapi", "filter": {"person": "Alice", "since": "2026-09-01"}, "limit": 20},
    ),
    (
        ["query", "semantic", "the okapi plan"],
        "search",
        {"query": "the okapi plan", "search_type": "semantic", "limit": 20},
    ),
    (
        ["prep", "alice@example.com, bob@example.com", "--topic", "okapi", "--days", "30"],
        "meeting_prep",
        {"people": "alice@example.com, bob@example.com", "filter": {"topic": "okapi", "days": 30}},
    ),
    (
        ["prep", "alice@example.com", "--limit", "3"],
        "meeting_prep",
        {"people": "alice@example.com", "filter": {"days": 365}},
    ),
    (
        ["stale"],
        "query_items",
        {"kind": "stale", "older_than_days": 5, "filter": {"days": 30}, "limit": 20},
    ),
    (
        ["stale", "--days", "3", "--max-days", "60", "--limit", "5"],
        "query_items",
        {"kind": "stale", "older_than_days": 3, "filter": {"days": 60}, "limit": 5},
    ),
    (["stats"], "stats", {}),
    (
        ["teams-search", "okapi", "--kind", "thread", "--limit", "5"],
        "search",
        {"query": "okapi", "source": "teams", "limit": 5},
    ),
    (["teams-thread", "1"], "read", {"ref": "teams:1"}),
    (["teams-stats"], "sql_query", {"sql": cli.TEAMS_STATS_SQL}),
    (["sharepoint-refused"], "sharepoint_index", {"operation": "list_stale"}),
]
IDS = [" ".join(argv) for argv, _, _ in SHIMS]


def test_every_read_command_of_the_mirror_is_covered():
    """The sixteen: nine query types and seven commands (teams-chat went with its tool)."""
    commands = {tuple(argv[:2]) if argv[0] == "query" else (argv[0],) for argv, _, _ in SHIMS}

    assert len(commands) == 16


@pytest.mark.parametrize(("argv", "tool", "arguments"), SHIMS, ids=IDS)
def test_each_old_command_calls_its_tool_with_its_arguments(
    argv, tool, arguments, monkeypatch, capsys
):
    calls = []
    answer = {"data_as_of": "2026-10-11T06:00:00Z", "stale": False, "result": []}

    def tool_call(name, given):
        calls.append((name, given))
        return answer

    monkeypatch.setattr(mcp_call, "call", tool_call)
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)

    code, out, err = brain(monkeypatch, capsys, *argv)

    assert code == 0
    assert calls == [(tool, arguments)]
    assert json.loads(out) == answer
    line = err.strip()
    assert "\n" not in line
    assert "deprecated" in line
    replacement = shlex.split(line.split("use ", 1)[1])
    assert replacement[:3] == ["brain", "tool", tool]
    assert (json.loads(replacement[3]) if len(replacement) > 3 else {}) == arguments


@pytest.mark.parametrize(("argv", "tool", "arguments"), SHIMS, ids=IDS)
def test_each_mapping_is_input_the_tool_accepts(argv, tool, arguments):
    registered_tool(tool).fn_metadata.validate_arguments(arguments)


def test_another_store_is_named_in_the_replacement(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(mcp_call, "call", lambda name, given: {"data_as_of": "x", "stale": False})
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    other = tmp_path / "copy.db"

    _, _, err = brain(monkeypatch, capsys, "--db", str(other), "stats")

    assert shlex.split(err.split("use ", 1)[1]) == ["brain", "--db", str(other), "tool", "stats"]


# ------------------------------------------------- the answers, through the real call


@pytest.fixture
def store(tmp_path, monkeypatch):
    allow_an_event_loop(monkeypatch)
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    path = seeded_store.build(tmp_path / "brain.db")
    seeded_store.serve(path, monkeypatch)
    return path


def test_stats_now_says_how_fresh_and_how_far_back(store, monkeypatch, capsys):
    code, out, _ = brain(monkeypatch, capsys, "--db", str(store), "stats")

    answer = json.loads(out)
    assert code == 0
    assert list(answer)[:2] == ["data_as_of", "stale"]
    assert {"age_hours", "coverage", "whatsapp_messages", "calendar_events"} <= set(answer)


def test_query_thread_answers_with_the_thread(store, monkeypatch, capsys):
    code, out, _ = brain(monkeypatch, capsys, "--db", str(store), "query", "thread", "2")

    answer = json.loads(out)
    assert code == 0
    assert [e["ref"] for e in answer["item"]["thread"]] == [1, 2]


def test_keyword_search_flags_rows_holding_only_some_words(store, monkeypatch, capsys):
    code, out, _ = brain(
        monkeypatch, capsys, "--db", str(store), "query", "keyword", "okapi ανύπαρκτηλέξη"
    )

    rows = json.loads(out)["result"]
    assert code == 0
    assert rows
    assert all(row["partial_match"] for row in rows)


def test_teams_search_flags_rows_holding_only_some_words(store, monkeypatch, capsys):
    code, out, _ = brain(
        monkeypatch, capsys, "--db", str(store), "teams-search", "okapi ανύπαρκτηλέξη"
    )

    rows = json.loads(out)["result"]
    assert code == 0
    assert rows
    assert all(row["partial_match"] for row in rows)


def test_prep_leaves_out_an_empty_name(store, monkeypatch, capsys):
    code, out, _ = brain(monkeypatch, capsys, "--db", str(store), "prep", "alice@example.com, ")

    assert code == 0
    assert len(json.loads(out)["attendees"]) == 1


def test_teams_stats_reports_what_it_reported_before(store, monkeypatch, capsys):
    from src.store.teams_query import stats

    conn = get_connection(str(store))
    before = stats(conn)
    conn.close()

    code, out, _ = brain(monkeypatch, capsys, "--db", str(store), "teams-stats")

    answer = json.loads(out)
    (row,) = answer["rows"]
    assert code == 0
    assert dict(zip(answer["columns"], row, strict=True)) == before


def test_a_read_command_on_a_replica_writes_nothing_beside_the_copy(
    store, tmp_path, monkeypatch, capsys
):
    """The old commands opened the copy through get_connection, a locking read that
    leaves a -wal and a -shm beside it for the next pull to find."""
    from src import config

    stamp = tmp_path / "db-pull.stamp"
    stamp.write_text("2026-10-11T06:00:00+00:00")
    monkeypatch.setattr(config, "REPLICA_STAMP", stamp)
    monkeypatch.delenv("BRAIN_ROLE")

    code, _, _ = brain(monkeypatch, capsys, "--db", str(store), "stale")

    assert code == 0
    assert sorted(p.name for p in store.parent.glob("brain.db*")) == ["brain.db"]
