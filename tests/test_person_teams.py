"""person_context shows what someone wrote in Teams, not only in mail.

31,204 Teams messages are in the store, 83% linked to a person, and the dossier
tools read none of them: someone who works with the owner mostly in Teams came
back as barely known.
"""

from datetime import datetime, timedelta

from src.store.context import get_person_context
from src.store.schema import create_database


def _ago(days: float) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _store():
    conn = create_database(":memory:")
    conn.execute(
        "INSERT INTO people (id, name, email) VALUES (1, 'Anna Example', 'anna@example.com')"
    )
    conn.execute(
        "INSERT INTO teams_chats (id, teams_chat_id, chat_kind, topic, team_name, first_seen_at) "
        "VALUES (1, 'c1', 'group', 'Cards squad', NULL, '2026-01-01'), "
        "(2, 'c2', 'channel', 'General', 'Digital', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO teams_threads (id, chat_id, thread_kind, anchor_message_id, started_at, "
        "ended_at, title) VALUES "
        "(10, 1, 'chat_session', 'a10', ?, ?, 'Launch plan'), "
        "(20, 2, 'channel_post', 'a20', ?, ?, 'Old post'), "
        "(30, 1, 'chat_session', 'a30', ?, ?, 'System only'), "
        "(40, 1, 'chat_session', 'a40', ?, ?, 'A call')",
        (_ago(3), _ago(2), _ago(500), _ago(500), _ago(1), _ago(1), _ago(1), _ago(1)),
    )
    rows = [
        ("m1", 1, 10, 1, _ago(3), 0, None),
        ("m2", 1, 10, 1, _ago(2), 0, None),
        ("m3", 1, 10, 1, _ago(2), 1, None),  # system message, not hers
        ("m4", 2, 20, 1, _ago(500), 0, None),  # outside the window
        ("m5", 1, 10, None, _ago(1), 0, None),  # someone unresolved
        ("m6", 1, 30, 1, _ago(1), 1, None),  # a thread with only a system message
        ("m7", 1, 10, 1, _ago(1), 0, "Event/Call"),  # a call event, not something she wrote
        ("m8", 1, 40, 1, _ago(1), 0, "Event/Call"),  # a thread with only a call event
    ]
    conn.executemany(
        "INSERT INTO teams_messages (teams_message_id, chat_id, thread_id, sender_person_id, "
        "composed_at, is_system, message_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


def test_person_context_carries_their_teams_activity_in_the_window():
    teams = get_person_context(_store(), "anna@example.com", days=365)["teams"]

    assert teams["message_count"] == 2
    assert teams["last_message_at"][:10] == _ago(2)[:10]
    assert [t["title"] for t in teams["recent_threads"]] == ["Launch plan"]
    assert teams["recent_threads"][0]["chat"] == "Cards squad"
    assert teams["recent_threads"][0]["messages"] == 2
    assert teams["recent_threads_total"] == 1


def test_a_longer_window_reaches_older_threads():
    teams = get_person_context(_store(), "anna@example.com", days=900)["teams"]

    assert teams["message_count"] == 3
    assert [t["title"] for t in teams["recent_threads"]] == ["Launch plan", "Old post"]
    assert teams["recent_threads"][1]["chat"] == "Digital / General"


def test_the_thread_list_is_capped_by_limit_and_says_how_many_there_were():
    teams = get_person_context(_store(), "anna@example.com", days=900, limit=1)["teams"]

    assert len(teams["recent_threads"]) == 1
    assert teams["recent_threads_total"] == 2


def test_a_chat_with_no_topic_is_named_by_its_kind():
    """1:1 chats and most group chats have neither a topic nor a team name."""
    conn = _store()
    conn.execute(
        "INSERT INTO teams_chats (id, teams_chat_id, chat_kind, topic, team_name, first_seen_at) "
        "VALUES (3, 'c3', 'oneOnOne', NULL, NULL, '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO teams_threads (id, chat_id, thread_kind, anchor_message_id, started_at, "
        "ended_at, title) VALUES (50, 3, 'chat_session', 'a50', ?, ?, 'Direct')",
        (_ago(0.5), _ago(0.5)),
    )
    conn.execute(
        "INSERT INTO teams_messages (teams_message_id, chat_id, thread_id, sender_person_id, "
        "composed_at, is_system) VALUES ('m9', 3, 50, 1, ?, 0)",
        (_ago(0.5),),
    )
    conn.commit()

    teams = get_person_context(conn, "anna@example.com", days=365)["teams"]

    assert teams["recent_threads"][0]["title"] == "Direct"
    assert teams["recent_threads"][0]["chat"] == "oneOnOne"


def test_someone_unknown_has_an_empty_teams_section():
    ctx = get_person_context(_store(), "nobody@example.com")

    assert ctx["person"] is None
    assert ctx["teams"] == {
        "message_count": 0,
        "last_message_at": None,
        "recent_threads": [],
        "recent_threads_total": 0,
    }
