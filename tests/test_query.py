"""Tests for the query interface.

Uses in-memory SQLite databases with sample data to test all query functions.
"""

from unittest.mock import patch

import pytest

from src.store.normalizer import find_or_create_person, find_or_create_topic
from src.store.query import (
    find_overdue_actions,
    find_stale_threads,
    get_stats,
    meeting_prep,
    query_action_items,
    query_by_date_range,
    query_by_keyword,
    query_by_person,
    query_by_topic,
    query_combined,
    query_decisions,
    query_thread,
)
from src.store.schema import create_database, normalize_subject, subject_to_conversation_id


@pytest.fixture
def sample_db():
    """Create an in-memory database with sample data for testing."""
    conn = create_database(":memory:")

    # Insert sample emails
    emails = [
        (
            1,
            "2026-03-15T10:00:00",
            "Alice",
            "alice@example.com",
            "Cards migration update",
            "Update on the cards migration project progress",
            "informational",
            "medium",
            "english",
            "INBOX",
            "Full content here",
            subject_to_conversation_id("Cards migration update"),
        ),
        (
            2,
            "2026-03-16T14:30:00",
            "Bob",
            "bob@example.com",
            "Budget approval needed",
            "Request for budget approval for Q2",
            "request",
            "high",
            "english",
            "INBOX",
            "Budget details",
            subject_to_conversation_id("Budget approval needed"),
        ),
        (
            3,
            "2026-03-17T09:00:00",
            "Charlie",
            "charlie@example.com",
            "Meeting notes",
            "Notes from the strategy meeting",
            "informational",
            "low",
            "english",
            "INBOX",
            "Meeting notes content",
            subject_to_conversation_id("Meeting notes"),
        ),
        (
            4,
            "2026-03-17T11:00:00",
            "Alice",
            "alice@example.com",
            "Digital banking roadmap",
            "Roadmap for digital banking transformation",
            "informational",
            "high",
            "english",
            "INBOX",
            "Roadmap details",
            subject_to_conversation_id("Digital banking roadmap"),
        ),
        (
            5,
            "2026-03-18T15:00:00",
            "Bob",
            "bob@example.com",
            "Action items from meeting",
            "Follow-up action items from yesterday meeting",
            "directive",
            "high",
            "english",
            "INBOX",
            "Action items list",
            subject_to_conversation_id("Action items from meeting"),
        ),
    ]

    for email in emails:
        conn.execute(
            """
            INSERT INTO emails (message_id, date_received, sender_name, sender_address,
                               subject, summary, sentiment, urgency, language, mailbox_name, content,
                               conversation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            email,
        )

    # Create topics
    topic1_id = find_or_create_topic(conn, "Cards Migration")
    topic2_id = find_or_create_topic(conn, "Digital Banking")
    topic3_id = find_or_create_topic(conn, "Budget")
    topic4_id = find_or_create_topic(conn, "Strategy")

    # Link topics to emails
    conn.execute("INSERT INTO email_topics (email_id, topic_id) VALUES (1, ?)", (topic1_id,))
    conn.execute("INSERT INTO email_topics (email_id, topic_id) VALUES (2, ?)", (topic3_id,))
    conn.execute("INSERT INTO email_topics (email_id, topic_id) VALUES (3, ?)", (topic4_id,))
    conn.execute("INSERT INTO email_topics (email_id, topic_id) VALUES (4, ?)", (topic2_id,))
    conn.execute("INSERT INTO email_topics (email_id, topic_id) VALUES (5, ?)", (topic4_id,))

    # Create people
    alice_id = find_or_create_person(conn, "Alice", "alice@example.com")
    bob_id = find_or_create_person(conn, "Bob", "bob@example.com")
    charlie_id = find_or_create_person(conn, "Charlie", "charlie@example.com")
    dave_id = find_or_create_person(conn, "Dave", "dave@example.com")

    # Link people to emails
    conn.execute(
        "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (1, ?, 'sender')",
        (alice_id,),
    )
    conn.execute(
        "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (1, ?, 'recipient')",
        (bob_id,),
    )
    conn.execute(
        "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (2, ?, 'sender')",
        (bob_id,),
    )
    conn.execute(
        "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (2, ?, 'decision-maker')",
        (charlie_id,),
    )
    conn.execute(
        "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (3, ?, 'sender')",
        (charlie_id,),
    )
    conn.execute(
        "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (4, ?, 'sender')",
        (alice_id,),
    )
    conn.execute(
        "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (5, ?, 'sender')",
        (bob_id,),
    )
    conn.execute(
        "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (5, ?, 'recipient')",
        (dave_id,),
    )

    # Add decisions
    conn.execute(
        "INSERT INTO decisions (email_id, decision, decided_by, decision_date) VALUES (2, 'Approve Q2 budget increase', 'Charlie', '2026-03-16')"
    )
    conn.execute(
        "INSERT INTO decisions (email_id, decision, decided_by, decision_date) VALUES (3, 'Proceed with vendor A', 'Alice', '2026-03-17')"
    )

    # Add action items
    conn.execute(
        "INSERT INTO action_items (email_id, task, owner, deadline, status) VALUES (5, 'Review proposal', 'Dave', '2026-03-20', 'open')"
    )
    conn.execute(
        "INSERT INTO action_items (email_id, task, owner, deadline, status) VALUES (5, 'Schedule follow-up', 'Bob', '2026-03-22', 'open')"
    )
    conn.execute(
        "INSERT INTO action_items (email_id, task, owner, deadline, status) VALUES (3, 'Update documentation', 'Alice', '2026-03-19', 'completed')"
    )

    # Add key facts
    email_id_1 = conn.execute("SELECT id FROM emails WHERE message_id = 1").fetchone()[0]
    email_id_2 = conn.execute("SELECT id FROM emails WHERE message_id = 2").fetchone()[0]

    conn.execute(
        "INSERT INTO key_facts (email_id, fact) VALUES (?, 'Migration timeline extended to Q2')",
        (email_id_1,),
    )
    conn.execute(
        "INSERT INTO key_facts (email_id, fact) VALUES (?, 'Budget increased by 15%')",
        (email_id_2,),
    )

    # Add an attachment on email 4 (Digital banking roadmap).
    # Use a unique keyword 'panorama42' that appears ONLY in the attachment
    # text/summary, never in any email field, so attachment-only matches can
    # be unambiguously verified.
    email_id_4 = conn.execute("SELECT id FROM emails WHERE message_id = 4").fetchone()[0]
    cursor = conn.execute(
        """
        INSERT INTO attachments (email_id, message_id, filename, mime_type,
                                 file_size, file_path, is_inline, exported_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            email_id_4,
            4,
            "Q4_Strategy_Deck.pdf",
            "application/pdf",
            1024,
            "/tmp/Q4_Strategy_Deck.pdf",
            "2026-03-17T11:05:00",
        ),
    )
    attachment_id = cursor.lastrowid
    conn.execute(
        """
        INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,
                                        extraction_status, extracted_at, summary, language,
                                        llm_status, llm_extracted_at)
        VALUES (?, ?, 'pdfplumber', 'extracted', ?, ?, 'english', 'extracted', ?)
        """,
        (
            attachment_id,
            "Strategic panorama42 analysis covering Q4 outlook",
            "2026-03-17T11:10:00",
            "Q4 strategic deck reviewing the market panorama42 and competitive landscape",
            "2026-03-17T11:15:00",
        ),
    )

    conn.commit()
    return conn


class TestQueryByPerson:
    """Test querying emails by person."""

    def test_query_by_person_name(self, sample_db):
        """Test querying by person name."""
        results = query_by_person(sample_db, "Alice")

        assert len(results) == 2  # Alice sent 2 emails
        assert all(
            r["subject"] in ["Cards migration update", "Digital banking roadmap"] for r in results
        )

    def test_query_by_person_partial_name(self, sample_db):
        """Test querying by partial person name."""
        results = query_by_person(sample_db, "Ali")

        # Should match Alice (who sent 2 emails and is recipient on 1)
        assert len(results) >= 2
        # All results should be emails involving someone matching 'Ali'
        assert all(
            r["subject"]
            in [
                "Cards migration update",
                "Digital banking roadmap",
                "Budget approval needed",
            ]
            for r in results
        )

    def test_query_by_person_email(self, sample_db):
        """Test querying by email address."""
        results = query_by_person(sample_db, "bob@example.com")

        assert len(results) >= 2  # Bob sent 2 emails
        assert any(r["subject"] == "Budget approval needed" for r in results)

    def test_query_by_person_email_case_insensitive(self, sample_db):
        """Test email search is case-insensitive."""
        results = query_by_person(sample_db, "BOB@EXAMPLE.COM")

        assert len(results) >= 2

    def test_query_by_person_with_limit(self, sample_db):
        """Test limit parameter works."""
        results = query_by_person(sample_db, "Alice", limit=1)

        assert len(results) == 1

    def test_query_by_person_no_results(self, sample_db):
        """Test query returns empty list when no matches."""
        results = query_by_person(sample_db, "NonexistentPerson")

        assert results == []

    def test_query_by_person_includes_role(self, sample_db):
        """Test results include person role."""
        results = query_by_person(sample_db, "Bob")

        assert all("person_role" in r for r in results)
        # Bob is both sender and recipient in different emails
        roles = [r["person_role"] for r in results]
        assert "sender" in roles or "recipient" in roles


class TestQueryByTopic:
    """Test querying emails by topic."""

    def test_query_by_topic_exact(self, sample_db):
        """Test querying by exact topic name."""
        results = query_by_topic(sample_db, "Cards Migration")

        assert len(results) == 1
        assert results[0]["subject"] == "Cards migration update"

    def test_query_by_topic_partial(self, sample_db):
        """Test querying by partial topic name."""
        results = query_by_topic(sample_db, "digital")

        assert len(results) == 1
        assert results[0]["subject"] == "Digital banking roadmap"

    def test_query_by_topic_case_insensitive(self, sample_db):
        """Test topic search is case-insensitive."""
        results = query_by_topic(sample_db, "BUDGET")

        assert len(results) == 1
        assert results[0]["subject"] == "Budget approval needed"

    def test_query_by_topic_with_limit(self, sample_db):
        """Test limit parameter works."""
        results = query_by_topic(sample_db, "Strategy", limit=1)

        assert len(results) == 1

    def test_query_by_topic_no_results(self, sample_db):
        """Test query returns empty list when no matches."""
        results = query_by_topic(sample_db, "NonexistentTopic")

        assert results == []


class TestQueryByKeyword:
    """Test full-text keyword search."""

    def test_query_by_keyword_in_summary(self, sample_db):
        """Test searching in email summaries."""
        results = query_by_keyword(sample_db, "migration")

        assert len(results) >= 1
        assert any("migration" in r["summary"].lower() for r in results)

    def test_query_by_keyword_in_key_facts(self, sample_db):
        """Test searching in key facts."""
        results = query_by_keyword(sample_db, "timeline")

        assert len(results) >= 1
        assert any(r["source"] == "key_fact" for r in results)

    def test_query_by_keyword_multiple_sources(self, sample_db):
        """Test keyword appearing in both summary and key facts."""
        results = query_by_keyword(sample_db, "budget")

        assert len(results) >= 1
        # Budget appears in both subject/summary and key facts

    def test_query_by_keyword_with_limit(self, sample_db):
        """Test limit parameter works."""
        results = query_by_keyword(sample_db, "meeting", limit=1)

        assert len(results) <= 1

    def test_query_by_keyword_in_content(self, sample_db):
        """Test searching finds matches in email body content."""
        # 'Roadmap' appears in content of email 4 ('Roadmap details') but not in any summary
        results = query_by_keyword(sample_db, "details")

        assert len(results) >= 1
        assert any(r["source"] == "content" for r in results)

    def test_query_by_keyword_content_snippet(self, sample_db):
        """Test that content search returns snippet extraction."""
        results = query_by_keyword(sample_db, "details")

        content_results = [r for r in results if r["source"] == "content"]
        assert len(content_results) >= 1
        # snippet should contain the matched term (possibly with <b> tags)
        assert any("details" in r["snippet"].lower() for r in content_results)

    def test_query_by_keyword_search_content_only(self, sample_db):
        """Test search_content_only parameter restricts to content column."""
        results = query_by_keyword(sample_db, "details", search_content_only=True)

        assert len(results) >= 1
        # All results should be from content, not summary or key_fact
        assert all(r["source"] == "content" for r in results)

    def test_query_by_keyword_no_results(self, sample_db):
        """Test query returns empty list when no matches."""
        results = query_by_keyword(sample_db, "nonexistentkeyword")

        assert results == []

    def test_query_by_keyword_in_attachment(self, sample_db):
        """Attachment-only matches surface as parent email rows with source='attachment'."""
        # 'panorama42' lives only in the attachment fixture, not in any email field.
        # Sanity-check that assumption first.
        leaks = sample_db.execute(
            "SELECT 1 FROM emails WHERE summary LIKE '%panorama42%' OR content LIKE '%panorama42%' OR subject LIKE '%panorama42%'"
        ).fetchall()
        assert leaks == [], "fixture invariant: panorama42 must not appear in email fields"

        results = query_by_keyword(sample_db, "panorama42")

        assert len(results) >= 1
        att_hits = [r for r in results if r["source"] == "attachment"]
        assert att_hits, "expected at least one result with source='attachment'"
        # The match must surface as the parent email (Digital banking roadmap)
        assert any(r["subject"] == "Digital banking roadmap" for r in att_hits)
        # The matched attachment filename should be exposed for caller transparency
        assert any(r.get("attachment_filename") == "Q4_Strategy_Deck.pdf" for r in att_hits)

    def test_query_by_keyword_attachment_dedup(self, sample_db):
        """If a keyword matches both an email field and one of its attachments, the email appears once."""
        # 'roadmap' is in email 4 summary AND we'll add it to the attachment too.
        # First confirm email 4 already matches via summary:
        results_before = query_by_keyword(sample_db, "roadmap")
        email_4_hits = [r for r in results_before if r["subject"] == "Digital banking roadmap"]
        assert len(email_4_hits) == 1, "email 4 should appear exactly once via its summary"

    def test_query_combined_keyword_in_attachment(self, sample_db):
        """query_combined keyword filter also matches attachment content."""
        results = query_combined(sample_db, keyword="panorama42")
        assert len(results) >= 1
        assert any(r["subject"] == "Digital banking roadmap" for r in results)

    def test_query_by_keyword_special_chars(self, sample_db):
        """Test FTS5 special characters are safely handled (no crashes or unexpected results)."""
        # These would crash or give wrong results without sanitization:
        # - "action-items" would be parsed as "action NOT items"
        # - '"quotes"' could break FTS5 syntax
        # - 'OR' is a FTS5 operator
        for dangerous_kw in [
            "action-items",
            '"quoted"',
            "foo OR bar",
            "NEAR(a,b)",
            "col:value",
            "pre*fix",
        ]:
            results = query_by_keyword(sample_db, dangerous_kw)
            assert isinstance(results, list)  # must not crash

    def test_query_by_keyword_ranks_by_relevance_not_recency(self, sample_db):
        """BM25 relevance must beat recency. A denser match with an OLDER date must
        outrank a sparse match with a NEWER date. Guards the fix that replaced
        `ORDER BY date_received` with `ORDER BY rank` in query_by_keyword."""
        # Dense match (term x5 in a short summary), OLDER date.
        sample_db.execute(
            """
            INSERT INTO emails (message_id, date_received, sender_name, sender_address,
                               subject, summary, sentiment, urgency, language, mailbox_name,
                               content, conversation_id)
            VALUES (9001, '2020-01-01T00:00:00', 'X', 'x@example.com', 'Dense',
                    'quokka quokka quokka quokka quokka', 'neutral', 'low', 'english',
                    'INBOX', 'quokka quokka quokka', 'conv-dense')
            """
        )
        # Sparse match (term x1 in a longer summary), NEWER date.
        sample_db.execute(
            """
            INSERT INTO emails (message_id, date_received, sender_name, sender_address,
                               subject, summary, sentiment, urgency, language, mailbox_name,
                               content, conversation_id)
            VALUES (9002, '2026-12-31T00:00:00', 'Y', 'y@example.com', 'Sparse',
                    'quokka appears just once among many unrelated filler words here',
                    'neutral', 'low', 'english', 'INBOX', 'unrelated body text', 'conv-sparse')
            """
        )
        sample_db.commit()

        results = query_by_keyword(sample_db, "quokka")
        ids = [r["email_id"] for r in results]
        dense_id = sample_db.execute("SELECT id FROM emails WHERE message_id = 9001").fetchone()[0]
        sparse_id = sample_db.execute("SELECT id FROM emails WHERE message_id = 9002").fetchone()[0]
        assert dense_id in ids and sparse_id in ids
        assert ids.index(dense_id) < ids.index(sparse_id), (
            "denser BM25 match must outrank the newer, sparser match"
        )


class TestQueryByDateRange:
    """Test querying by date range."""

    def test_query_by_date_range_single_day(self, sample_db):
        """Test querying emails from a single day."""
        results = query_by_date_range(sample_db, "2026-03-17", "2026-03-17")

        assert len(results) == 2  # 2 emails on 2026-03-17

    def test_query_by_date_range_multiple_days(self, sample_db):
        """Test querying emails across multiple days."""
        results = query_by_date_range(sample_db, "2026-03-15", "2026-03-17")

        assert len(results) == 4  # 4 emails in this range

    def test_query_by_date_range_with_limit(self, sample_db):
        """Test limit parameter works."""
        results = query_by_date_range(sample_db, "2026-03-15", "2026-03-18", limit=2)

        assert len(results) == 2

    def test_query_by_date_range_no_results(self, sample_db):
        """Test query returns empty list when no emails in range."""
        results = query_by_date_range(sample_db, "2026-01-01", "2026-01-31")

        assert results == []

    def test_query_by_date_range_includes_sender(self, sample_db):
        """Test results include sender name."""
        results = query_by_date_range(sample_db, "2026-03-15", "2026-03-18")

        assert all("sender" in r for r in results)
        assert all(r["sender"] in ["Alice", "Bob", "Charlie"] for r in results)


class TestQueryDecisions:
    """Test querying decisions."""

    def test_query_decisions_all(self, sample_db):
        """Test querying all decisions."""
        results = query_decisions(sample_db)

        assert len(results) == 2
        assert any("budget" in r["decision"].lower() for r in results)
        assert any("vendor" in r["decision"].lower() for r in results)

    def test_query_decisions_by_person(self, sample_db):
        """Test filtering decisions by person."""
        results = query_decisions(sample_db, person="Charlie")

        assert len(results) == 1
        assert results[0]["decided_by"] == "Charlie"

    def test_query_decisions_by_topic(self, sample_db):
        """Test filtering decisions by topic."""
        results = query_decisions(sample_db, topic="Budget")

        assert len(results) == 1
        assert "budget" in results[0]["decision"].lower()

    def test_query_decisions_includes_email_subject(self, sample_db):
        """Test results include email subject."""
        results = query_decisions(sample_db)

        assert all("email_subject" in r for r in results)

    def test_query_decisions_no_results(self, sample_db):
        """Test query returns empty list when no matches."""
        results = query_decisions(sample_db, person="NonexistentPerson")

        assert results == []


class TestQueryActionItems:
    """Test querying action items."""

    def test_query_action_items_all_open(self, sample_db):
        """Test querying all open action items (default)."""
        results = query_action_items(sample_db)

        assert len(results) == 2  # 2 open action items
        assert all(r["status"] == "open" for r in results)

    def test_query_action_items_by_owner(self, sample_db):
        """Test filtering by owner."""
        results = query_action_items(sample_db, owner="Dave")

        assert len(results) == 1
        assert results[0]["owner"] == "Dave"
        assert results[0]["task"] == "Review proposal"

    def test_query_action_items_by_status(self, sample_db):
        """Test filtering by status."""
        results = query_action_items(sample_db, status="completed")

        assert len(results) == 1
        assert results[0]["status"] == "completed"

    def test_query_action_items_owner_and_status(self, sample_db):
        """Test filtering by both owner and status."""
        results = query_action_items(sample_db, owner="Alice", status="completed")

        assert len(results) == 1
        assert results[0]["owner"] == "Alice"
        assert results[0]["status"] == "completed"

    def test_query_action_items_includes_deadline(self, sample_db):
        """Test results include deadline."""
        results = query_action_items(sample_db)

        assert all("deadline" in r for r in results)
        assert any(r["deadline"] is not None for r in results)

    def test_query_action_items_no_results(self, sample_db):
        """Test query returns empty list when no matches."""
        results = query_action_items(sample_db, owner="NonexistentPerson")

        assert results == []


class TestQueryCombined:
    """Test combined query with multiple filters."""

    def test_query_combined_person_only(self, sample_db):
        """Test combined query with person filter only."""
        results = query_combined(sample_db, person="Alice")

        assert len(results) >= 2

    def test_query_combined_topic_only(self, sample_db):
        """Test combined query with topic filter only."""
        results = query_combined(sample_db, topic="Budget")

        assert len(results) >= 1

    def test_query_combined_person_and_topic(self, sample_db):
        """Test combined query with person and topic filters."""
        results = query_combined(sample_db, person="Bob", topic="Budget")

        assert len(results) >= 1
        assert any("budget" in r["subject"].lower() for r in results)

    def test_query_combined_date_range(self, sample_db):
        """Test combined query with date range."""
        results = query_combined(sample_db, start_date="2026-03-17", end_date="2026-03-17")

        assert len(results) == 2

    def test_query_combined_keyword(self, sample_db):
        """Test combined query with keyword."""
        results = query_combined(sample_db, keyword="migration")

        assert len(results) >= 1

    def test_query_combined_all_filters(self, sample_db):
        """Test combined query with all filters."""
        results = query_combined(
            sample_db,
            person="Alice",
            topic="Cards",
            keyword="migration",
            start_date="2026-03-15",
            end_date="2026-03-16",
        )

        assert len(results) >= 1
        assert results[0]["subject"] == "Cards migration update"

    def test_query_combined_no_filters_raises_error(self, sample_db):
        """Test that combined query without filters raises ValueError."""
        with pytest.raises(ValueError, match="At least one filter must be provided"):
            query_combined(sample_db)

    def test_query_combined_no_results(self, sample_db):
        """Test query returns empty list when no matches."""
        results = query_combined(sample_db, person="Alice", topic="NonexistentTopic")

        assert results == []

    def test_query_combined_includes_topics(self, sample_db):
        """Test results include topics."""
        results = query_combined(sample_db, person="Alice")

        assert all("topics" in r for r in results)


class TestGetStats:
    """Test database statistics."""

    def test_get_stats_counts(self, sample_db):
        """Test statistics include correct counts."""
        stats = get_stats(sample_db)

        assert stats["total_emails"] == 5
        assert stats["total_topics"] == 4
        assert stats["total_people"] == 4
        assert stats["total_decisions"] == 2
        assert stats["total_action_items"] == 3

    def test_get_stats_date_range(self, sample_db):
        """Test statistics include date range."""
        stats = get_stats(sample_db)

        assert stats["earliest_email"] == "2026-03-15T10:00:00"
        assert stats["latest_email"] == "2026-03-18T15:00:00"

    def test_get_stats_empty_database(self):
        """Test statistics on empty database."""
        conn = create_database(":memory:")
        stats = get_stats(conn)

        assert stats["total_emails"] == 0
        assert stats["total_topics"] == 0
        assert stats["total_people"] == 0
        assert stats["total_decisions"] == 0
        assert stats["total_action_items"] == 0
        assert stats["earliest_email"] is None
        assert stats["latest_email"] is None

        conn.close()


class TestQueryResultStructure:
    """Test that query results have the expected structure."""

    def test_query_by_person_result_keys(self, sample_db):
        """Test query_by_person returns expected keys."""
        results = query_by_person(sample_db, "Alice")

        assert len(results) > 0
        for result in results:
            assert "email_id" in result
            assert "date" in result
            assert "subject" in result
            assert "summary" in result
            assert "person_role" in result
            assert "sentiment" in result

    def test_query_by_topic_result_keys(self, sample_db):
        """Test query_by_topic returns expected keys."""
        results = query_by_topic(sample_db, "Budget")

        assert len(results) > 0
        for result in results:
            assert "email_id" in result
            assert "date" in result
            assert "subject" in result
            assert "summary" in result
            assert "topic" in result

    def test_query_by_keyword_result_keys(self, sample_db):
        """Test query_by_keyword returns expected keys."""
        results = query_by_keyword(sample_db, "migration")

        assert len(results) > 0
        for result in results:
            assert "email_id" in result
            assert "date" in result
            assert "subject" in result
            assert "summary" in result
            assert "snippet" in result
            assert "source" in result
            assert result["source"] in ["summary", "content", "key_fact"]

    def test_query_decisions_result_keys(self, sample_db):
        """Test query_decisions returns expected keys."""
        results = query_decisions(sample_db)

        assert len(results) > 0
        for result in results:
            assert "decision_id" in result
            assert "decision" in result
            assert "decided_by" in result
            assert "date" in result
            assert "email_subject" in result
            assert "topics" in result

    def test_query_action_items_result_keys(self, sample_db):
        """Test query_action_items returns expected keys."""
        results = query_action_items(sample_db)

        assert len(results) > 0
        for result in results:
            assert "action_id" in result
            assert "task" in result
            assert "owner" in result
            assert "deadline" in result
            assert "status" in result
            assert "email_subject" in result
            assert "date" in result


class TestMeetingPrep:
    """Test meeting prep dossier generation."""

    def test_meeting_prep_basic(self, sample_db):
        """Test basic meeting prep returns attendee dossier."""
        result = meeting_prep(sample_db, ["Alice"])

        assert len(result["attendees"]) == 1
        dossier = result["attendees"][0]
        assert dossier["name"] == "Alice"
        assert len(dossier["emails"]) > 0
        assert isinstance(dossier["sentiment_summary"], dict)
        assert isinstance(dossier["topics"], list)

    def test_meeting_prep_multiple_people(self, sample_db):
        """Test meeting prep with multiple attendees."""
        result = meeting_prep(sample_db, ["Alice", "Bob"])

        assert len(result["attendees"]) == 2
        names = [d["name"] for d in result["attendees"]]
        assert "Alice" in names
        assert "Bob" in names

    def test_meeting_prep_with_topic(self, sample_db):
        """Test meeting prep with topic context."""
        result = meeting_prep(sample_db, ["Alice"], topic="Budget")

        assert result["topic_context"] is not None
        tc = result["topic_context"]
        assert tc["topic"] == "Budget"
        assert isinstance(tc["decisions"], list)
        assert isinstance(tc["key_facts"], list)
        assert isinstance(tc["action_items"], list)

    def test_meeting_prep_by_email(self, sample_db):
        """Test meeting prep lookup by email address."""
        result = meeting_prep(sample_db, ["alice@example.com"])

        assert len(result["attendees"]) == 1
        assert len(result["attendees"][0]["emails"]) > 0

    def test_meeting_prep_nonexistent_person(self, sample_db):
        """Test meeting prep with unknown person returns empty dossier."""
        result = meeting_prep(sample_db, ["NonexistentPerson"])

        assert len(result["attendees"]) == 1
        dossier = result["attendees"][0]
        assert dossier["emails"] == []
        assert dossier["decisions"] == []

    def test_meeting_prep_includes_decisions(self, sample_db):
        """Test dossier includes decisions for the person."""
        result = meeting_prep(sample_db, ["Charlie"])

        dossier = result["attendees"][0]
        # Charlie was involved in the budget decision email
        assert isinstance(dossier["decisions"], list)

    def test_meeting_prep_includes_open_actions(self, sample_db):
        """Test dossier includes open action items."""
        result = meeting_prep(sample_db, ["Dave"])

        dossier = result["attendees"][0]
        assert isinstance(dossier["open_actions"], list)


class TestQueryThread:
    """Test querying conversation threads."""

    def test_query_thread_returns_conversation(self):
        """Test query_thread returns full conversation grouped by subject."""
        conn = create_database(":memory:")

        conv_id = subject_to_conversation_id("Project update")
        conn.execute(
            """INSERT INTO emails (message_id, date_received, sender_name, sender_address,
                                   subject, summary, sentiment, urgency, language, mailbox_name, content,
                                   conversation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                100,
                "2026-03-15T10:00:00",
                "Alice",
                "alice@example.com",
                "Project update",
                "Initial update",
                "informational",
                "medium",
                "english",
                "INBOX",
                "content1",
                conv_id,
            ),
        )
        conn.execute(
            """INSERT INTO emails (message_id, date_received, sender_name, sender_address,
                                   subject, summary, sentiment, urgency, language, mailbox_name, content,
                                   conversation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                101,
                "2026-03-15T14:00:00",
                "Bob",
                "bob@example.com",
                "Re: Project update",
                "Reply to update",
                "informational",
                "medium",
                "english",
                "INBOX",
                "content2",
                conv_id,
            ),
        )
        conn.execute(
            """INSERT INTO emails (message_id, date_received, sender_name, sender_address,
                                   subject, summary, sentiment, urgency, language, mailbox_name, content,
                                   conversation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                102,
                "2026-03-16T09:00:00",
                "Alice",
                "alice@example.com",
                "Re: Re: Project update",
                "Follow up",
                "informational",
                "medium",
                "english",
                "INBOX",
                "content3",
                conv_id,
            ),
        )
        conn.commit()

        # Query thread from any email in the conversation
        email_id = conn.execute("SELECT id FROM emails WHERE message_id = 100").fetchone()[0]
        results = query_thread(conn, email_id)

        assert len(results) == 3
        assert results[0]["sender_name"] == "Alice"
        assert results[1]["sender_name"] == "Bob"
        assert results[2]["sender_name"] == "Alice"
        # Verify chronological order
        assert results[0]["date"] < results[1]["date"] < results[2]["date"]
        conn.close()

    def test_query_thread_single_email(self, sample_db):
        """Test query_thread with a single email (no conversation partners)."""
        # Email 1 has unique subject so it's alone in its thread
        results = query_thread(sample_db, 1)

        assert len(results) == 1
        assert results[0]["email_id"] == 1

    def test_query_thread_nonexistent_email(self, sample_db):
        """Test query_thread with non-existent email ID."""
        results = query_thread(sample_db, 9999)

        assert results == []

    def test_conversation_id_groups_by_normalized_subject(self):
        """Test that conversation_id correctly groups emails with Re:/Fwd: prefixes."""
        assert normalize_subject("Project update") == "project update"
        assert normalize_subject("Re: Project update") == "project update"
        assert normalize_subject("Fwd: Project update") == "project update"
        assert normalize_subject("RE: FW: Project update") == "project update"
        assert normalize_subject("Απ: Project update") == "project update"
        assert normalize_subject("Πρ: Project update") == "project update"

        # All should produce the same conversation_id
        base = subject_to_conversation_id("Project update")
        assert subject_to_conversation_id("Re: Project update") == base
        assert subject_to_conversation_id("Fwd: Project update") == base
        assert subject_to_conversation_id("RE: FW: Project update") == base
        assert subject_to_conversation_id("Απ: Project update") == base

    def test_query_thread_result_keys(self):
        """Test query_thread returns expected keys."""
        conn = create_database(":memory:")
        conv_id = subject_to_conversation_id("Test subject")
        conn.execute(
            """INSERT INTO emails (message_id, date_received, sender_name, sender_address,
                                   subject, summary, sentiment, urgency, language, mailbox_name, content,
                                   conversation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                200,
                "2026-03-15T10:00:00",
                "Alice",
                "alice@example.com",
                "Test subject",
                "Summary",
                "neutral",
                "low",
                "english",
                "INBOX",
                "content",
                conv_id,
            ),
        )
        conn.commit()

        email_id = conn.execute("SELECT id FROM emails WHERE message_id = 200").fetchone()[0]
        results = query_thread(conn, email_id)

        assert len(results) == 1
        for key in (
            "email_id",
            "date",
            "subject",
            "summary",
            "sender_name",
            "sender_address",
            "sentiment",
        ):
            assert key in results[0]
        conn.close()


@patch("src.store.query.USER_EMAIL_PATTERN", "papadopoulos")
class TestFindStaleThreads:
    """Test stale thread detection."""

    def _make_stale_db(self):
        """Create a DB with threads where papadopoulos is last sender."""
        conn = create_database(":memory:")
        conv_id_stale = subject_to_conversation_id("Stale topic")
        conv_id_replied = subject_to_conversation_id("Replied topic")
        conv_id_fresh = subject_to_conversation_id("Fresh topic")

        emails = [
            # Stale thread: papadopoulos sent last, 10 days ago
            (
                100,
                "2026-03-05T10:00:00",
                "Other",
                "other@example.com",
                "Stale topic",
                "Initial",
                "neutral",
                "low",
                "en",
                "INBOX",
                "c",
                conv_id_stale,
            ),
            (
                101,
                "2026-03-06T10:00:00",
                "Nikos Papadopoulos",
                "papadopoulos@example.com",
                "Re: Stale topic",
                "Reply",
                "neutral",
                "low",
                "en",
                "INBOX",
                "c",
                conv_id_stale,
            ),
            # Replied thread: papadopoulos sent, then got a reply
            (
                200,
                "2026-03-05T10:00:00",
                "Nikos Papadopoulos",
                "papadopoulos@example.com",
                "Replied topic",
                "Sent",
                "neutral",
                "low",
                "en",
                "INBOX",
                "c",
                conv_id_replied,
            ),
            (
                201,
                "2026-03-07T10:00:00",
                "Other",
                "other@example.com",
                "Re: Replied topic",
                "Got reply",
                "neutral",
                "low",
                "en",
                "INBOX",
                "c",
                conv_id_replied,
            ),
            # Fresh thread: papadopoulos sent recently (today)
            (
                300,
                "2099-01-01T10:00:00",
                "Nikos Papadopoulos",
                "papadopoulos@example.com",
                "Fresh topic",
                "Recent",
                "neutral",
                "low",
                "en",
                "INBOX",
                "c",
                conv_id_fresh,
            ),
        ]
        for e in emails:
            conn.execute(
                """INSERT INTO emails (message_id, date_received, sender_name, sender_address,
                   subject, summary, sentiment, urgency, language, mailbox_name, content,
                   conversation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                e,
            )
        conn.commit()
        return conn

    def test_finds_stale_thread(self):
        conn = self._make_stale_db()
        results = find_stale_threads(conn, days=5)
        subjects = [r["subject"] for r in results]
        assert any("Stale" in s for s in subjects)
        conn.close()

    def test_excludes_replied_thread(self):
        conn = self._make_stale_db()
        results = find_stale_threads(conn, days=5)
        subjects = [r["subject"] for r in results]
        # The replied thread's last sender is 'other@example.com', not papadopoulos
        assert not any("Replied" in s for s in subjects)
        conn.close()

    def test_excludes_fresh_thread(self):
        conn = self._make_stale_db()
        results = find_stale_threads(conn, days=5)
        subjects = [r["subject"] for r in results]
        assert not any("Fresh" in s for s in subjects)
        conn.close()

    def test_days_parameter(self):
        conn = self._make_stale_db()
        # With days=9999, nothing is stale
        results = find_stale_threads(conn, days=9999)
        assert results == []
        conn.close()

    def test_result_keys(self):
        conn = self._make_stale_db()
        results = find_stale_threads(conn, days=5)
        if results:
            for key in (
                "conversation_id",
                "subject",
                "date_received",
                "sender_address",
                "days_waiting",
            ):
                assert key in results[0]
        conn.close()

    def test_empty_db(self):
        conn = create_database(":memory:")
        results = find_stale_threads(conn, days=5)
        assert results == []
        conn.close()


class TestFindOverdueActions:
    """Test overdue action item detection."""

    def test_finds_overdue_action(self, sample_db):
        # The sample_db has action items with deadline '2026-03-20' and '2026-03-22'
        # These are in the past relative to 'now' (test runs after 2026-03-20)
        # To make a reliable test, insert an action with a definitely-past deadline
        sample_db.execute(
            "INSERT INTO action_items (email_id, task, owner, deadline, status) "
            "VALUES (1, 'Overdue task', 'TestOwner', '2020-01-01', 'open')"
        )
        sample_db.commit()
        results = find_overdue_actions(sample_db)
        tasks = [r["task"] for r in results]
        assert "Overdue task" in tasks

    def test_excludes_completed(self, sample_db):
        sample_db.execute(
            "INSERT INTO action_items (email_id, task, owner, deadline, status) "
            "VALUES (1, 'Done old task', 'TestOwner', '2020-01-01', 'completed')"
        )
        sample_db.commit()
        results = find_overdue_actions(sample_db)
        tasks = [r["task"] for r in results]
        assert "Done old task" not in tasks

    def test_excludes_no_deadline(self, sample_db):
        sample_db.execute(
            "INSERT INTO action_items (email_id, task, owner, deadline, status) "
            "VALUES (1, 'No deadline task', 'TestOwner', NULL, 'open')"
        )
        sample_db.commit()
        results = find_overdue_actions(sample_db)
        tasks = [r["task"] for r in results]
        assert "No deadline task" not in tasks

    def test_excludes_future_deadline(self, sample_db):
        sample_db.execute(
            "INSERT INTO action_items (email_id, task, owner, deadline, status) "
            "VALUES (1, 'Future task', 'TestOwner', '2099-12-31', 'open')"
        )
        sample_db.commit()
        results = find_overdue_actions(sample_db)
        tasks = [r["task"] for r in results]
        assert "Future task" not in tasks

    def test_result_keys(self, sample_db):
        sample_db.execute(
            "INSERT INTO action_items (email_id, task, owner, deadline, status) "
            "VALUES (1, 'Key check task', 'TestOwner', '2020-01-01', 'open')"
        )
        sample_db.commit()
        results = find_overdue_actions(sample_db)
        assert len(results) > 0
        for key in (
            "action_id",
            "task",
            "owner",
            "deadline",
            "email_subject",
            "date",
            "days_overdue",
        ):
            assert key in results[0]

    def test_empty_db(self):
        conn = create_database(":memory:")
        results = find_overdue_actions(conn)
        assert results == []
        conn.close()


class TestImportPeople:
    """Test canonical people import logic."""

    def test_load_canonical_json(self):
        """Test that canonical_people.json is valid JSON."""
        import json
        from pathlib import Path

        json_path = Path(__file__).parent.parent / "data" / "canonical_people.json"
        if not json_path.exists():
            pytest.skip("canonical_people.json not available in CI")
        with open(json_path) as f:
            data = json.load(f)
        assert isinstance(data, list)
        assert len(data) > 0
        for entry in data:
            assert "canonical_name" in entry
            assert "aliases" in entry
            assert isinstance(entry["aliases"], list)

    def test_match_by_email(self):
        """Test finding a person by email and updating role/dept."""
        conn = create_database(":memory:")
        conn.execute(
            "INSERT INTO people (name, email) VALUES ('effie b', 'chen.bella@example.com')"
        )
        conn.commit()

        row = conn.execute(
            "SELECT id FROM people WHERE LOWER(email) = LOWER(?)",
            ("chen.bella@example.com",),
        ).fetchone()
        assert row is not None

        conn.execute(
            "UPDATE people SET name = ?, role = ?, department = ? WHERE id = ?",
            (
                "Bella Chen",
                "Director, Digital Banking",
                "Digital Banking Division",
                row[0],
            ),
        )
        conn.commit()

        updated = conn.execute(
            "SELECT name, role, department FROM people WHERE id = ?", (row[0],)
        ).fetchone()
        assert updated[0] == "Bella Chen"
        assert updated[1] == "Director, Digital Banking"
        assert updated[2] == "Digital Banking Division"
        conn.close()

    def test_match_by_alias_name(self):
        """Test finding a person by normalized alias name."""
        from src.store.dedup_people import normalize_name

        conn = create_database(":memory:")
        conn.execute("INSERT INTO people (name, email) VALUES ('ΒΟΛΙΩΤΗ ΕΛΕΥΘΕΡΙΑ', NULL)")
        conn.commit()

        alias_norm = normalize_name("ΒΟΛΙΩΤΗ ΕΛΕΥΘΕΡΙΑ")
        rows = conn.execute("SELECT id, name FROM people").fetchall()
        match = None
        for r in rows:
            if normalize_name(r[1]) == alias_norm:
                match = r[0]
                break
        assert match is not None
        conn.close()

    def test_update_role_department(self):
        """Test updating role and department on matched person."""
        conn = create_database(":memory:")
        conn.execute(
            "INSERT INTO people (name, email, role, department) VALUES ('Test Person', 'test@example.com', NULL, NULL)"
        )
        conn.commit()

        conn.execute(
            "UPDATE people SET role = ?, department = ? WHERE LOWER(email) = LOWER(?)",
            ("Director", "Test Dept", "test@example.com"),
        )
        conn.commit()

        row = conn.execute(
            "SELECT role, department FROM people WHERE LOWER(email) = LOWER(?)",
            ("test@example.com",),
        ).fetchone()
        assert row[0] == "Director"
        assert row[1] == "Test Dept"
        conn.close()


def test_get_stats_separates_mail_from_news_and_documents():
    """total_emails must mean real mail, not every row in the `emails` table.

    News articles (mailbox_name='News') and ingested standalone documents
    (message_id <= 0) share the `emails` table. Counting them as mail made the
    stats tool report 74,424 against the health check's 65,363 for the same DB —
    a 9,061 overstatement with no way for a caller to see the split.
    """
    conn = create_database(":memory:")
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, mailbox_name) "
        "VALUES (1, '2026-01-01', 'Real mail', 'Inbox')"
    )
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, mailbox_name) "
        "VALUES (2, '2026-01-02', 'Also real', NULL)"
    )
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, mailbox_name) "
        "VALUES (3, '2026-01-03', 'A news article', 'News')"
    )
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, mailbox_name) "
        "VALUES (0, '2026-01-04', 'A standalone document', 'External')"
    )
    conn.commit()

    stats = get_stats(conn)

    assert stats["total_emails"] == 2
    assert stats["total_news_articles"] == 1
    assert stats["total_documents"] == 1
