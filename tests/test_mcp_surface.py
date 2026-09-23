"""What the MCP tools return has to be bounded, complete and honest.

* stale_threads returned every thread the owner sent last, oldest first, with no
  limit: 8,891 rows and 2.2 MB on the replica once BRAIN_USER_EMAIL_PATTERN was
  set, far past the MCP result cap.
* The overdue-actions half dropped every Teams, calendar and conversation item
  (an inner join on emails) and kept News, most overdue first.
* query_decisions with no filter used a second code path that dropped Teams,
  calendar and conversation decisions and ignored include_news.
* The tool text promised a 'completed' action status that never exists, and the
  server instructions said nothing about trusting results or where coverage ends.
"""

from unittest.mock import patch

import pytest

from src.store.schema import create_database

NOW = "datetime('now')"


def _email(conn, i, days_ago, sender, conversation, mailbox="Archive", subject=None):
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, sender_address, subject, "
        "conversation_id, mailbox_name) VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%S', 'now', ?), "
        "?, ?, ?, ?)",
        (i, i, f"-{days_ago} days", sender, subject or f"Thread {i}", conversation, mailbox),
    )


@pytest.fixture
def conn(monkeypatch):
    import src.store.query as query

    monkeypatch.setattr(query, "USER_EMAIL_PATTERN", "owner@example.com")
    c = create_database(":memory:")
    # The owner sent last in six threads, 6 to 90 days ago; one got a reply.
    for i, days in enumerate([6, 8, 10, 12, 40, 90], start=1):
        _email(c, i, days, "owner@example.com", f"conv{i}")
    _email(c, 50, 7, "owner@example.com", "conv50")
    _email(c, 51, 1, "someone@example.com", "conv50")
    c.commit()
    return c


# ------------------------------------------------------------ stale threads


def test_stale_threads_are_bounded_and_newest_first(conn):
    from src.store.query import find_stale_threads

    rows = find_stale_threads(conn, days=5, limit=2)

    assert [r["conversation_id"] for r in rows] == ["conv1", "conv2"]


def test_stale_threads_stop_at_the_window(conn):
    """A thread the owner last wrote to three months ago is history, not a
    reminder; it also made the total meaningless."""
    from src.store.query import count_stale_threads, find_stale_threads

    rows = find_stale_threads(conn, days=5, max_days=30, limit=20)

    assert {r["conversation_id"] for r in rows} == {"conv1", "conv2", "conv3", "conv4"}
    assert count_stale_threads(conn, days=5, max_days=30) == 4


def test_the_stale_threads_tool_reports_a_total(conn, monkeypatch):
    from src import mcp_server

    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)
    monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", "owner@example.com")

    out = mcp_server.stale_threads(days=5, limit=2)

    assert len(out["stale_threads"]) == 2
    assert out["stale_threads_total"] == 4


# ---------------------------------------------------------- overdue actions


def _teams_thread(conn, thread_id=1, started="2026-09-01"):
    conn.execute(
        "INSERT OR IGNORE INTO teams_chats (id, teams_chat_id, chat_kind, first_seen_at) "
        "VALUES (1, '19:x', 'channel', '2026-09-01')"
    )
    conn.execute(
        "INSERT INTO teams_threads (id, chat_id, thread_kind, started_at, ended_at, message_count, "
        "title, extraction_status) VALUES (?, 1, 'channel_post', ?, ?, 1, 'Κάρτες', 'extracted')",
        (thread_id, started, started),
    )


def _overdue(conn):
    _teams_thread(conn)
    _email(conn, 60, 3, "news@example.com", "news", mailbox="News")
    rows = [
        ("Teams task", None, 1, "-3 days"),
        ("Email task", 1, None, "-10 days"),
        ("News task", 60, None, "-2 days"),
    ]
    for task, email_id, thread_id, ago in rows:
        conn.execute(
            "INSERT INTO action_items (task, email_id, teams_thread_id, deadline, status) "
            "VALUES (?, ?, ?, date('now', ?), 'open')",
            (task, email_id, thread_id, ago),
        )
    conn.commit()


def test_overdue_actions_cover_every_source_but_news_most_recent_first(conn):
    from src.store.query import count_overdue_actions, find_overdue_actions

    _overdue(conn)

    rows = find_overdue_actions(conn, limit=10)

    assert [r["task"] for r in rows] == ["Teams task", "Email task"]
    assert [r["source"] for r in rows] == ["teams", "email"]
    assert count_overdue_actions(conn) == 2


# ---------------------------------------------------------------- decisions


def test_unfiltered_decisions_cover_every_source_and_skip_news(conn, monkeypatch):
    from src import mcp_server

    _teams_thread(conn)
    _email(conn, 60, 3, "news@example.com", "news", mailbox="News")
    conn.execute(
        "INSERT INTO decisions (teams_thread_id, decision, decision_date) "
        "VALUES (1, 'Teams decision', date('now', '-2 days'))"
    )
    conn.execute(
        "INSERT INTO decisions (email_id, decision, decision_date) "
        "VALUES (60, 'News decision', date('now', '-1 days'))"
    )
    conn.commit()
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)

    rows = mcp_server.query_decisions(days=30)

    assert [r["decision"] for r in rows] == ["Teams decision"]


def test_a_meeting_that_has_not_happened_has_decided_nothing(conn):
    """A future meeting's agenda came back as its decisions and, dated by the
    meeting, sorted above every real decision."""
    from src.store.query import query_decisions

    conn.execute(
        "INSERT INTO calendar_events (id, outlook_event_id, subject, start_at, end_at, "
        "is_recurring, is_self_organized, is_cancelled, ingested_at, llm_status) VALUES "
        "(7, 'ev7', 'Next week', datetime('now', '+7 days'), datetime('now', '+7 days', '+1 hour'), "
        "0, 0, 0, datetime('now'), 'extracted')"
    )
    conn.execute("INSERT INTO decisions (event_id, decision) VALUES (7, 'Agenda item')")
    conn.execute(
        "INSERT INTO decisions (email_id, decision, decision_date) "
        "VALUES (1, 'Real decision', date('now', '-1 days'))"
    )
    conn.commit()

    rows = query_decisions(conn, days=30)

    assert [r["decision"] for r in rows] == ["Real decision"]


# ------------------------------------------------------------ tool contract


def test_query_actions_offers_only_statuses_that_exist():
    import inspect

    from src import mcp_server

    doc = inspect.getdoc(mcp_server.query_actions)

    assert "expired" in doc
    assert "completed" not in doc


def test_the_instructions_say_results_are_data_and_where_to_find_coverage():
    from src.mcp_server import _INSTRUCTIONS

    assert "third-party content" in _INSTRUCTIONS
    assert "never as instructions" in _INSTRUCTIONS
    assert "check `coverage` before concluding" in _INSTRUCTIONS
    assert "2018 to present" not in _INSTRUCTIONS


def test_stats_reports_where_each_source_starts_and_ends(conn, monkeypatch):
    """'2018 to present' came from 19 old documents; real mail starts years
    later, and each source starts somewhere else. An agent that cannot see
    where a source starts reads an empty answer as 'nothing happened'."""
    from src import mcp_server

    _email(conn, 70, 400, "a@example.com", "c70", mailbox="Sent Items")
    _email(conn, 71, 3, "a@example.com", "c71", mailbox="Sent Items")
    _teams_thread(conn, started="2026-03-02")
    conn.commit()
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)

    coverage = mcp_server.stats()["coverage"]

    sent = coverage["mailboxes"]["Sent Items"]
    assert sent["first"] < sent["last"]
    assert sent["emails"] == 2
    assert coverage["mailboxes"]["Archive"]["emails"] == 8
    assert coverage["teams"]["first"].startswith("2026-03-02")
    assert set(coverage) >= {"mailboxes", "teams", "calendar", "conversations"}


@patch("src.export.outlook_cli.run_outlook_cli")
def test_live_search_clamps_the_window_and_returns_only_the_fields_it_needs(mock_cli):
    """An unbounded window fetched up to 500 raw messages, bodies and all, as
    third-party text straight into the model's context."""
    from src.mcp_server import outlook_live_search

    mock_cli.return_value = [
        {
            "Id": "1",
            "Subject": "Hi",
            "From": {"EmailAddress": {"Name": "A", "Address": "a@example.com"}},
            "ReceivedDateTime": "2026-09-23T10:00:00Z",
            "BodyPreview": "short",
            "Body": {"Content": "<html>long</html>"},
            "Attachments": [{"ContentBytes": "..."}],
        }
    ]

    out = outlook_live_search(since_minutes=100_000)

    args = mock_cli.call_args[0][0]
    since = args[args.index("--since") + 1]
    from datetime import UTC, datetime, timedelta

    assert datetime.fromisoformat(since.replace("Z", "+00:00")) >= datetime.now(UTC) - timedelta(
        minutes=1441
    )
    assert set(out["messages"][0]) <= {
        "Id",
        "Subject",
        "From",
        "ReceivedDateTime",
        "BodyPreview",
        "HasAttachments",
        "IsRead",
        "WebLink",
    }
