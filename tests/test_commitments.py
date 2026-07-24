"""Tests for commitments: schema migration, loader storage, recall surfacing."""

from src.store.loader import load_single_email
from src.store.recall import recall
from src.store.schema import create_database, get_schema_version, migrate_add_commitments


class TestCommitmentsSchema:
    def test_create_database_has_commitments_table_at_v14(self):
        conn = create_database(":memory:")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "commitments" in tables
        assert get_schema_version(conn) == 14
        conn.close()

    def test_migration_is_idempotent(self):
        conn = create_database(":memory:")
        migrate_add_commitments(conn)  # IF NOT EXISTS — must not raise
        conn.close()


class TestCommitmentsLoader:
    def _meta(self):
        return {
            "message_id": 1,
            "date_received": "2026-01-01T00:00:00",
            "sender": {"name": "X", "address": "x@example.com"},
            "subject": "s",
            "content": "c",
            "mailbox": "INBOX",
            "to": [],
            "cc": [],
        }

    def test_loader_stores_dict_and_string_commitments(self):
        conn = create_database(":memory:")
        extraction = {
            "summary": "s",
            "sentiment": "informational",
            "urgency": "low",
            "language": "english",
            "topics": [],
            "decisions": [],
            "action_items": [],
            "commitments": [
                {"commitment": "deliver the Q3 report", "by": "Maria", "to": "Papadopoulos"},
                "send the deck",  # simple string form
                {"commitment": ""},  # empty -> skipped
            ],
            "people_roles": {},
            "key_facts": [],
        }
        load_single_email(conn, self._meta(), extraction)
        rows = conn.execute(
            "SELECT commitment, by_person, to_person FROM commitments ORDER BY id"
        ).fetchall()
        assert len(rows) == 2  # empty one skipped
        assert tuple(rows[0]) == ("deliver the Q3 report", "Maria", "Papadopoulos")
        assert rows[1][0] == "send the deck"
        conn.close()

    def test_list_valued_by_and_to_are_joined(self):
        conn = create_database(":memory:")
        extraction = {
            "summary": "s",
            "sentiment": "informational",
            "urgency": "low",
            "language": "english",
            "topics": [],
            "decisions": [],
            "action_items": [],
            "commitments": [{"commitment": "ship it", "by": ["A", "B"], "to": ["C"]}],
            "people_roles": {},
            "key_facts": [],
        }
        load_single_email(conn, self._meta(), extraction)
        row = conn.execute("SELECT by_person, to_person FROM commitments").fetchone()
        assert row[0] == "A, B"
        assert row[1] == "C"
        conn.close()


class TestCommitmentsRecall:
    def test_recall_surfaces_commitments(self):
        conn = create_database(":memory:")
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, summary) VALUES (1, 1, '2026-01-01', 's')"
        )
        conn.execute(
            "INSERT INTO commitments (email_id, commitment, by_person, to_person) "
            "VALUES (1, 'deliver unicornz9 report by Friday', 'Maria', 'Papadopoulos')"
        )
        conn.commit()
        res = recall(conn, "unicornz9")
        assert "commitments" in res
        assert any("unicornz9" in c["commitment"] for c in res["commitments"])
        conn.close()
