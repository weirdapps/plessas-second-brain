"""`brain tool <name> <json>`: any MCP tool from the command line, through the server's call.

The CLI mirrored the read tools by hand, seventeen commands over the same store
functions, and fell behind them: its `stats` printed seven counts without the
freshness, coverage or staleness the tool returns, and nothing read WhatsApp, the
calendar, recall, conversations or SQL. `brain tool` calls a tool by name through
MCPServer.call_tool, the call the server makes for tools/call, so the input schema,
the read-only connection, the freshness header and the response budget are the
tool's own, and it prints the structured result the server would send, as JSON.
"""

import json

import pytest

from src import cli, config, mcp_server
from src.mcp_budget import RESPONSE_BUDGET_CHARS, json_chars
from src.store.schema import get_connection
from tests import seeded_store
from tests.brain_cli import brain
from tests.mcp_listing import allow_an_event_loop, listed_tools, registered_tool


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A store with an item of every kind; each call names it with --db."""
    allow_an_event_loop(monkeypatch)
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    path = seeded_store.build(tmp_path / "brain.db")
    seeded_store.serve(path, monkeypatch)
    return path


def _schema_in(err: str) -> dict:
    """The input schema an error prints after its message."""
    return json.loads(err[err.index("\n{") + 1 :])


def test_a_tool_prints_the_answer_the_server_sends(store, monkeypatch, capsys):
    arguments = {"query": "okapi"}

    code, out, err = brain(
        monkeypatch, capsys, "--db", str(store), "tool", "search_emails", json.dumps(arguments)
    )

    sent = registered_tool("search_emails").fn_metadata.convert_result(
        mcp_server.search_emails(**arguments)
    )
    assert (code, err) == (0, "")
    assert json.loads(out) == sent.structured_content


def test_the_answer_opens_with_the_freshness_the_old_stats_lacked(store, monkeypatch, capsys):
    code, out, _ = brain(monkeypatch, capsys, "--db", str(store), "tool", "stats")

    answer = json.loads(out)
    assert code == 0
    assert list(answer)[:2] == ["data_as_of", "stale"]
    assert {"age_hours", "coverage", "whatsapp_messages"} <= set(answer)


def test_the_answer_is_held_to_the_response_budget(store, monkeypatch, capsys):
    conn = get_connection(str(store))
    for n in range(10, 210):
        conn.execute(
            "INSERT INTO attachments (id, email_id, message_id, filename, file_path, "
            "exported_at, mime_type, file_size) VALUES (?, 1, 1, ?, '/x', '2026-10-01', "
            "'application/pdf', 1)",
            (n, f"okapi{n}.pdf"),
        )
        conn.execute(
            "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_status, "
            "summary) VALUES (?, 'okapi', 'done', ?)",
            (n, "okapi " * 20),
        )
    conn.commit()
    conn.close()

    code, out, _ = brain(
        monkeypatch,
        capsys,
        "--db",
        str(store),
        "tool",
        "search_attachments",
        '{"query": "okapi", "limit": 200}',
    )

    answer = json.loads(out)
    assert code == 0
    assert answer["truncated"]["result"]["total"] == 200
    assert json_chars(answer) <= RESPONSE_BUDGET_CHARS


def test_an_unknown_tool_exits_2_and_names_the_tools(store, monkeypatch, capsys):
    code, out, err = brain(
        monkeypatch, capsys, "--db", str(store), "tool", "recal", '{"query": "okapi"}'
    )

    assert (code, out) == (2, "")
    assert "recal" in err
    assert all(name in err for name in listed_tools())


def test_input_the_schema_rejects_exits_2_with_the_schema(store, monkeypatch, capsys):
    code, out, err = brain(
        monkeypatch, capsys, "--db", str(store), "tool", "email_thread", '{"email_id": "seven"}'
    )

    assert (code, out) == (2, "")
    assert "email_id" in err.splitlines()[0]
    assert _schema_in(err) == listed_tools()["email_thread"].input_schema


@pytest.mark.parametrize("raw", ["[1]", "not json", '"okapi"', "{"])
def test_the_arguments_must_be_one_json_object(store, monkeypatch, capsys, raw):
    code, out, err = brain(monkeypatch, capsys, "--db", str(store), "tool", "search_emails", raw)

    assert (code, out) == (2, "")
    assert _schema_in(err) == listed_tools()["search_emails"].input_schema


def test_a_tool_that_refuses_exits_1_with_its_own_words(store, monkeypatch, capsys):
    """What the server sends a client as an error result, and nothing on stdout."""
    code, out, err = brain(monkeypatch, capsys, "--db", str(store), "tool", "query_emails", "{}")

    assert (code, out) == (1, "")
    assert "at least one filter" in err


def test_a_missing_store_exits_1_and_names_it(store, tmp_path, monkeypatch, capsys):
    absent = tmp_path / "absent" / "brain.db"

    code, out, err = brain(monkeypatch, capsys, "--db", str(absent), "tool", "stats")

    assert (code, out) == (1, "")
    assert str(absent) in err


def test_db_points_the_tools_at_that_store_for_the_call_only(store, tmp_path, monkeypatch, capsys):
    other = seeded_store.build(tmp_path / "other.db", synced="2026-02-02T02:02:02Z")

    code, out, _ = brain(monkeypatch, capsys, "--db", str(other), "tool", "stats")

    assert code == 0
    assert json.loads(out)["data_as_of"] == "2026-02-02T02:02:02Z"
    assert mcp_server.DEFAULT_DB == store


def test_list_gives_every_tool_as_a_client_lists_it(store, monkeypatch, capsys):
    code, out, _ = brain(monkeypatch, capsys, "tool", "--list")

    listed = {tool["name"]: tool for tool in json.loads(out)["tools"]}
    client = listed_tools()
    assert code == 0
    assert set(listed) == set(client)
    for name, tool in listed.items():
        assert tool["description"] == client[name].description
        assert tool["inputSchema"] == client[name].input_schema
        assert "outputSchema" not in tool
    assert listed["recall"]["annotations"]["readOnlyHint"] is True
    assert listed["sharepoint_index"]["annotations"]["readOnlyHint"] is False


@pytest.mark.parametrize("argv", [["tool"], ["tool", "stats", "--list"]])
def test_a_call_names_one_tool_or_asks_for_the_list(store, monkeypatch, capsys, argv):
    code, out, err = brain(monkeypatch, capsys, *argv)

    assert (code, out) == (2, "")
    assert err


@pytest.fixture
def replica(store, tmp_path, monkeypatch):
    """This host holds a pulled copy: the pull's stamp exists and BRAIN_ROLE is unset."""
    stamp = tmp_path / "db-pull.stamp"
    stamp.write_text("2026-10-11T06:00:00+00:00")
    monkeypatch.setattr(config, "REPLICA_STAMP", stamp)
    monkeypatch.delenv("BRAIN_ROLE")
    return store


def test_a_replica_answers_without_writing_beside_its_copy(replica, monkeypatch, capsys):
    """The open a pulled copy gets from the server: no -wal or -shm for the next pull to
    find beside the file it replaces."""
    code, out, _ = brain(monkeypatch, capsys, "--db", str(replica), "tool", "stats")

    assert code == 0
    assert json.loads(out)["data_as_of"]
    assert sorted(p.name for p in replica.parent.glob("brain.db*")) == ["brain.db"]


def test_a_replica_refuses_the_one_write_as_the_server_does(replica, monkeypatch, capsys):
    """sharepoint_index's refetch writes, and the tool itself refuses it on a replica."""
    code, out, err = brain(
        monkeypatch,
        capsys,
        "--db",
        str(replica),
        "tool",
        "sharepoint_index",
        '{"operation": "refetch", "url": "https://contoso.sharepoint.com/x"}',
    )

    assert (code, out) == (1, "")
    assert "replica" in err
