"""What the MCP tools promise a client before and after a call.

No tool declared annotations, so Claude Code ran every brain call serially and
treated all of them as possibly destructive. Input schemas carried a title per
property and nothing else: free strings where only two or three values work (a
model sent 'Semantic' once) and no bounds on any limit. Dict results had no
output schema, so they went out as indented JSON only. Failures came back three
ways, most of them flagged as success. The server instructions ran past the
2,048 characters Claude Code kept of them.
"""

import json
import re
import sqlite3
from datetime import UTC, datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from src import config, mcp_server
from src.store.schema import create_database
from tests.mcp_listing import listed_tools, registered_tool

# ------------------------------------------------------------- tools/list


def test_the_tool_count_is_unchanged():
    # 27 until recall_preference and recent_conversations were retired, 25 until
    # get_item, 26 until topic_page, 27 until figures; five of those 28 are deprecated
    # and go in the next release.
    assert len(listed_tools()) == 28


def test_every_tool_says_whether_it_reads_or_writes():
    for name, tool in listed_tools().items():
        hints = tool.annotations
        assert hints is not None, name
        assert hints.idempotent_hint is True, name
        if name == "sharepoint_index":
            # Its refetch fetches from SharePoint and records the result.
            assert (hints.read_only_hint, hints.destructive_hint) == (False, False)
            assert hints.open_world_hint is True
        elif name == "outlook_live_search":
            assert (hints.read_only_hint, hints.open_world_hint) == (True, True), name
        else:
            assert (hints.read_only_hint, hints.open_world_hint) == (True, False), name


def test_every_tool_declares_an_output_schema():
    missing = [name for name, tool in listed_tools().items() if not tool.output_schema]

    assert missing == []


def test_every_parameter_says_what_it_is():
    for name, tool in listed_tools().items():
        for param, schema in tool.input_schema.get("properties", {}).items():
            assert schema.get("description"), f"{name}.{param}"


def test_every_limit_is_bounded_in_the_schema():
    for name, tool in listed_tools().items():
        for param, schema in tool.input_schema.get("properties", {}).items():
            if param in ("limit", "limit_per_kind"):
                ceiling = 10 if name == "recall" else 200
                assert (schema.get("minimum"), schema.get("maximum")) == (1, ceiling), name


@pytest.mark.parametrize(
    "tool, param, values",
    [
        ("search_emails", "search_type", ["keyword", "semantic"]),
        ("search_conversations", "search_type", ["keyword", "semantic"]),
        ("search_teams", "kind", ["thread", "message", "both"]),
        ("sharepoint_index", "operation", ["list_stale", "list_unfetched", "refetch"]),
        ("query_actions", "status", ["open", "expired", "done"]),
    ],
)
def test_the_choices_are_enums(tool, param, values):
    assert listed_tools()[tool].input_schema["properties"][param]["enum"] == values


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("search_emails", {"query": "x", "search_type": "Semantic"}),
        ("search_teams", {"query": "x", "kind": "threads"}),
        ("query_actions", {"status": "completed"}),
        ("search_emails", {"query": "x", "limit": 0}),
        ("search_attachments", {"query": "x", "limit": 201}),
        ("recall", {"query": "x", "limit_per_kind": 11}),
        ("search_whatsapp", {"query": "x", "days": 0}),
    ],
)
def test_the_schema_refuses_what_the_tool_cannot_honour(tool, arguments):
    with pytest.raises(ValidationError):
        registered_tool(tool).fn_metadata.validate_arguments(arguments)


# sql_query carries a third of all calls and loads upfront: the columns agents guessed
# wrong (50 failed calls in six weeks) and the full-text form are worth the length.
_LONGER = {"sql_query": 2000}


def test_descriptions_are_short_and_carry_no_history():
    for name, tool in listed_tools().items():
        text = tool.description or ""
        assert len(text) < _LONGER.get(name, 1000), name
        # Python 3.12 keeps a docstring's indentation; the client gets it dedented.
        assert "\n    " not in text, name
        assert "TODO" not in text, name
        assert not re.search(r"\b(?:until|since|on) 20\d\d-\d\d-\d\d", text), name


@pytest.mark.parametrize("replica", [True, False])
def test_the_instructions_fit_and_lead_with_routing(tmp_path, monkeypatch, replica):
    stamp = tmp_path / "db-pull.stamp"
    if replica:
        stamp.write_text("pulled")
    monkeypatch.setattr(mcp_server, "REPLICA_STAMP", stamp)

    text = mcp_server._instructions()

    assert len(text) <= 4000  # Claude Code keeps 4,096 since v2.1.296
    assert text.startswith("Routing.")
    assert "Not covered:" in text


# -------------------------------------------------------------- tools/call


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A store with a row or two of every kind a tool reads, all about 'okapi'."""
    from src.store import sql_readonly

    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.executescript(
        f"""
        INSERT INTO people (id, name, email) VALUES (1, 'Alice Smith', 'alice@example.com');
        INSERT INTO topics (id, name, display_name) VALUES (1, 'okapi', 'Okapi');
        INSERT INTO emails (id, message_id, date_received, subject, summary, content,
                            sender_name, sender_address, conversation_id, mailbox_name)
            VALUES (1, 1, '{now}', 'Okapi plan', 'The okapi plan', 'okapi body',
                    'Alice Smith', 'alice@example.com', 'c1', 'Inbox');
        INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (1, 1, 'sender');
        INSERT INTO email_topics (email_id, topic_id) VALUES (1, 1);
        INSERT INTO decisions (email_id, decision, decided_by, decision_date)
            VALUES (1, 'Adopt the okapi plan', 'Alice Smith', date('now'));
        INSERT INTO action_items (email_id, task, owner, deadline, status)
            VALUES (1, 'Draft the okapi budget', 'Alice Smith', date('now', '+5 days'), 'open');
        INSERT INTO attachments (id, email_id, message_id, filename, file_path, exported_at,
                                 mime_type, file_size)
            VALUES (1, 1, 1, 'okapi.pdf', '/x', '{now}', 'application/pdf', 1);
        INSERT INTO attachment_content (attachment_id, extracted_text, extraction_status,
                                        summary)
            VALUES (1, 'the okapi figures', 'done', 'Okapi figures');
        INSERT INTO calendar_events (id, outlook_event_id, subject, start_at, end_at,
                                     is_recurring, is_self_organized, is_cancelled,
                                     ingested_at, llm_status, response_status)
            VALUES (1, 'ev1', 'Okapi review', '{now}', '{now}', 0, 0, 0, '{now}',
                    'extracted', 'Accepted');
        INSERT INTO event_attendees (event_id, email, name, response_status, is_self)
            VALUES (1, 'alice@example.com', 'Alice Smith', 'Accepted', 0);
        INSERT INTO teams_chats (id, teams_chat_id, chat_kind, first_seen_at)
            VALUES (1, '19:x', 'channel', '{now}');
        INSERT INTO teams_threads (id, chat_id, thread_kind, started_at, ended_at,
                                   message_count, title, summary, extraction_status)
            VALUES (1, 1, 'channel_post', '{now}', '{now}', 1, 'Okapi thread',
                    'The okapi thread', 'extracted');
        INSERT INTO conversations (id, session_id, started_at, ended_at, project_name,
                                   turn_count, summary, created_at)
            VALUES (1, 's1', '{now}', '{now}', 'okapi', 1, 'An okapi session', '{now}');
        INSERT INTO conversation_turns (conversation_id, turn_index, timestamp, speaker,
                                        content)
            VALUES (1, 0, '{now}', 'user', 'the okapi question');
        """
    )
    conn.commit()
    conn.close()
    # The store itself, not a stand-in connection: every call goes through the open a
    # client's call goes through.
    monkeypatch.setattr(mcp_server, "DEFAULT_DB", path)
    monkeypatch.setattr(sql_readonly, "DEFAULT_DB", path)
    monkeypatch.setattr("src.export.outlook_cli.run_outlook_cli", lambda args: [])
    return path


CALLS = {
    "person_context": {"name_or_email": "Alice"},
    "topic_context": {"topic": "okapi"},
    "topic_page": {"topic": "okapi"},
    "sender_brief": {"name_or_email": "alice@example.com"},
    "email_thread": {"email_id": 1},
    "search_emails": {"query": "okapi"},
    "recall": {"query": "okapi", "include_context": True},
    "query_emails": {"keyword": "okapi"},
    "query_decisions": {},
    "query_actions": {},
    "stale_threads": {},
    "meeting_prep": {"people": "Alice Smith", "topic": "okapi"},
    "search_attachments": {"query": "okapi"},
    "figures": {"query": "okapi"},
    "query_calendar_events": {"keyword": "okapi"},
    "stats": {},
    "search_conversations": {"query": "okapi"},
    "conversation_context": {"session_id": "s1"},
    "sharepoint_index": {"operation": "list_stale"},
    "attachment_image_search": {"query": "okapi"},
    "outlook_live_search": {},
    "search_teams": {"query": "okapi"},
    "search_whatsapp": {"query": "okapi"},
    "teams_thread_context": {"thread_id": 1},
    "teams_chat_summary": {"chat_id": 1},
    "sql_query": {"sql": "SELECT id, subject FROM emails"},
    "sql_schema": {"table": "emails"},
    "get_item": {"kind": "email", "ref": 1},
}


def test_the_sample_calls_cover_every_tool():
    assert set(CALLS) == set(listed_tools())


@pytest.mark.parametrize("name", sorted(CALLS))
def test_every_result_goes_out_whole_as_structured_content(store, name):
    """Output validation runs on every call: a key the schema does not list must
    still go through, and a value of the wrong type would turn the call into an
    error."""
    result = getattr(mcp_server, name)(**CALLS[name])

    sent = registered_tool(name).fn_metadata.convert_result(result)

    assert sent.structured_content is not None
    if isinstance(result, dict):
        assert sent.structured_content == json.loads(json.dumps(result, default=str))
    else:
        assert sent.structured_content == {"result": json.loads(json.dumps(result, default=str))}


@pytest.mark.parametrize(
    "call, hint",
    [
        (lambda: mcp_server.email_thread(email_id=999), "search_emails"),
        (lambda: mcp_server.teams_thread_context(thread_id=999), "search_teams"),
        (lambda: mcp_server.teams_chat_summary(chat_id=999), "search_teams"),
        (lambda: mcp_server.conversation_context(session_id="nope"), "search_conversations"),
        (lambda: mcp_server.sql_schema(table="nope"), "sql_schema"),
        (lambda: mcp_server.query_emails(), "search_emails"),
        (lambda: mcp_server.sql_query(sql="SELECT nope FROM emails"), "no such column"),
    ],
)
def test_a_failure_is_a_tool_error_that_says_what_to_do(store, call, hint):
    """An unknown id gave an empty list, a not-found came back as a normal result
    with an `error` key, and a missing filter as a bare 'Error executing tool'."""
    with pytest.raises(ToolError, match=hint):
        call()


@pytest.mark.parametrize(
    "call, key",
    [
        (lambda: mcp_server.search_emails("zzyzx"), "result"),
        (lambda: mcp_server.search_attachments("zzyzx"), "result"),
        (lambda: mcp_server.query_emails(keyword="zzyzx"), "result"),
        (lambda: mcp_server.query_decisions(topic="zzyzx"), "result"),
        (lambda: mcp_server.search_conversations("zzyzx"), "result"),
        (lambda: mcp_server.search_teams("zzyzx"), "results"),
        (lambda: mcp_server.search_whatsapp("zzyzx"), "results"),
        (lambda: mcp_server.query_calendar_events(keyword="zzyzx"), "events"),
        (lambda: mcp_server.attachment_image_search("zzyzx"), "results"),
    ],
)
def test_nothing_matched_is_an_empty_list_with_a_reason(store, call, key):
    out = call()

    assert out[key] == []
    assert out["reason"]


def test_a_recall_that_found_nothing_says_why(store):
    out = mcp_server.recall("zzyzx")

    assert out["summary"]["total_hits"] == 0
    assert out["reason"]


def test_an_unknown_person_or_topic_says_why(store):
    assert mcp_server.person_context("Nobody")["reason"]
    assert mcp_server.topic_context("zzyzx")["reason"]


# ------------------------------------------------------ how a call opens the store

# Calls that open brain.db read-write. Every other call reads through the read-only
# open, so a name added here is a decision about who may write, never drift.
READ_WRITE = {"sharepoint_index refetch"}
LINK = "https://tenant.example.com/sites/Team/Shared%20Documents/Plan.txt"


@pytest.mark.parametrize("on_replica", [False, True], ids=["producer", "replica"])
def test_only_the_refetch_opens_the_store_read_write(store, tmp_path, monkeypatch, on_replica):
    """readOnlyHint promises a client that a call changes nothing; this holds every tool to it.

    Each sample call runs against the store, and every sqlite3.connect is recorded as
    read-only (a mode=ro URI) or read-write.
    """
    from src.export import sharepoint_fetcher
    from src.export.sharepoint_fetcher import SharepointFetchResult

    seed = sqlite3.connect(store)
    try:
        sharepoint_fetcher.record_link_in_db(seed, url=LINK, message_id="1", status="http-error")
    finally:
        seed.close()
    monkeypatch.setattr(config, "SHAREPOINT_HOST", "tenant.example.com")
    monkeypatch.setattr(
        sharepoint_fetcher,
        "fetch_sharepoint_link",
        lambda url, out_dir, managed_host=None: SharepointFetchResult(
            url=url, status="http-error", error_message="refused"
        ),
    )
    monkeypatch.setattr(config, "REPLICA_STAMP", tmp_path / "no-pull-stamp")
    if on_replica:
        monkeypatch.setenv("BRAIN_ROLE", "replica")
    calls = {name: (name, args) for name, args in CALLS.items()}
    calls["sharepoint_index refetch"] = ("sharepoint_index", {"operation": "refetch", "url": LINK})
    opened: list[str] = []
    real = sqlite3.connect

    def spy(database, *args, **kwargs):
        if database != ":memory:":
            read_only = kwargs.get("uri") and "mode=ro" in str(database)
            opened.append("read-only" if read_only else "read-write")
        return real(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    seen: dict[str, set[str]] = {}
    for label, (tool, args) in calls.items():
        opened.clear()
        if on_replica and label == "sharepoint_index refetch":
            with pytest.raises(ToolError, match="Refusing"):
                getattr(mcp_server, tool)(**args)
        else:
            getattr(mcp_server, tool)(**args)
        seen[label] = set(opened)

    assert {label for label, modes in seen.items() if "read-write" in modes} == (
        set() if on_replica else READ_WRITE
    )
    # Every call but the live mailbox reads the store, through the read-only open.
    assert {label for label, modes in seen.items() if "read-only" in modes} == set(seen) - {
        "outlook_live_search"
    }


@pytest.mark.parametrize(
    "role, stamped, reads_the_wal",
    [
        pytest.param(None, True, False, id="Mac replica: immutable, the pulled file alone"),
        pytest.param("replica", False, True, id="producer HTTP server: read-only with locks"),
        pytest.param("producer", False, True, id="single host: read-only with locks"),
    ],
)
def test_each_deployment_reads_through_its_own_open(
    store, tmp_path, monkeypatch, role, stamped, reads_the_wal
):
    """Where timers write the store, a read sees what they committed to the WAL; on a
    pulled copy only the pull replaces the file, so a stray WAL is never replayed."""
    from tests.test_store_connections import _topics, _uncheckpointed_row

    stamp = tmp_path / "db-pull.stamp"
    if stamped:
        stamp.write_text("2026-10-11T00:00:00+00:00")
    monkeypatch.setattr(config, "REPLICA_STAMP", stamp)
    if role is None:
        monkeypatch.delenv("BRAIN_ROLE")
    else:
        monkeypatch.setenv("BRAIN_ROLE", role)

    with _uncheckpointed_row(store):
        conn = mcp_server._get_conn()
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("DELETE FROM emails")
            assert ("fresh" in _topics(conn)) is reads_the_wal
        finally:
            conn.close()
