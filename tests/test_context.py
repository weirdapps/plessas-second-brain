"""Tests for the context retrieval API.

Uses in-memory SQLite databases with sample data to test all context functions.
"""

import pytest

from src.store.context import (
    get_conversation_context,
    get_person_context,
    get_recent_decisions,
    get_topic_context,
)
from src.store.normalizer import find_or_create_person, find_or_create_topic
from src.store.schema import create_database


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
        ),
    ]

    for email in emails:
        conn.execute(
            """
            INSERT INTO emails (message_id, date_received, sender_name, sender_address,
                               subject, summary, sentiment, urgency, language, mailbox_name, content)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    conn.execute(
        "INSERT INTO key_facts (email_id, fact) VALUES (1, 'Migration timeline extended to Q2')"
    )
    conn.execute("INSERT INTO key_facts (email_id, fact) VALUES (2, 'Budget increased by 15%')")

    conn.commit()
    return conn


class TestGetPersonContext:
    """Test get_person_context function."""

    def test_returns_expected_structure(self, sample_db):
        result = get_person_context(sample_db, "Alice")

        assert "person" in result
        assert "email_count" in result
        assert "recent_emails" in result
        assert "topics" in result
        assert "sentiment_distribution" in result
        assert "decisions" in result
        assert "open_actions" in result
        assert "communication_pattern" in result

    def test_person_info(self, sample_db):
        result = get_person_context(sample_db, "Alice")

        assert result["person"] is not None
        assert result["person"]["name"] == "Alice"
        assert result["person"]["email"] == "alice@example.com"

    def test_search_by_email(self, sample_db):
        result = get_person_context(sample_db, "alice@example.com")

        assert result["person"] is not None
        assert result["person"]["name"] == "Alice"

    def test_email_count(self, sample_db):
        result = get_person_context(sample_db, "Alice")

        assert result["email_count"] == 2  # Alice is sender on 2 emails

    def test_recent_emails(self, sample_db):
        result = get_person_context(sample_db, "Alice")

        assert len(result["recent_emails"]) == 2
        assert all("date" in e for e in result["recent_emails"])
        assert all("subject" in e for e in result["recent_emails"])
        assert all("summary" in e for e in result["recent_emails"])
        assert all("role_in_email" in e for e in result["recent_emails"])

    def test_topics(self, sample_db):
        result = get_person_context(sample_db, "Alice")

        assert len(result["topics"]) >= 1
        topic_names = [t["topic"] for t in result["topics"]]
        assert any("Cards" in t or "Digital" in t for t in topic_names)

    def test_sentiment_distribution(self, sample_db):
        result = get_person_context(sample_db, "Alice")

        assert isinstance(result["sentiment_distribution"], dict)
        assert "informational" in result["sentiment_distribution"]

    def test_decisions(self, sample_db):
        # Alice decided 'Proceed with vendor A' (email 3 links Alice via email_people? No.)
        # Alice is sender on email 3? No, Charlie is. But Alice decided on email 3.
        # Actually Alice is NOT linked to email 3 via email_people. Charlie is.
        # Alice is linked to emails 1 and 4.
        # Decision on email 2 links to Bob and Charlie.
        # So Alice has no decisions via email_people.
        result = get_person_context(sample_db, "Charlie")

        # Charlie is on email 2 (decision-maker) and email 3 (sender)
        assert len(result["decisions"]) >= 1

    def test_open_actions(self, sample_db):
        result = get_person_context(sample_db, "Dave")

        # Dave is recipient on email 5 which has open action items
        assert len(result["open_actions"]) >= 1
        assert all(a["status"] == "open" for a in result["open_actions"])

    def test_communication_pattern(self, sample_db):
        result = get_person_context(sample_db, "Alice")

        assert "first_email_date" in result["communication_pattern"]
        assert "last_email_date" in result["communication_pattern"]
        assert "avg_emails_per_week" in result["communication_pattern"]

    def test_nonexistent_person(self, sample_db):
        result = get_person_context(sample_db, "NonexistentPerson")

        assert result["person"] is None
        assert result["email_count"] == 0
        assert result["recent_emails"] == []
        assert result["topics"] == []
        assert result["sentiment_distribution"] == {}
        assert result["decisions"] == []
        assert result["open_actions"] == []
        assert result["communication_pattern"] == {}

    def test_date_filtering(self, sample_db):
        # With days=0, cutoff is now, so no emails from 2026 should match
        result = get_person_context(sample_db, "Alice", days=0)

        # The cutoff is "now" which is 2026-03-20 or later, so 2026-03-15 emails won't match
        # But days=0 means timedelta(days=0) which is now, so emails from the past won't match
        assert result["email_count"] == 0
        assert result["recent_emails"] == []


class TestGetTopicContext:
    """Test get_topic_context function."""

    def test_returns_expected_structure(self, sample_db):
        result = get_topic_context(sample_db, "Budget")

        assert "topic" in result
        assert "email_count" in result
        assert "recent_emails" in result
        assert "key_people" in result
        assert "decisions" in result
        assert "open_actions" in result
        assert "key_facts" in result

    def test_topic_info(self, sample_db):
        result = get_topic_context(sample_db, "Budget")

        assert result["topic"] is not None
        assert result["topic"]["name"] == "budget"
        assert result["topic"]["display_name"] == "Budget"

    def test_email_count(self, sample_db):
        result = get_topic_context(sample_db, "Budget")

        assert result["email_count"] == 1

    def test_recent_emails(self, sample_db):
        result = get_topic_context(sample_db, "Budget")

        assert len(result["recent_emails"]) == 1
        assert result["recent_emails"][0]["subject"] == "Budget approval needed"
        assert "sender" in result["recent_emails"][0]

    def test_key_people(self, sample_db):
        result = get_topic_context(sample_db, "Budget")

        assert len(result["key_people"]) >= 1
        names = [p["name"] for p in result["key_people"]]
        assert "Bob" in names or "Charlie" in names

    def test_decisions(self, sample_db):
        result = get_topic_context(sample_db, "Budget")

        assert len(result["decisions"]) == 1
        assert "budget" in result["decisions"][0]["decision"].lower()

    def test_key_facts(self, sample_db):
        result = get_topic_context(sample_db, "Budget")

        assert len(result["key_facts"]) == 1
        assert "Budget increased by 15%" in result["key_facts"][0]["fact"]

    def test_open_actions(self, sample_db):
        result = get_topic_context(sample_db, "Strategy")

        # Email 5 is tagged with Strategy and has open actions
        assert len(result["open_actions"]) >= 1

    def test_nonexistent_topic(self, sample_db):
        result = get_topic_context(sample_db, "NonexistentTopic")

        assert result["topic"] is None
        assert result["email_count"] == 0
        assert result["recent_emails"] == []
        assert result["key_people"] == []
        assert result["decisions"] == []
        assert result["open_actions"] == []
        assert result["key_facts"] == []

    def test_date_filtering(self, sample_db):
        result = get_topic_context(sample_db, "Budget", days=0)

        assert result["email_count"] == 0
        assert result["recent_emails"] == []


class TestGetConversationContext:
    """Test get_conversation_context function."""

    def test_returns_expected_structure(self, sample_db):
        result = get_conversation_context(sample_db, 1)

        assert "email" in result
        assert "thread" in result
        assert "participants" in result
        assert "decisions" in result
        assert "action_items" in result

    def test_single_email(self, sample_db):
        result = get_conversation_context(sample_db, 1)

        assert result["email"] is not None
        assert result["email"]["subject"] == "Cards migration update"
        # No conversation_id column, so thread is just the single email
        assert len(result["thread"]) == 1

    def test_participants(self, sample_db):
        result = get_conversation_context(sample_db, 1)

        names = [p["name"] for p in result["participants"]]
        assert "Alice" in names
        assert "Bob" in names

    def test_decisions_in_thread(self, sample_db):
        result = get_conversation_context(sample_db, 2)

        assert len(result["decisions"]) == 1
        assert "budget" in result["decisions"][0]["decision"].lower()

    def test_action_items_in_thread(self, sample_db):
        result = get_conversation_context(sample_db, 5)

        assert len(result["action_items"]) == 2

    def test_nonexistent_email(self, sample_db):
        result = get_conversation_context(sample_db, 999)

        assert result["email"] is None
        assert result["thread"] == []
        assert result["participants"] == []
        assert result["decisions"] == []
        assert result["action_items"] == []


class TestGetRecentDecisions:
    """Test get_recent_decisions function."""

    def test_returns_recent_decisions(self, sample_db):
        results = get_recent_decisions(sample_db, days=365)

        assert len(results) == 2

    def test_decision_structure(self, sample_db):
        results = get_recent_decisions(sample_db, days=365)

        for r in results:
            assert "decision" in r
            assert "decided_by" in r
            assert "date" in r
            assert "email_subject" in r
            assert "email_date" in r
            assert "topics" in r

    def test_includes_topics(self, sample_db):
        results = get_recent_decisions(sample_db, days=365)

        # At least one decision should have a topic
        assert any(r["topics"] is not None for r in results)

    def test_ordered_by_date_desc(self, sample_db):
        results = get_recent_decisions(sample_db, days=365)

        dates = [r["date"] for r in results]
        assert dates == sorted(dates, reverse=True)

    def test_limit(self, sample_db):
        results = get_recent_decisions(sample_db, days=365, limit=1)

        assert len(results) == 1

    def test_date_filtering(self, sample_db):
        results = get_recent_decisions(sample_db, days=0)

        assert len(results) == 0
