"""Tests for the unified recall() function.

recall() is the single 'ask once, get everything' entry point.
It composes keyword search across emails (incl. attachments + standalone
docs), conversations, decisions, actions, inline images, plus auto-detected
person and topic context.
"""

import pytest

from src.store.normalizer import find_or_create_person, find_or_create_topic
from src.store.recall import recall
from src.store.schema import create_database, migrate_add_conversations

# A keyword guaranteed to be unique across the fixture so we can verify
# each kind surfaces independently.
UNIQUE_KW = "unicornz9"


@pytest.fixture
def recall_db():
    """In-memory DB seeded with one hit per discoverable kind for UNIQUE_KW."""
    conn = create_database(":memory:")
    # create_database stamps schema_version=CURRENT, so run_migrations is a no-op.
    # Conversation tables only ship via the migration, so add them explicitly.
    migrate_add_conversations(conn)

    # --- Email with the keyword in summary ---
    conn.execute(
        """
        INSERT INTO emails (id, message_id, date_received, sender_name, sender_address,
                            subject, summary, sentiment, urgency, language, mailbox_name, content)
        VALUES (1, 1, '2026-04-01T10:00:00', 'Alice', 'alice@example.com',
                'Mail subject', 'Email summary mentioning unicornz9', 'informational',
                'low', 'english', 'INBOX', 'body')
        """
    )

    # --- Standalone document (negative message_id) with the keyword ---
    conn.execute(
        """
        INSERT INTO emails (id, message_id, date_received, sender_name, sender_address,
                            subject, summary, sentiment, urgency, language, mailbox_name, content)
        VALUES (2, -100, '2026-04-02T10:00:00', 'documents', 'external@documents.local',
                '[Document] strategic-deck.pdf', 'Standalone doc covering unicornz9 analysis',
                'informational', 'low', 'english', 'documents', 'doc body')
        """
    )

    # --- Email with an attachment that contains the keyword in extracted text ---
    conn.execute(
        """
        INSERT INTO emails (id, message_id, date_received, sender_name, sender_address,
                            subject, summary, sentiment, urgency, language, mailbox_name, content)
        VALUES (3, 3, '2026-04-03T10:00:00', 'Bob', 'bob@example.com',
                'Attachment carrier', 'Carrier email with no keyword',
                'informational', 'low', 'english', 'INBOX', 'no kw here')
        """
    )
    cur = conn.execute(
        """
        INSERT INTO attachments (email_id, message_id, filename, mime_type,
                                 file_size, file_path, is_inline, exported_at)
        VALUES (3, 3, 'deck.pdf', 'application/pdf', 1024, '/tmp/deck.pdf', 0,
                '2026-04-03T10:05:00')
        """
    )
    aid = cur.lastrowid
    conn.execute(
        """
        INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,
                                        extraction_status, extracted_at, summary, language,
                                        llm_status, llm_extracted_at)
        VALUES (?, 'Strategic deck mentioning unicornz9 in chart 3', 'pdfplumber',
                'extracted', '2026-04-03T10:10:00', 'Deck summary', 'english',
                'extracted', '2026-04-03T10:15:00')
        """,
        (aid,),
    )

    # --- Conversation with the keyword in a turn ---
    conn.execute(
        """
        INSERT INTO conversations (id, session_id, started_at, workspace, project_name,
                                   turn_count, summary, topics_summary, created_at)
        VALUES (1, 'sess-abc', '2026-04-04T10:00:00', '/tmp/work', 'work',
                2, 'Conversation summary', 'topics', '2026-04-04T10:00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO conversation_turns (conversation_id, turn_index, timestamp, speaker,
                                        content, summary)
        VALUES (1, 0, '2026-04-04T10:01:00', 'user',
                'Discussing unicornz9 implications', 'turn summary')
        """
    )

    # --- Decision with the keyword ---
    conn.execute(
        """
        INSERT INTO decisions (email_id, decision, decided_by, decision_date)
        VALUES (1, 'Adopt unicornz9 framework', 'Alice', '2026-04-01')
        """
    )

    # --- Action item with the keyword ---
    conn.execute(
        """
        INSERT INTO action_items (email_id, task, owner, deadline, status)
        VALUES (1, 'Review unicornz9 proposal', 'Bob', '2026-04-15', 'open')
        """
    )

    # --- Inline image with the keyword in vision_description ---
    conn.execute(
        """
        INSERT INTO inline_images (sha256, width, height, bytes, classification,
                                   classification_method, classified_at, vision_description)
        VALUES ('sha-test-1', 800, 600, 12345, 'content', 'vision-llm',
                '2026-04-05T10:00:00', 'Bar chart titled unicornz9 quarterly trend')
        """
    )

    # --- Person + topic for context auto-detection ---
    find_or_create_person(conn, "PolitiTest", "duarte@example.com")
    find_or_create_topic(conn, "StrategyTopic")
    # Link the person to email 1 so person_context has something to surface
    person_id = conn.execute("SELECT id FROM people WHERE name = 'PolitiTest'").fetchone()[0]
    conn.execute(
        "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (1, ?, 'sender')",
        (person_id,),
    )
    # Link topic to email 2
    topic_id = conn.execute("SELECT id FROM topics WHERE name = 'strategytopic'").fetchone()[0]
    conn.execute("INSERT INTO email_topics (email_id, topic_id) VALUES (2, ?)", (topic_id,))

    conn.commit()
    return conn


class TestRecallTextSearch:
    """recall(query) must surface text matches across every text-bearing kind."""

    def test_returns_categorized_dict_with_expected_keys(self, recall_db):
        result = recall(recall_db, UNIQUE_KW)
        assert "query" in result
        for kind in (
            "emails",
            "conversations",
            "decisions",
            "actions",
            "commitments",
            "inline_images",
            "person_context",
            "topic_context",
            "summary",
        ):
            assert kind in result, f"missing key: {kind}"

    def test_emails_includes_regular_mail(self, recall_db):
        result = recall(recall_db, UNIQUE_KW)
        assert any(r["email_id"] == 1 for r in result["emails"]), (
            "regular email with keyword in summary must surface"
        )

    def test_emails_includes_standalone_documents(self, recall_db):
        result = recall(recall_db, UNIQUE_KW)
        # Document has email.id=2, message_id=-100
        assert any(r["email_id"] == 2 for r in result["emails"]), (
            "standalone document (negative msg_id) must surface via emails kind"
        )

    def test_emails_includes_attachment_matches(self, recall_db):
        result = recall(recall_db, UNIQUE_KW)
        # Carrier email id=3, only matches because its attachment contains the kw
        assert any(r["email_id"] == 3 for r in result["emails"]), (
            "email whose only match is an attachment must surface"
        )

    def test_conversations_surface(self, recall_db):
        result = recall(recall_db, UNIQUE_KW)
        assert len(result["conversations"]) >= 1
        assert any("sess-abc" == c.get("session_id") for c in result["conversations"])

    def test_decisions_surface(self, recall_db):
        result = recall(recall_db, UNIQUE_KW)
        assert any(UNIQUE_KW in d["decision"].lower() for d in result["decisions"])

    def test_actions_surface(self, recall_db):
        result = recall(recall_db, UNIQUE_KW)
        assert any(UNIQUE_KW in a["task"].lower() for a in result["actions"])

    def test_inline_images_surface(self, recall_db):
        result = recall(recall_db, UNIQUE_KW)
        assert any(
            UNIQUE_KW in (img.get("vision_description") or "").lower()
            for img in result["inline_images"]
        )

    def test_summary_reports_total_hits_and_kinds(self, recall_db):
        result = recall(recall_db, UNIQUE_KW)
        assert result["summary"]["total_hits"] >= 6  # emails(3) + conv + dec + act + img
        assert "emails" in result["summary"]["kinds_with_results"]


class TestRecallContextAutoDetection:
    """recall must auto-pull person/topic context when query matches one."""

    def test_person_context_populated_for_matching_name(self, recall_db):
        result = recall(recall_db, "PolitiTest")
        assert result["person_context"] is not None
        assert result["person_context"].get("person") is not None
        assert result["person_context"]["person"]["name"] == "PolitiTest"

    def test_topic_context_populated_for_matching_topic(self, recall_db):
        result = recall(recall_db, "StrategyTopic")
        assert result["topic_context"] is not None
        assert result["topic_context"].get("topic") is not None

    def test_no_person_context_when_query_doesnt_match(self, recall_db):
        result = recall(recall_db, UNIQUE_KW)
        # UNIQUE_KW isn't a person name, so person_context should be None or empty
        pc = result["person_context"]
        assert pc is None or pc.get("person") is None


class TestRecallEdgeCases:
    """Defensive behavior: empty DB, no matches, special chars."""

    def test_no_matches_returns_empty_buckets_not_error(self, recall_db):
        result = recall(recall_db, "totallynonexistentterm9999")
        # Each bucket should be present and empty
        for kind in (
            "emails",
            "conversations",
            "decisions",
            "actions",
            "inline_images",
        ):
            assert result[kind] == [], f"{kind} should be empty list, got {result[kind]}"
        assert result["summary"]["total_hits"] == 0

    def test_fts5_special_chars_dont_crash(self, recall_db):
        # Should not raise — sanitization required
        for q in ['"quoted"', "foo OR bar", "action-items", "NEAR(a,b)"]:
            result = recall(recall_db, q)
            assert isinstance(result, dict)


class TestRecallHybridFusion:
    """recall() fuses keyword + injected semantic candidates via RRF."""

    def test_semantic_only_email_surfaces_and_is_tagged(self, recall_db):
        # An email with NO keyword match anywhere.
        recall_db.execute(
            "INSERT INTO emails (id, message_id, date_received, sender_name, sender_address, "
            "subject, summary, sentiment, urgency, language, mailbox_name, content) "
            "VALUES (99, 99, '2026-04-09T10:00:00', 'Z', 'z@example.com', 'No kw', "
            "'totally unrelated content', 'informational', 'low', 'english', 'INBOX', 'x')"
        )
        recall_db.commit()

        def fake_sem(conn, query, limit):
            return [99]

        res = recall(recall_db, "unicornz9", semantic_candidates=fake_sem)
        hit = next((e for e in res["emails"] if e["email_id"] == 99), None)
        assert hit is not None, "semantic-only email should surface via fusion"
        assert hit["source"] == "semantic"

    def test_semantic_signal_reorders_above_keyword_only(self, recall_db):
        # Email 3 matches only via its attachment (last in the keyword waterfall).
        # A semantic vote for it should lift it above the keyword-only top hit.
        def fake_sem(conn, query, limit):
            return [3]

        res = recall(recall_db, "unicornz9", semantic_candidates=fake_sem)
        ids = [e["email_id"] for e in res["emails"]]
        assert ids[0] == 3, f"consensus (kw+semantic) should rank first, got {ids}"

    def test_semantic_failure_falls_back_to_keyword(self, recall_db):
        def broken_sem(conn, query, limit):
            raise RuntimeError("no ADC / index missing")

        res = recall(recall_db, "unicornz9", semantic_candidates=broken_sem)
        ids = [e["email_id"] for e in res["emails"]]
        assert 1 in ids  # keyword hits preserved, no crash

    def test_keyword_only_when_no_provider(self, recall_db):
        # Default path (no injected provider) is unchanged, keyword-only.
        res = recall(recall_db, "unicornz9")
        assert isinstance(res["emails"], list)
        assert any(e["email_id"] == 1 for e in res["emails"])


class TestRecallAttachmentsBucket:
    """Attachment hits need their own kind, not burial inside `emails`.

    query_by_keyword ranks attachment matches LAST in its source waterfall and
    dedups them by email_id, so whenever the covering email also matches on its
    own summary/content the attachment's far richer extracted text never
    surfaces. Against the real DB, a vendor-proposal query returned four emails
    from that vendor and none of the three matching PDF attachments, which
    search_attachments finds instantly.
    """

    def test_attachments_is_a_returned_kind(self, recall_db):
        result = recall(recall_db, UNIQUE_KW)
        assert "attachments" in result, "recall must expose attachments as its own kind"

    def test_attachment_surfaces_even_when_parent_email_also_matches(self, recall_db):
        # Give email 3 its own match, so the attachment is deduped out of `emails`.
        recall_db.execute(
            "UPDATE emails SET summary = ? WHERE id = 3",
            (f"Carrier email now mentioning {UNIQUE_KW} itself",),
        )
        recall_db.commit()

        result = recall(recall_db, UNIQUE_KW)

        assert any(a["filename"] == "deck.pdf" for a in result["attachments"]), (
            "attachment content must stay reachable when its parent email also matches"
        )
