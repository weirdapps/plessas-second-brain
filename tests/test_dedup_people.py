"""Tests for phase 7 of people dedup — transliterated / reversed name variants."""

from src.store.dedup_people import phase7_merge_transliterated_variants
from src.store.schema import create_database


def _add_person(conn, name, email=None):
    cur = conn.execute("INSERT INTO people (name, email) VALUES (?, ?)", (name, email))
    return cur.lastrowid


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]


class TestPhase7Transliteration:
    def test_merges_order_reversed_name(self):
        conn = create_database(":memory:")
        _add_person(conn, "Παπαδόπουλος Νίκος")  # emailless
        _add_person(conn, "Νίκος Παπαδόπουλος", "n.papadopoulos@example.com")
        conn.commit()

        assert phase7_merge_transliterated_variants(conn) == 1
        conn.commit()
        assert _count(conn) == 1
        # Survivor keeps the email-bearing record's address.
        assert (
            conn.execute("SELECT email FROM people").fetchone()[0] == "n.papadopoulos@example.com"
        )

    def test_merges_cross_script_variant(self):
        conn = create_database(":memory:")
        _add_person(conn, "Νίκος Παπαδόπουλος")  # emailless Greek
        _add_person(conn, "Nikos Papadopoulos", "dp@example.com")  # Latin with email
        conn.commit()

        assert phase7_merge_transliterated_variants(conn) == 1
        conn.commit()
        assert _count(conn) == 1

    def test_does_not_merge_different_emails(self):
        # Same name, two DIFFERENT emails = two real people. Must NOT merge.
        conn = create_database(":memory:")
        _add_person(conn, "Maria Papadopoulou", "a@example.com")
        _add_person(conn, "Papadopoulou Maria", "b@example.com")
        conn.commit()

        assert phase7_merge_transliterated_variants(conn) == 0
        assert _count(conn) == 2

    def test_skips_single_token_names(self):
        conn = create_database(":memory:")
        _add_person(conn, "Νίκος")
        _add_person(conn, "Nikos")
        conn.commit()

        assert phase7_merge_transliterated_variants(conn) == 0
        assert _count(conn) == 2

    def test_reassigns_email_people_references(self):
        conn = create_database(":memory:")
        _add_person(conn, "Νίκος Παπαδόπουλος", "dp@example.com")  # will survive
        remove = _add_person(conn, "Παπαδόπουλος Νίκος")  # emailless, will be removed
        conn.execute(
            "INSERT INTO emails (message_id, date_received, summary) VALUES (1, '2026-01-01', 's')"
        )
        eid = conn.execute("SELECT id FROM emails").fetchone()[0]
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, 'sender')",
            (eid, remove),
        )
        conn.commit()

        phase7_merge_transliterated_variants(conn)
        conn.commit()

        survivor = conn.execute("SELECT id FROM people").fetchone()[0]
        who = conn.execute(
            "SELECT person_id FROM email_people WHERE email_id = ?", (eid,)
        ).fetchone()[0]
        assert who == survivor  # reference followed the merge
