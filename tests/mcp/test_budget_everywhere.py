"""Every tool that can return a list holds its answer to the response budget.

Only ten tools did. stale_threads once returned 5,366,123 characters and
query_calendar_events 272,098; person_context, meeting_prep and outlook_live_search
(up to 500 messages) had no character bound at all. Each is read here against a store
heavy enough to cross 40,000 characters, and must come back under it, saying in
`truncated` which lists lost rows, as {"kept": n, "total": m}.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src import config, mcp_coverage, mcp_server
from src.mcp_budget import RESPONSE_BUDGET_CHARS, json_chars
from src.store import query
from src.store.schema import create_database
from tests import seeded_store

OWNER = "owner@example.com"
LONG = "okapi " + "word " * 400  # about 2,000 characters


def _at(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(scope="module")
def heavy(tmp_path_factory):
    """A store where every list a tool returns runs far past the budget."""
    path = tmp_path_factory.mktemp("heavy") / "brain.db"
    conn = create_database(str(path))
    conn.execute(
        "INSERT INTO people (id, name, email) VALUES (1, 'Alice Smith', 'alice@example.com')"
    )
    conn.execute(
        "INSERT INTO people (id, name, email) VALUES (2, 'Someone', 'someone@example.com')"
    )
    conn.execute("INSERT INTO topics (id, name, display_name) VALUES (1, 'okapi', 'Okapi')")
    emails = []
    for i in range(1, 251):
        # 1-200: one long thread with Alice; 201-250: threads the owner sent last.
        mine = i > 200
        emails.append(
            (
                i,
                i,
                _at(10 if mine else i / 10),
                f"Okapi {i} " + "subject " * 20,
                LONG,
                "body",
                "Owner" if mine else "Alice Smith",
                OWNER if mine else "alice@example.com",
                f"own{i}" if mine else "c1",
                "Sent Items" if mine else "Inbox",
                "mail",
            )
        )
    conn.executemany(
        "INSERT INTO emails (id, message_id, date_received, subject, summary, content, "
        "sender_name, sender_address, conversation_id, mailbox_name, source_class) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        emails,
    )
    conn.executemany(
        "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, ?)",
        [(i, 1 if i <= 200 else 2, "sender" if i <= 200 else "recipient") for i in range(1, 251)],
    )
    conn.executemany(
        "INSERT INTO email_topics (email_id, topic_id) VALUES (?, 1)", [(i,) for i in range(1, 251)]
    )
    conn.executemany(
        "INSERT INTO decisions (email_id, decision, decided_by, decision_date) "
        "VALUES (?, ?, 'Alice Smith', date('now'))",
        [(i, LONG) for i in range(1, 201)],
    )
    conn.executemany(
        "INSERT INTO action_items (email_id, task, owner, deadline, status) "
        "VALUES (?, ?, 'Alice Smith', date('now', '-3 days'), 'open')",
        [(i, LONG) for i in range(1, 201)],
    )
    conn.executemany(
        "INSERT INTO key_facts (email_id, fact) VALUES (?, ?)", [(i, LONG) for i in range(1, 201)]
    )
    conn.execute(
        "INSERT INTO teams_chats (id, teams_chat_id, chat_kind, topic, first_seen_at) "
        "VALUES (1, '19:okapi', 'group', 'Okapi team', ?)",
        (_at(2),),
    )
    conn.executemany(
        "INSERT INTO teams_threads (id, chat_id, thread_kind, started_at, ended_at, "
        "message_count, title, summary, extraction_status) "
        "VALUES (?, 1, 'chat_session', ?, ?, 25, 'Okapi thread', ?, 'extracted')",
        [(t, _at(1), _at(1), LONG) for t in range(1, 21)],
    )
    conn.executemany(
        "INSERT INTO teams_messages (teams_message_id, chat_id, thread_id, "
        "sender_display_name, sender_person_id, composed_at, content_text, is_system) "
        "VALUES (?, 1, 1, 'Alice Smith', 1, ?, ?, 0)",
        [(f"t{m}", _at(1 - m / 10_000), LONG) for m in range(400)],
    )
    conn.execute(
        "INSERT INTO conversations (id, session_id, started_at, ended_at, project_name, "
        "turn_count, summary, created_at) VALUES (1, 's1', ?, ?, 'okapi', 300, 'okapi', ?)",
        (_at(3), _at(3), _at(3)),
    )
    conn.executemany(
        "INSERT INTO conversation_turns (conversation_id, turn_index, timestamp, speaker, "
        "content, summary) VALUES (1, ?, ?, 'user', 'the okapi question', ?)",
        [(t, _at(3), LONG) for t in range(300)],
    )
    conn.executemany(
        "INSERT INTO inline_images (sha256, width, height, bytes, classification, "
        "classification_method, classified_at, vision_description) "
        "VALUES (?, 800, 600, 1000, 'content', 'vision', ?, ?)",
        [(f"{n:064x}", _at(1), LONG) for n in range(200)],
    )
    conn.executemany(
        "INSERT INTO inline_image_occurrences (sha256, message_id, sender_email, "
        "position_in_body) VALUES (?, ?, 'alice@example.com', 0.5)",
        [(f"{n:064x}", str(n)) for n in range(200)],
    )
    conn.executemany(
        "INSERT INTO sharepoint_links (url, message_id, last_status, last_attempt_at) "
        "VALUES (?, '1', 'http-error', ?)",
        [(f"https://tenant.example.com/sites/Team/{'x' * 300}/{n}", _at(1)) for n in range(400)],
    )
    conn.executemany(
        "INSERT INTO calendar_events (id, outlook_event_id, subject, body_summary, "
        "organizer_name, organizer_email, start_at, end_at, is_recurring, is_self_organized, "
        "is_cancelled, ingested_at, llm_status, response_status) "
        "VALUES (?, ?, 'Okapi review', ?, 'Alice Smith', 'alice@example.com', ?, ?, 0, 0, 0, "
        "?, 'extracted', 'Accepted')",
        [(e, f"ev{e}", LONG, _at(e), _at(e), _at(1)) for e in range(1, 6)],
    )
    conn.executemany(
        "INSERT INTO event_attendees (event_id, email, name, response_status, is_organizer, "
        "is_self) VALUES (?, 'alice@example.com', 'Alice Smith', 'Organizer', 1, 0)",
        [(e,) for e in range(1, 6)],
    )
    conn.execute(
        "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES ('last_sync_date', ?)",
        (_at(0),),
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def store(heavy, monkeypatch):
    mcp_coverage.clear()
    seeded_store.serve(heavy, monkeypatch)
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", OWNER)
    monkeypatch.setattr(query, "USER_EMAIL_PATTERN", OWNER)
    return heavy


def _live(args):
    return [
        {
            "Id": f"m{n}",
            "Subject": f"Okapi {n}",
            "From": "alice@example.com",
            "ReceivedDateTime": _at(0),
            "BodyPreview": LONG[:255],
            "HasAttachments": False,
            "IsRead": False,
            "WebLink": f"https://outlook.example.com/{'x' * 200}/{n}",
        }
        for n in range(500)
    ]


CALLS = {
    "stale_threads": lambda: mcp_server.stale_threads(limit=200, max_days=60),
    "person_context": lambda: mcp_server.person_context("alice@example.com", limit=200),
    "topic_context": lambda: mcp_server.topic_context("okapi", limit=200),
    "email_thread": lambda: mcp_server.email_thread(email_id=100, limit=200),
    "teams_thread_context": lambda: mcp_server.teams_thread_context(thread_id=1),
    "conversation_context": lambda: mcp_server.conversation_context(session_id="s1"),
    "teams_chat_summary": lambda: mcp_server.teams_chat_summary(chat_id=1),
    "attachment_image_search": lambda: mcp_server.attachment_image_search("okapi", limit=200),
    "sharepoint_index": lambda: mcp_server.sharepoint_index(operation="list_stale"),
    "query_actions": lambda: mcp_server.query_actions(limit=200),
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_the_answer_fits_and_says_what_it_cut(store, name):
    out = CALLS[name]()

    assert json_chars(out) <= RESPONSE_BUDGET_CHARS, name
    assert out["truncated"], name
    for cut in out["truncated"].values():
        assert cut["kept"] < cut["total"]


def test_live_mail_fits_and_says_what_it_cut(store, monkeypatch):
    monkeypatch.setattr("src.export.outlook_cli.run_outlook_cli", _live)

    out = mcp_server.outlook_live_search(since_minutes=1440)

    assert json_chars(out) <= RESPONSE_BUDGET_CHARS
    assert out["truncated"]["messages"]["total"] == 500
    assert out["count"] == len(out["messages"])


def test_meeting_prep_cuts_inside_each_dossier_not_whole_attendees(store):
    out = mcp_server.meeting_prep("alice@example.com, alice@example.com, alice@example.com")

    assert json_chars(out) <= RESPONSE_BUDGET_CHARS
    assert len(out["attendees"]) == 3
    for attendee in out["attendees"]:
        assert attendee["resolved_email"] == "alice@example.com"
        assert attendee["truncated"]


def test_the_image_search_count_follows_the_rows_kept(store):
    out = mcp_server.attachment_image_search("okapi", limit=200)

    assert out["count"] == len(out["results"])


def test_a_dossier_keeps_its_totals_when_rows_are_cut(store):
    out = mcp_server.person_context("alice@example.com", limit=200)

    assert out["decisions_total"] == 200
    assert len(out["decisions"]) < 200
