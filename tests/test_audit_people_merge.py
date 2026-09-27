"""Merging two people moves every link to the survivor, not only the email ones.

merge_person reassigned email_people alone, so a merged-away person's Teams
messages, meeting attendance and resolved MRI pointed at a deleted row: invisible
to person_context, and a dangling foreign key once the row was gone. Synthetic
people only.
"""

from src.store.dedup_people import merge_person
from src.store.schema import create_database


def _store():
    conn = create_database(":memory:")
    keep = conn.execute(
        "INSERT INTO people (name, email) VALUES ('Jane Roe', 'jane.roe@example.com')"
    ).lastrowid
    gone = conn.execute("INSERT INTO people (name) VALUES ('JANE ROE')").lastrowid
    chat = conn.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, first_seen_at) "
        "VALUES ('19:x', 'oneOnOne', '2026-09-01T00:00:00')"
    ).lastrowid
    conn.execute(
        "INSERT INTO teams_messages (teams_message_id, chat_id, composed_at, sender_mri, "
        "sender_person_id) VALUES ('m1', ?, '2026-09-01T10:00:00', '8:orgid:x', ?)",
        (chat, gone),
    )
    conn.execute(
        "INSERT INTO teams_mri_resolution (mri, email, person_id, status) "
        "VALUES ('8:orgid:x', 'jane.roe@example.com', ?, 'resolved')",
        (gone,),
    )
    event = conn.execute(
        "INSERT INTO calendar_events (outlook_event_id, start_at, end_at, ingested_at) "
        "VALUES ('e1', '2026-09-01T10:00:00', '2026-09-01T11:00:00', '2026-09-01')"
    ).lastrowid
    conn.execute(
        "INSERT INTO event_attendees (event_id, person_id, email, response_status) "
        "VALUES (?, ?, 'jane.roe@example.com', 'accepted')",
        (event, gone),
    )
    conn.commit()
    return conn, keep, gone


def test_teams_calendar_and_mri_links_move_to_the_survivor():
    conn, keep, gone = _store()

    merge_person(conn, keep, gone)
    conn.commit()

    assert conn.execute("SELECT sender_person_id FROM teams_messages").fetchone()[0] == keep
    assert conn.execute("SELECT person_id FROM teams_mri_resolution").fetchone()[0] == keep
    assert conn.execute("SELECT person_id FROM event_attendees").fetchone()[0] == keep
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 1


def test_a_merge_leaves_no_dangling_reference():
    conn, keep, gone = _store()
    conn.execute("PRAGMA foreign_keys = ON")

    merge_person(conn, keep, gone)
    conn.commit()

    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
