"""An empty answer says why, and what the store holds of the sources it searched.

In six weeks no empty result carried a `reason`, and agents read empty as "does not
exist": 87 of the 105 calls after an empty result moved on to another topic. Now
every empty list a tool returns carries `reason` and `coverage`, the first and last
date held and the number of items of each source the call searched, read as stats
reads them, so "nothing found" reads as "nothing in X between A and B".
"""

import pytest

from src import config, mcp_coverage, mcp_server
from src.store import query
from src.store.schema import create_database, get_connection
from tests import seeded_store

MAIL, ATTACHMENTS, TEAMS, WHATSAPP, CALENDAR, CONVERSATIONS = (
    "mailboxes",
    "attachments",
    "teams",
    "whatsapp",
    "calendar",
    "conversations",
)
EXTRACTED_FROM = {MAIL, TEAMS, WHATSAPP, CALENDAR, CONVERSATIONS}


@pytest.fixture
def store(tmp_path, monkeypatch):
    mcp_coverage.clear()
    path = seeded_store.build(tmp_path / "brain.db")
    seeded_store.serve(path, monkeypatch)
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", "")
    monkeypatch.setattr(query, "USER_EMAIL_PATTERN", "")
    return path


@pytest.mark.parametrize(
    "call, key, sources",
    [
        (lambda: mcp_server.search_emails("zzyzx"), "result", {MAIL}),
        (lambda: mcp_server.query_emails(keyword="zzyzx"), "result", {MAIL}),
        (lambda: mcp_server.query_decisions(topic="zzyzx"), "result", EXTRACTED_FROM),
        (lambda: mcp_server.query_actions(owner="Nobody"), "result", EXTRACTED_FROM),
        (lambda: mcp_server.search_attachments("zzyzx"), "result", {ATTACHMENTS}),
        (lambda: mcp_server.attachment_image_search("zzyzx"), "results", {MAIL}),
        (lambda: mcp_server.query_calendar_events(keyword="zzyzx"), "events", {CALENDAR}),
        (lambda: mcp_server.search_conversations("zzyzx"), "result", {CONVERSATIONS}),
        (lambda: mcp_server.search_teams("zzyzx"), "results", {TEAMS}),
        (lambda: mcp_server.search_whatsapp("zzyzx"), "results", {WHATSAPP}),
        (lambda: mcp_server.teams_chat_summary(chat_id=1, days=1), "threads", {TEAMS}),
        (
            lambda: mcp_server.sql_query(sql="SELECT id FROM emails WHERE id < 0"),
            "rows",
            {MAIL},
        ),
    ],
)
def test_an_empty_list_carries_a_reason_and_the_coverage_searched(store, call, key, sources):
    out = call()

    assert out[key] == []
    assert out["reason"]
    assert set(out["coverage"]) == sources
    for source, span in out["coverage"].items():
        # Mail is held per mailbox, as stats gives it.
        for one in span.values() if source == MAIL else [span]:
            assert {"first", "last"} <= set(one)


@pytest.mark.parametrize(
    "call, sources",
    [
        (lambda: mcp_server.recall("zzyzx"), set(mcp_coverage.EVERY_SOURCE)),
        (lambda: mcp_server.recall("zzyzx", kinds=["teams", "calendar"]), {TEAMS, CALENDAR}),
        (lambda: mcp_server.person_context("Nobody"), {MAIL, TEAMS, WHATSAPP, CALENDAR}),
        (lambda: mcp_server.topic_context("zzyzx"), {MAIL}),
        (lambda: mcp_server.sender_brief("nobody@example.com"), {MAIL}),
        (lambda: mcp_server.meeting_prep("nobody@example.com"), {MAIL, CALENDAR}),
    ],
)
def test_a_dossier_or_recall_that_found_nothing_says_where_it_looked(store, call, sources):
    out = call()

    assert out["reason"]
    assert set(out["coverage"]) == sources


def test_the_spans_are_the_ones_stats_reports(store):
    held = mcp_server.stats()["coverage"]

    out = mcp_server.recall("zzyzx")

    for source in (MAIL, TEAMS, WHATSAPP, CONVERSATIONS, ATTACHMENTS):
        assert out["coverage"][source] == held[source], source
    assert out["coverage"][CALENDAR]["first"] == held[CALENDAR]["first"]
    # The month by month detail stays in stats.
    assert "per_month" not in out["coverage"][CALENDAR]


def test_stats_gives_the_attachments_span_too(store):
    span = mcp_server.stats()["coverage"][ATTACHMENTS]

    assert span["items"] == 1
    assert span["first"] == span["last"]


def test_a_result_with_rows_carries_no_coverage(store):
    out = mcp_server.search_emails("okapi")

    assert out["result"]
    assert "coverage" not in out
    assert "reason" not in out


def test_the_spans_are_read_once_and_again_when_the_store_changes(store, monkeypatch):
    reads = []
    real = query.get_coverage
    monkeypatch.setattr(query, "get_coverage", lambda conn: reads.append(1) or real(conn))

    mcp_server.search_emails("zzyzx")
    mcp_server.search_teams("zzyzx")
    assert len(reads) == 1

    conn = get_connection(str(store))
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, mailbox_name, "
        "source_class) VALUES (99, 99, '2030-01-01T00:00:00Z', 'later', 'Inbox', 'mail')"
    )
    conn.commit()
    conn.close()

    out = mcp_server.search_emails("zzyzx")
    assert len(reads) == 2
    assert out["coverage"][MAIL]["Inbox"]["last"] == "2030-01-01T00:00:00Z"


def test_nothing_overdue_and_no_stale_thread_says_why(tmp_path, monkeypatch):
    mcp_coverage.clear()
    path = tmp_path / "empty.db"
    create_database(str(path)).close()
    seeded_store.serve(path, monkeypatch)

    out = mcp_server.stale_threads()

    assert out["stale_threads"] == out["overdue_actions"] == []
    assert out["reason"]
    assert set(out["coverage"]) == EXTRACTED_FROM


def test_an_empty_live_mailbox_says_which_window_it_read(store):
    out = mcp_server.outlook_live_search(since_minutes=30)

    assert out["messages"] == []
    assert out["reason"]
    window = out["coverage"]["live_mailbox"]
    assert window["folder"] == "Inbox"
    assert window["first"] < window["last"]


def test_no_link_in_a_state_says_so(store):
    out = mcp_server.sharepoint_index(operation="list_unfetched")

    assert out["links"] == []
    assert out["reason"]
    assert "sharepoint_links" in out["coverage"]
