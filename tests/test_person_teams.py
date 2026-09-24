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
        "(20, 2, 'channel_post', 'a20', ?, ?, 'Old post')",
        (_ago(3), _ago(2), _ago(500), _ago(500)),
    )
    rows = [
        ("m1", 1, 10, 1, _ago(3), 0),
        ("m2", 1, 10, 1, _ago(2), 0),
        ("m3", 1, 10, 1, _ago(2), 1),  # system message, not hers
        ("m4", 2, 20, 1, _ago(500), 0),  # outside the window
        ("m5", 1, 10, None, _ago(1), 0),  # someone unresolved
    ]
    conn.executemany(
        "INSERT INTO teams_messages (teams_message_id, chat_id, thread_id, sender_person_id, "
        "composed_at, is_system) VALUES (?, ?, ?, ?, ?, ?)",
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
