"""A sender's address is saved on the namesake it is linked to, never on a stranger.

When the address missed, find_or_create_person linked the sender to the first
person with the same name and never saved the address on it, even when that
person held a different address. People named in people_roles are loaded before
the sender, usually without an address, so about 195 active senders stayed
unknown by address to sender_brief and person_context (db-integrity-1). All names
and addresses below are synthetic.
"""

from src.bridge import sender_brief
from src.store.loader import load_single_email
from src.store.normalizer import find_or_create_person
from src.store.schema import create_database


def _email(conn, pid):
    return conn.execute("SELECT email FROM people WHERE id = ?", (pid,)).fetchone()[0]


class TestFindOrCreatePersonAddress:
    def test_a_namesake_without_an_address_gets_it(self):
        conn = create_database(":memory:")
        pid = find_or_create_person(conn, "Jane Roe")

        assert find_or_create_person(conn, "Jane Roe", "Jane.Roe@example.com") == pid
        assert _email(conn, pid) == "jane.roe@example.com"

    def test_a_namesake_with_another_address_is_someone_else(self):
        conn = create_database(":memory:")
        other = find_or_create_person(conn, "Jane Roe", "jane.roe@example.org")

        pid = find_or_create_person(conn, "Jane Roe", "jane.roe@example.com")

        assert pid != other
        assert _email(conn, other) == "jane.roe@example.org"
        assert _email(conn, pid) == "jane.roe@example.com"

    def test_the_namesake_without_an_address_wins_over_one_with_another(self):
        conn = create_database(":memory:")
        find_or_create_person(conn, "Jane Roe", "jane.roe@example.org")
        bare = conn.execute("INSERT INTO people (name) VALUES ('Jane Roe')").lastrowid

        assert find_or_create_person(conn, "Jane Roe", "jane.roe@example.com") == bare
        assert _email(conn, bare) == "jane.roe@example.com"


def _extraction(people_roles):
    return {
        "summary": "s",
        "sentiment": "informational",
        "urgency": "low",
        "language": "english",
        "topics": [],
        "decisions": [],
        "action_items": [],
        "people_roles": people_roles,
        "key_facts": [],
    }


def _meta(message_id, sender):
    return {
        "message_id": message_id,
        "date_received": "2026-09-01T10:00:00",
        "sender": sender,
        "subject": f"s{message_id}",
        "content": "c",
        "mailbox": "Inbox",
        "to": [{"name": "Owner", "address": "owner@example.com"}],
        "cc": [],
    }


def test_a_sender_first_named_by_people_roles_is_found_by_address():
    conn = create_database(":memory:")
    # Named in an email that gives no address for them: created without one.
    load_single_email(
        conn,
        _meta(1, {"name": "Owner", "address": "owner@example.com"}),
        _extraction({"Jane Roe": "mentioned"}),
    )
    conn.commit()
    # Then the sender of the next, whose header carries the address.
    load_single_email(
        conn,
        _meta(2, {"name": "Jane Roe", "address": "jane.roe@example.com"}),
        _extraction({}),
    )
    conn.commit()

    row = conn.execute(
        "SELECT id FROM people WHERE LOWER(email) = 'jane.roe@example.com'"
    ).fetchone()
    assert row is not None
    assert conn.execute("SELECT COUNT(*) FROM people WHERE name = 'Jane Roe'").fetchone()[0] == 1
    brief = sender_brief(conn, "jane.roe@example.com", days=100000)
    assert brief["known"] is True
    assert brief["email_count"] == 2
