"""Every tool result opens with how current the store it was read from is.

Only recall said so. search_emails, search_attachments and sql_query, 70% of real
calls, carried no data_as_of, and a replica that is hours behind every night by
schedule read like a quiet inbox: "nothing since Tuesday" off a seven-hour-old copy
looks exactly like nothing since Tuesday.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src import mcp_server
from src.mcp_budget import RESPONSE_BUDGET_CHARS, json_chars
from tests import seeded_store
from tests.mcp_listing import listed_tools, registered_tool

HEADER = ["data_as_of", "stale"]
SYNCED = "2026-10-11T06:00:00Z"


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = seeded_store.build(tmp_path / "brain.db")
    seeded_store.serve(path, monkeypatch)
    return path


def _opening(name: str) -> list[str]:
    # recall leads with its summary, which says how far to trust the rest.
    return ["summary", *HEADER] if name == "recall" else HEADER


def test_every_tool_declares_the_header_in_its_output_schema():
    for name, tool in listed_tools().items():
        schema = tool.output_schema or {}
        assert set(HEADER) <= set(schema.get("properties", {})), name
        assert set(HEADER) <= set(schema.get("required", [])), name


@pytest.mark.parametrize("name", sorted(seeded_store.CALLS))
def test_every_result_opens_with_the_header(store, name):
    result = getattr(mcp_server, name)(**seeded_store.CALLS[name])

    sent = registered_tool(name).fn_metadata.convert_result(result)

    opening = _opening(name)
    assert list(result)[: len(opening)] == opening
    assert list(sent.structured_content)[: len(opening)] == opening
    assert result["stale"] is False
    assert "_stale_warning" not in result


@pytest.mark.parametrize("name", sorted(set(seeded_store.CALLS) - {"outlook_live_search"}))
def test_the_header_is_the_stores_own_freshness(tmp_path, monkeypatch, name):
    path = seeded_store.build(tmp_path / "brain.db", synced=SYNCED)
    seeded_store.serve(path, monkeypatch)

    result = getattr(mcp_server, name)(**seeded_store.CALLS[name])

    assert result["data_as_of"] == SYNCED


@pytest.mark.parametrize("name", sorted(set(seeded_store.CALLS) - {"outlook_live_search"}))
def test_a_stale_store_says_so_beside_the_header(tmp_path, monkeypatch, name):
    path = seeded_store.build(tmp_path / "brain.db", synced="2026-01-01T00:00:00Z")
    seeded_store.serve(path, monkeypatch)

    result = getattr(mcp_server, name)(**seeded_store.CALLS[name])

    keys = list(result)
    assert result["stale"] is True
    assert keys[keys.index("stale") + 1] == "_stale_warning"
    assert "2026-01-01T00:00:00Z" in result["_stale_warning"]


def test_the_live_mailbox_is_as_of_the_call(store):
    before = datetime.now(UTC) - timedelta(seconds=1)

    result = mcp_server.outlook_live_search()

    as_of = datetime.fromisoformat(result["data_as_of"].replace("Z", "+00:00"))
    assert before <= as_of <= datetime.now(UTC) + timedelta(seconds=1)
    assert result["stale"] is False


def test_sql_tools_read_the_header_from_the_store_they_query(tmp_path, monkeypatch):
    """The sql tests point only sql_readonly at a store; the header follows it there."""
    from src.store import sql_readonly

    path = seeded_store.build(tmp_path / "brain.db", synced=SYNCED)
    monkeypatch.setattr(sql_readonly, "DEFAULT_DB", path)
    monkeypatch.setattr(mcp_server, "DEFAULT_DB", tmp_path / "absent.db")

    assert mcp_server.sql_query("SELECT COUNT(*) AS n FROM emails")["data_as_of"] == SYNCED
    assert mcp_server.sql_schema()["data_as_of"] == SYNCED


def test_query_actions_answers_with_rows_under_result(store):
    """A bare list could carry no header; its structured content was {"result": [...]}
    already, as the five list tools #183 converted."""
    result = mcp_server.query_actions()

    sent = registered_tool("query_actions").fn_metadata.convert_result(result)

    assert [row["task"] for row in result["result"]] == [
        "Draft the okapi budget",
        "Book the okapi room",
    ]
    assert sent.structured_content["result"] == result["result"]


def test_no_action_matched_is_an_empty_list_with_a_reason(store):
    result = mcp_server.query_actions(owner="Nobody")

    assert result["result"] == []
    assert result["reason"]


def test_the_budget_counts_the_header(tmp_path, monkeypatch):
    """Rows of about 200 characters land the cut within a row of the budget, so a
    header added after the cut would push a stale answer over it."""
    path = seeded_store.build(tmp_path / "brain.db", synced="2026-01-01T00:00:00Z")
    from src.store.schema import get_connection

    conn = get_connection(str(path))
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
    seeded_store.serve(path, monkeypatch)

    result = mcp_server.search_attachments("okapi", limit=200)

    assert result["truncated"]["result"]["total"] == 200
    assert result["_stale_warning"]
    assert json_chars(result) <= RESPONSE_BUDGET_CHARS
