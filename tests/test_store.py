"""Tests for the SQLite knowledge store (schema, loader, normalizer).

Uses in-memory SQLite databases for fast, isolated testing.
"""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.store.loader import load_extractions, load_single_email
from src.store.normalizer import (
    find_or_create_person,
    find_or_create_topic,
    merge_people,
    normalize_topic,
)
from src.store.schema import create_database, get_connection


class TestSchema:
    """Test database schema creation and structure."""

    def test_create_database_in_memory(self):
        """Test creating database in memory."""
        conn = create_database(":memory:")
        assert conn is not None
        assert isinstance(conn, sqlite3.Connection)
        conn.close()

    def test_create_database_file(self):
        """Test creating database as file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            conn = create_database(db_path)
            assert conn is not None
            assert Path(db_path).exists()
            conn.close()

    def test_all_tables_created(self):
        """Test that all required tables are created."""
        conn = create_database(":memory:")

        # Check all main tables exist
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]

        # Core tables that must exist
        expected_core_tables = [
            "action_items",
            "decisions",
            "email_people",
            "email_topics",
            "emails",
            "key_facts",
            "people",
            "topics",
        ]

        # FTS5 virtual tables (internal table names vary by SQLite version)
        expected_fts_tables = ["emails_fts", "key_facts_fts"]

        for table in expected_core_tables:
            assert table in tables, f"Missing core table: {table}"

        for table in expected_fts_tables:
            assert table in tables, f"Missing FTS5 table: {table}"

        conn.close()

    def test_indexes_created(self):
        """Test that all indexes are created."""
        conn = create_database(":memory:")

        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")
        indexes = [row[0] for row in cursor.fetchall()]

        expected_indexes = [
            "idx_emails_date_received",
            "idx_emails_sender_address",
            "idx_email_topics_topic_id",
            "idx_email_people_person_id",
            "idx_decisions_email_id",
            "idx_action_items_email_id",
            "idx_action_items_owner",
            "idx_key_facts_email_id",
        ]

        for index in expected_indexes:
            assert index in indexes, f"Missing index: {index}"

        conn.close()

    def test_foreign_keys_enabled(self):
        """Test that foreign keys are enabled."""
        conn = create_database(":memory:")
        cursor = conn.execute("PRAGMA foreign_keys")
        assert cursor.fetchone()[0] == 1
        conn.close()

    def test_get_connection_nonexistent_file(self):
        """Test that get_connection raises error for nonexistent database."""
        with pytest.raises(FileNotFoundError):
            get_connection("/nonexistent/path/to/db.sqlite")

    def test_get_connection_existing_file(self):
        """Test that get_connection opens existing database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            create_database(db_path).close()

            conn = get_connection(db_path)
            assert conn is not None
            conn.close()

    def test_get_connection_sets_wal_journal_mode(self):
        """get_connection must return a WAL-mode connection so readers never block
        the writer. WAL is ignored on :memory:, so this requires a real file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            create_database(db_path).close()
            conn = get_connection(db_path)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            conn.close()
            assert mode == "wal"

    def test_create_database_sets_wal_journal_mode(self):
        """A freshly-created file DB must be born in WAL mode, so a rebuilt DB
        (VPS reinstall, fresh clone) doesn't silently fall back to DELETE mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "fresh.db")
            conn = create_database(db_path)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            conn.close()
            assert mode == "wal"

    def test_fts5_triggers_work(self):
        """Test that FTS5 triggers populate search tables on insert."""
        conn = create_database(":memory:")

        # Insert an email
        conn.execute(
            """
            INSERT INTO emails (message_id, date_received, summary)
            VALUES (1, '2026-03-17T10:00:00', 'Test email summary')
            """
        )
        conn.commit()

        # Check FTS5 table was populated
        cursor = conn.execute("SELECT summary FROM emails_fts WHERE summary MATCH 'test'")
        result = cursor.fetchone()
        assert result is not None
        assert "Test email summary" in result[0]

        # Insert a key fact
        email_id = conn.execute("SELECT id FROM emails WHERE message_id = 1").fetchone()[0]
        conn.execute(
            "INSERT INTO key_facts (email_id, fact) VALUES (?, 'Important decision made')",
            (email_id,),
        )
        conn.commit()

        # Check FTS5 table was populated
        cursor = conn.execute("SELECT fact FROM key_facts_fts WHERE fact MATCH 'decision'")
        result = cursor.fetchone()
        assert result is not None
        assert "decision" in result[0].lower()

        conn.close()


class TestNormalizer:
    """Test topic and person normalization functions."""

    def test_normalize_topic_lowercase(self):
        """Test topic name is lowercased."""
        assert normalize_topic("Cards Migration") == "cards migration"
        assert normalize_topic("CARDS MIGRATION") == "cards migration"

    def test_normalize_topic_whitespace(self):
        """Test whitespace handling."""
        assert normalize_topic("  Cards   migration  ") == "cards migration"
        assert normalize_topic("Cards\t\tmigration") == "cards migration"

    def test_normalize_topic_special_chars(self):
        """Separators (hyphen/underscore/slash/dot) unify to spaces so variants merge."""
        assert normalize_topic("Cards-Migration") == "cards migration"
        assert normalize_topic("Cards_Migration") == "cards migration"
        assert normalize_topic("cards/migration") == "cards migration"

    def test_normalize_topic_strips_accents(self):
        """Accent variants of the same topic collapse together."""
        assert normalize_topic("Στρατηγική") == normalize_topic("στρατηγικη")

    def test_find_or_create_topic_new(self):
        """Test creating a new topic."""
        conn = create_database(":memory:")
        topic_id = find_or_create_topic(conn, "Digital Banking")
        assert topic_id is not None
        assert topic_id > 0

        # Verify it was inserted correctly
        cursor = conn.execute("SELECT name, display_name FROM topics WHERE id = ?", (topic_id,))
        row = cursor.fetchone()
        assert row[0] == "digital banking"  # normalized
        assert row[1] == "Digital Banking"  # original

        conn.close()

    def test_find_or_create_topic_existing(self):
        """Test finding an existing topic by normalized name."""
        conn = create_database(":memory:")

        # Create first time
        topic_id1 = find_or_create_topic(conn, "Cards Migration")

        # Create second time with different casing
        topic_id2 = find_or_create_topic(conn, "CARDS MIGRATION")

        # Should return same ID
        assert topic_id1 == topic_id2

        # Should only have one record
        cursor = conn.execute("SELECT COUNT(*) FROM topics WHERE name = 'cards migration'")
        assert cursor.fetchone()[0] == 1

        conn.close()

    def test_find_or_create_topic_with_parent(self):
        """Test creating topic with parent hierarchy."""
        conn = create_database(":memory:")

        parent_id = find_or_create_topic(conn, "Banking")
        child_id = find_or_create_topic(conn, "Digital Banking", parent_id=parent_id)

        cursor = conn.execute("SELECT parent_id FROM topics WHERE id = ?", (child_id,))
        assert cursor.fetchone()[0] == parent_id

        conn.close()

    def test_find_or_create_person_new(self):
        """Test creating a new person."""
        conn = create_database(":memory:")

        person_id = find_or_create_person(conn, "John Doe", "john.doe@example.com")
        assert person_id is not None
        assert person_id > 0

        cursor = conn.execute("SELECT name, email FROM people WHERE id = ?", (person_id,))
        row = cursor.fetchone()
        assert row[0] == "John Doe"
        assert row[1] == "john.doe@example.com"

        conn.close()

    def test_find_or_create_person_by_email(self):
        """Test finding person by email (primary dedup key)."""
        conn = create_database(":memory:")

        # Create first time
        person_id1 = find_or_create_person(conn, "John Doe", "john.doe@example.com")

        # Create second time with same email but different name
        person_id2 = find_or_create_person(conn, "J. Doe", "john.doe@example.com")

        # Should return same ID
        assert person_id1 == person_id2

        # Should only have one record
        cursor = conn.execute("SELECT COUNT(*) FROM people")
        assert cursor.fetchone()[0] == 1

        conn.close()

    def test_find_or_create_person_email_case_insensitive(self):
        """Test email matching is case-insensitive."""
        conn = create_database(":memory:")

        person_id1 = find_or_create_person(conn, "John Doe", "John.Doe@Example.COM")
        person_id2 = find_or_create_person(conn, "John Doe", "john.doe@example.com")

        assert person_id1 == person_id2

        conn.close()

    def test_find_or_create_person_updates_name(self):
        """Test that longer name replaces shorter name."""
        conn = create_database(":memory:")

        # Create with short name
        person_id = find_or_create_person(conn, "J. Doe", "john.doe@example.com")

        # Update with longer name
        find_or_create_person(conn, "John Doe", "john.doe@example.com")

        # Should have updated name
        cursor = conn.execute("SELECT name FROM people WHERE id = ?", (person_id,))
        assert cursor.fetchone()[0] == "John Doe"

        conn.close()

    def test_find_or_create_person_no_email(self):
        """Test creating person without email."""
        conn = create_database(":memory:")

        person_id = find_or_create_person(conn, "Jane Smith")
        assert person_id is not None

        cursor = conn.execute("SELECT name, email FROM people WHERE id = ?", (person_id,))
        row = cursor.fetchone()
        assert row[0] == "Jane Smith"
        assert row[1] is None

        conn.close()

    def test_merge_people(self):
        """Test merging two person records."""
        conn = create_database(":memory:")

        # Create two people
        person1_id = find_or_create_person(conn, "John Doe", "john.doe@example.com")
        person2_id = find_or_create_person(conn, "Jane Smith", "jane.smith@example.com")

        # Create email and link to person2
        conn.execute(
            "INSERT INTO emails (message_id, date_received) VALUES (1, '2026-03-17T10:00:00')"
        )
        email_id = conn.execute("SELECT id FROM emails WHERE message_id = 1").fetchone()[0]

        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, 'sender')",
            (email_id, person2_id),
        )
        conn.commit()

        # Merge person2 into person1
        merge_people(conn, person1_id, person2_id)

        # person2 should be deleted
        cursor = conn.execute("SELECT COUNT(*) FROM people WHERE id = ?", (person2_id,))
        assert cursor.fetchone()[0] == 0

        # email_people should now reference person1
        cursor = conn.execute("SELECT person_id FROM email_people WHERE email_id = ?", (email_id,))
        assert cursor.fetchone()[0] == person1_id

        conn.close()

    def test_merge_people_with_duplicate_references(self):
        """Test merging when both people are linked to same email with same role."""
        conn = create_database(":memory:")

        person1_id = find_or_create_person(conn, "John Doe", "john.doe@example.com")
        person2_id = find_or_create_person(conn, "J. Doe")

        # Create an email linked to BOTH people with the same role
        conn.execute(
            "INSERT INTO emails (message_id, date_received) VALUES (1, '2026-03-17T10:00:00')"
        )
        email_id = conn.execute("SELECT id FROM emails WHERE message_id = 1").fetchone()[0]

        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, 'sender')",
            (email_id, person1_id),
        )
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, 'sender')",
            (email_id, person2_id),
        )
        conn.commit()

        # Merge should not raise IntegrityError despite duplicate (email_id, role)
        merge_people(conn, person1_id, person2_id)

        # person2 deleted, only person1 remains linked
        assert (
            conn.execute("SELECT COUNT(*) FROM people WHERE id = ?", (person2_id,)).fetchone()[0]
            == 0
        )
        refs = conn.execute(
            "SELECT person_id FROM email_people WHERE email_id = ?", (email_id,)
        ).fetchall()
        assert len(refs) == 1
        assert refs[0][0] == person1_id

        conn.close()


class TestLoader:
    """Test loading extracted emails into database."""

    def test_load_single_email_basic(self):
        """Test loading a single email with basic metadata."""
        conn = create_database(":memory:")

        metadata = {
            "message_id": 12345,
            "date_received": "2026-03-17T10:30:00",
            "sender": {"name": "John Doe", "address": "john.doe@example.com"},
            "subject": "Test email",
            "content": "This is a test email.",
            "mailbox": "INBOX",
            "to": [],
            "cc": [],
        }

        extraction = {
            "summary": "Test email about testing",
            "sentiment": "informational",
            "urgency": "low",
            "language": "english",
            "topics": [],
            "decisions": [],
            "action_items": [],
            "people_roles": {},
            "key_facts": [],
        }

        result = load_single_email(conn, metadata, extraction)
        assert result is True

        # Verify email was inserted
        cursor = conn.execute("SELECT message_id, subject, summary FROM emails")
        row = cursor.fetchone()
        assert row[0] == 12345
        assert row[1] == "Test email"
        assert row[2] == "Test email about testing"

        conn.close()

    def test_load_single_email_duplicate(self):
        """Test that duplicate message_id is skipped."""
        conn = create_database(":memory:")

        metadata = {
            "message_id": 12345,
            "date_received": "2026-03-17T10:30:00",
            "sender": {"name": "John Doe", "address": "john.doe@example.com"},
            "subject": "Test email",
            "content": "This is a test email.",
            "mailbox": "INBOX",
            "to": [],
            "cc": [],
        }

        extraction = {
            "summary": "Test email",
            "sentiment": "informational",
            "urgency": "low",
            "language": "english",
            "topics": [],
            "decisions": [],
            "action_items": [],
            "people_roles": {},
            "key_facts": [],
        }

        # Load first time
        result1 = load_single_email(conn, metadata, extraction)
        assert result1 is True

        # Load second time
        result2 = load_single_email(conn, metadata, extraction)
        assert result2 is False

        # Should only have one record
        cursor = conn.execute("SELECT COUNT(*) FROM emails")
        assert cursor.fetchone()[0] == 1

        conn.close()

    def test_load_single_email_with_topics(self):
        """Test loading email with topics."""
        conn = create_database(":memory:")

        metadata = {
            "message_id": 12345,
            "date_received": "2026-03-17T10:30:00",
            "sender": {"name": "John Doe", "address": "john.doe@example.com"},
            "subject": "Migration update",
            "content": "Update on cards migration.",
            "mailbox": "INBOX",
            "to": [],
            "cc": [],
        }

        extraction = {
            "summary": "Migration progress update",
            "sentiment": "informational",
            "urgency": "medium",
            "language": "english",
            "topics": ["Cards Migration", "Digital Banking"],
            "decisions": [],
            "action_items": [],
            "people_roles": {},
            "key_facts": [],
        }

        load_single_email(conn, metadata, extraction)

        # Verify topics were created
        cursor = conn.execute("SELECT COUNT(*) FROM topics")
        assert cursor.fetchone()[0] == 2

        # Verify email-topic links
        cursor = conn.execute("SELECT COUNT(*) FROM email_topics")
        assert cursor.fetchone()[0] == 2

        conn.close()

    def test_load_single_email_with_decisions(self):
        """Test loading email with decisions."""
        conn = create_database(":memory:")

        metadata = {
            "message_id": 12345,
            "date_received": "2026-03-17T10:30:00",
            "sender": {"name": "John Doe", "address": "john.doe@example.com"},
            "subject": "Approval",
            "content": "Approved.",
            "mailbox": "INBOX",
            "to": [],
            "cc": [],
        }

        extraction = {
            "summary": "Approval granted",
            "sentiment": "directive",
            "urgency": "high",
            "language": "english",
            "topics": [],
            "decisions": [
                {
                    "decision": "Approve budget increase",
                    "decided_by": "John Doe",
                    "decision_date": "2026-03-17",
                },
                "Use vendor A for the project",  # Simple string format
            ],
            "action_items": [],
            "people_roles": {},
            "key_facts": [],
        }

        load_single_email(conn, metadata, extraction)

        # Verify decisions were inserted
        cursor = conn.execute("SELECT decision, decided_by FROM decisions ORDER BY id")
        rows = cursor.fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "Approve budget increase"
        assert rows[0][1] == "John Doe"
        assert rows[1][0] == "Use vendor A for the project"

        conn.close()

    def test_load_single_email_with_action_items(self):
        """Test loading email with action items."""
        conn = create_database(":memory:")

        metadata = {
            "message_id": 12345,
            "date_received": "2026-03-17T10:30:00",
            "sender": {"name": "John Doe", "address": "john.doe@example.com"},
            "subject": "Tasks",
            "content": "Tasks assigned.",
            "mailbox": "INBOX",
            "to": [],
            "cc": [],
        }

        extraction = {
            "summary": "Task assignments",
            "sentiment": "directive",
            "urgency": "high",
            "language": "english",
            "topics": [],
            "decisions": [],
            "action_items": [
                {
                    "task": "Review proposal",
                    "owner": "Jane Smith",
                    "deadline": "2026-03-20",
                    "status": "open",
                },
                "Schedule meeting",  # Simple string format
            ],
            "people_roles": {},
            "key_facts": [],
        }

        load_single_email(conn, metadata, extraction)

        # Verify action items were inserted
        cursor = conn.execute("SELECT task, owner, status FROM action_items ORDER BY id")
        rows = cursor.fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "Review proposal"
        assert rows[0][1] == "Jane Smith"
        assert rows[0][2] == "open"
        assert rows[1][0] == "Schedule meeting"

        conn.close()

    def test_load_single_email_with_people_roles(self):
        """Test loading email with people and their roles."""
        conn = create_database(":memory:")

        metadata = {
            "message_id": 12345,
            "date_received": "2026-03-17T10:30:00",
            "sender": {"name": "John Doe", "address": "john.doe@example.com"},
            "subject": "Decision",
            "content": "Decision made.",
            "mailbox": "INBOX",
            "to": [{"name": "Jane Smith", "address": "jane.smith@example.com"}],
            "cc": [],
        }

        extraction = {
            "summary": "Decision notification",
            "sentiment": "informational",
            "urgency": "medium",
            "language": "english",
            "topics": [],
            "decisions": [],
            "action_items": [],
            "people_roles": {
                "John Doe": "decision-maker",
                "Jane Smith": ["reviewer", "FYI"],
            },
            "key_facts": [],
        }

        load_single_email(conn, metadata, extraction)

        # Verify people were created
        cursor = conn.execute("SELECT COUNT(*) FROM people")
        assert cursor.fetchone()[0] == 2

        # Verify people roles (3 from people_roles + 1 sender + 1 recipient)
        cursor = conn.execute("SELECT COUNT(*) FROM email_people")
        count = cursor.fetchone()[0]
        assert count >= 3  # At least the people_roles entries

        # Check specific roles
        cursor = conn.execute(
            """
            SELECT p.name, ep.role_in_email
            FROM email_people ep
            JOIN people p ON p.id = ep.person_id
            ORDER BY p.name, ep.role_in_email
            """
        )
        roles = cursor.fetchall()
        assert any(r[0] == "John Doe" and r[1] == "decision-maker" for r in roles)
        assert any(r[0] == "Jane Smith" and r[1] == "reviewer" for r in roles)

        conn.close()

    def test_load_single_email_with_key_facts(self):
        """Test loading email with key facts."""
        conn = create_database(":memory:")

        metadata = {
            "message_id": 12345,
            "date_received": "2026-03-17T10:30:00",
            "sender": {"name": "John Doe", "address": "john.doe@example.com"},
            "subject": "Update",
            "content": "Important update.",
            "mailbox": "INBOX",
            "to": [],
            "cc": [],
        }

        extraction = {
            "summary": "Important facts shared",
            "sentiment": "informational",
            "urgency": "medium",
            "language": "english",
            "topics": [],
            "decisions": [],
            "action_items": [],
            "people_roles": {},
            "key_facts": [
                "Budget increased by 20%",
                "Launch date moved to Q2",
                "New vendor selected",
            ],
        }

        load_single_email(conn, metadata, extraction)

        # Verify key facts were inserted
        cursor = conn.execute("SELECT fact FROM key_facts ORDER BY id")
        facts = [row[0] for row in cursor.fetchall()]
        assert len(facts) == 3
        assert "Budget increased by 20%" in facts
        assert "Launch date moved to Q2" in facts
        assert "New vendor selected" in facts

        conn.close()

    def test_load_single_email_uses_outlook_conversation_id(self):
        """ConversationId from Outlook (when present) is the primary thread anchor."""
        conn = create_database(":memory:")

        metadata = {
            "message_id": 99001,
            "date_received": "2026-04-22T08:00:00",
            "sender": {"name": "Alice", "address": "alice@example.com"},
            "subject": "Re: Project status",
            "content": "Reply body",
            "mailbox": "INBOX",
            "to": [],
            "cc": [],
            # Outlook ConversationId — must be preferred over subject hash
            "conversation_id": "AAQkAD-outlook-conv-xyz",
        }
        extraction = {
            "summary": "Reply about project status",
            "sentiment": "informational",
            "urgency": "low",
            "language": "english",
            "topics": [],
            "decisions": [],
            "action_items": [],
            "people_roles": {},
            "key_facts": [],
        }

        assert load_single_email(conn, metadata, extraction) is True

        cursor = conn.execute(
            "SELECT conversation_id FROM emails WHERE message_id = ?",
            (99001,),
        )
        row = cursor.fetchone()
        assert row[0] == "AAQkAD-outlook-conv-xyz"
        conn.close()

    def test_load_single_email_falls_back_to_subject_when_no_conversation_id(self):
        """Without Outlook ConversationId, fall back to existing subject-based hash."""
        conn = create_database(":memory:")

        metadata = {
            "message_id": 99002,
            "date_received": "2026-04-22T09:00:00",
            "sender": {"name": "Bob", "address": "bob@example.com"},
            "subject": "Re: Status update",
            "content": "No conv id here",
            "mailbox": "INBOX",
            "to": [],
            "cc": [],
            # No conversation_id, no in_reply_to, no references
        }
        extraction = {
            "summary": "Status update reply",
            "sentiment": "informational",
            "urgency": "low",
            "language": "english",
            "topics": [],
            "decisions": [],
            "action_items": [],
            "people_roles": {},
            "key_facts": [],
        }

        assert load_single_email(conn, metadata, extraction) is True

        # Subject-based fallback still produces a non-null hashed conversation_id
        cursor = conn.execute(
            "SELECT conversation_id FROM emails WHERE message_id = ?",
            (99002,),
        )
        row = cursor.fetchone()
        assert row[0] is not None
        assert len(row[0]) == 16  # sha256[:16] hex
        # Must NOT be the placeholder we used in the previous test
        assert row[0] != "AAQkAD-outlook-conv-xyz"
        conn.close()

    def test_load_extractions_full_workflow(self):
        """Test full workflow: load batch files and extractions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Create database
            db_path = str(tmppath / "test.db")
            create_database(db_path).close()

            # Create staging and extracted directories
            staging_dir = tmppath / "staging"
            staging_dir.mkdir()
            extracted_dir = tmppath / "extracted"
            extracted_dir.mkdir()

            # Create sample batch file
            batch_data = [
                {
                    "message_id": 1001,
                    "date_received": "2026-03-17T10:00:00",
                    "sender": {"name": "Alice", "address": "alice@example.com"},
                    "subject": "First email",
                    "content": "First email content",
                    "mailbox": "INBOX",
                    "to": [],
                    "cc": [],
                },
                {
                    "message_id": 1002,
                    "date_received": "2026-03-17T11:00:00",
                    "sender": {"name": "Bob", "address": "bob@example.com"},
                    "subject": "Second email",
                    "content": "Second email content",
                    "mailbox": "INBOX",
                    "to": [],
                    "cc": [],
                },
            ]

            with open(staging_dir / "batch-00001.json", "w") as f:
                json.dump(batch_data, f)

            # Create extraction files
            extraction1 = {
                "summary": "First email summary",
                "sentiment": "informational",
                "urgency": "low",
                "language": "english",
                "topics": ["Topic A"],
                "decisions": [],
                "action_items": [],
                "people_roles": {},
                "key_facts": [],
            }

            extraction2 = {
                "summary": "Second email summary",
                "sentiment": "directive",
                "urgency": "high",
                "language": "english",
                "topics": ["Topic B"],
                "decisions": ["Decision X"],
                "action_items": ["Task Y"],
                "people_roles": {},
                "key_facts": ["Fact Z"],
            }

            with open(extracted_dir / "1001.json", "w") as f:
                json.dump(extraction1, f)

            with open(extracted_dir / "1002.json", "w") as f:
                json.dump(extraction2, f)

            # Load extractions
            count = load_extractions(db_path, str(extracted_dir), str(staging_dir))

            assert count == 2

            # Verify data was loaded
            conn = get_connection(db_path)

            cursor = conn.execute("SELECT COUNT(*) FROM emails")
            assert cursor.fetchone()[0] == 2

            cursor = conn.execute("SELECT COUNT(*) FROM topics")
            assert cursor.fetchone()[0] == 2

            cursor = conn.execute("SELECT COUNT(*) FROM decisions")
            assert cursor.fetchone()[0] == 1

            cursor = conn.execute("SELECT COUNT(*) FROM action_items")
            assert cursor.fetchone()[0] == 1

            cursor = conn.execute("SELECT COUNT(*) FROM key_facts")
            assert cursor.fetchone()[0] == 1

            conn.close()

    def test_load_extractions_missing_directories(self):
        """Test that load_extractions raises error for missing directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            db_path = str(tmppath / "test.db")
            create_database(db_path).close()

            with pytest.raises(FileNotFoundError):
                load_extractions(db_path, "/nonexistent/extracted", "/nonexistent/staging")

    def test_load_extractions_case_insensitive_extraction_filename(self):
        """Match extraction files case-insensitively.

        Outlook message-ids are case-sensitive base64, but macOS/APFS is
        case-insensitive (and case-preserving), so the extraction file on
        disk can end up with a different letter-case than the staged
        message_id. The loader must still pair them, using the staged
        message_id as the canonical (correct-case) identifier.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            db_path = str(tmppath / "test.db")
            create_database(db_path).close()
            staging_dir = tmppath / "staging"
            staging_dir.mkdir()
            extracted_dir = tmppath / "extracted"
            extracted_dir.mkdir()

            mid = "AAMkExampleAbCdEfGhIj=="
            batch = {
                "batch_number": 1,
                "exported_at": "2026-05-30T00:00:00Z",
                "emails": [
                    {
                        "message_id": mid,
                        "date_received": "2026-05-30T10:00:00",
                        "sender": {"name": "Alice", "address": "alice@example.com"},
                        "subject": "Case test",
                        "content": "body",
                        "mailbox": "Inbox",
                        "to": [],
                        "cc": [],
                        "internet_message_id": "<case-test@example.com>",
                    }
                ],
            }
            with open(staging_dir / "batch-00001.json", "w") as f:
                json.dump(batch, f)

            extraction = {
                "summary": "Case summary",
                "sentiment": "informational",
                "urgency": "low",
                "language": "english",
                "topics": [],
                "decisions": [],
                "action_items": [],
                "people_roles": {},
                "key_facts": [],
            }
            # Written with a DIFFERENT case than the staged message_id.
            with open(extracted_dir / (mid.lower() + ".json"), "w") as f:
                json.dump(extraction, f)

            count = load_extractions(db_path, str(extracted_dir), str(staging_dir))
            assert count == 1

            conn = get_connection(db_path)
            row = conn.execute("SELECT message_id FROM emails").fetchone()
            assert row[0] == mid  # canonical (staged) case is preserved
            conn.close()

    def test_load_extractions_prunes_cross_source_duplicate_batch(self):
        """Prune batches whose emails are cross-source duplicates.

        A staged Outlook email whose RFC822 internet_message_id already
        exists in the DB (captured via another export under a different
        source message_id) is a duplicate and is correctly not re-inserted —
        but its batch must still be pruned so staging drains instead of
        accumulating forever.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            db_path = str(tmppath / "test.db")
            conn = create_database(db_path)
            # Pre-existing email from another source: numeric id + RFC822 IM.
            conn.execute(
                "INSERT INTO emails (message_id, date_received, internet_message_id) "
                "VALUES (?, ?, ?)",
                ("9001", "2026-05-30T09:00:00", "<dup@example.com>"),
            )
            conn.commit()
            conn.close()

            staging_dir = tmppath / "staging"
            staging_dir.mkdir()
            extracted_dir = tmppath / "extracted"
            extracted_dir.mkdir()

            mid = "AAMkOutlookDupId=="
            batch = {
                "batch_number": 1,
                "exported_at": "2026-05-30T00:00:00Z",
                "emails": [
                    {
                        "message_id": mid,
                        "date_received": "2026-05-30T09:00:00",
                        "sender": {"name": "A", "address": "a@example.com"},
                        "subject": "dup",
                        "content": "b",
                        "mailbox": "Inbox",
                        "to": [],
                        "cc": [],
                        "internet_message_id": "<dup@example.com>",
                    }
                ],
            }
            batch_file = staging_dir / "batch-00001.json"
            with open(batch_file, "w") as f:
                json.dump(batch, f)
            with open(extracted_dir / (mid + ".json"), "w") as f:
                json.dump(
                    {
                        "summary": "s",
                        "topics": [],
                        "decisions": [],
                        "action_items": [],
                        "people_roles": {},
                        "key_facts": [],
                    },
                    f,
                )

            count = load_extractions(db_path, str(extracted_dir), str(staging_dir))
            # Cross-source duplicate → not newly inserted ...
            assert count == 0
            # ... but its batch must be pruned (drained), not kept forever.
            assert not batch_file.exists()


class TestFTS5Search:
    """Test full-text search functionality."""

    def test_email_fts_search(self):
        """Test searching emails by summary."""
        conn = create_database(":memory:")

        # Insert test emails
        conn.execute(
            """
            INSERT INTO emails (message_id, date_received, summary)
            VALUES (1, '2026-03-17T10:00:00', 'Cards migration project update')
            """
        )
        conn.execute(
            """
            INSERT INTO emails (message_id, date_received, summary)
            VALUES (2, '2026-03-17T11:00:00', 'Digital banking transformation roadmap')
            """
        )
        conn.commit()

        # Search for "migration"
        cursor = conn.execute(
            """
            SELECT e.message_id, e.summary
            FROM emails e
            WHERE e.id IN (SELECT rowid FROM emails_fts WHERE summary MATCH 'migration')
            """
        )
        results = cursor.fetchall()
        assert len(results) == 1
        assert results[0][0] == 1

        # Search for "digital"
        cursor = conn.execute(
            """
            SELECT e.message_id, e.summary
            FROM emails e
            WHERE e.id IN (SELECT rowid FROM emails_fts WHERE summary MATCH 'digital')
            """
        )
        results = cursor.fetchall()
        assert len(results) == 1
        assert results[0][0] == 2

        conn.close()

    def test_key_facts_fts_search(self):
        """Test searching key facts."""
        conn = create_database(":memory:")

        # Insert email and facts
        conn.execute(
            "INSERT INTO emails (message_id, date_received) VALUES (1, '2026-03-17T10:00:00')"
        )
        email_id = conn.execute("SELECT id FROM emails WHERE message_id = 1").fetchone()[0]

        conn.execute(
            "INSERT INTO key_facts (email_id, fact) VALUES (?, 'Budget increased by 20%')",
            (email_id,),
        )
        conn.execute(
            "INSERT INTO key_facts (email_id, fact) VALUES (?, 'Timeline extended to Q3')",
            (email_id,),
        )
        conn.commit()

        # Search for "budget"
        cursor = conn.execute(
            """
            SELECT kf.fact
            FROM key_facts kf
            WHERE kf.id IN (SELECT rowid FROM key_facts_fts WHERE fact MATCH 'budget')
            """
        )
        results = cursor.fetchall()
        assert len(results) == 1
        assert "Budget" in results[0][0]

        # Search for "timeline"
        cursor = conn.execute(
            """
            SELECT kf.fact
            FROM key_facts kf
            WHERE kf.id IN (SELECT rowid FROM key_facts_fts WHERE fact MATCH 'timeline')
            """
        )
        results = cursor.fetchall()
        assert len(results) == 1
        assert "Timeline" in results[0][0]

        conn.close()
