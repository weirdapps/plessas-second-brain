"""A store holding an item of every kind, all about 'okapi', and one sample call per tool.

The result-shape tests (freshness header, row envelope, kinds filter, the shape
snapshot) read every tool against it, so each list a tool returns has a row in it.
Every value is invented: example.com addresses, WhatsApp numbers from the
30000000000 range, as tests/whatsapp/conftest.py uses.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.store.schema import create_database

ALICE = "Alice Smith <alice@example.com>"
NEWS_DESK = "News Desk <news-desk@example.com>"
ORGANISER = "Olga Organiser <olga@example.com>"
# Each item's own date and, where the store records one, when it was ingested.
EXPORTED_AT = "2026-10-01T09:00:00Z"
EVENT_INGESTED_AT = "2026-10-01T09:30:00Z"
CONVERSATION_CREATED_AT = "2026-10-01T10:00:00Z"


def _at(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(path: Path, *, synced: str | None = None) -> Path:
    """Seed a new store at `path`; `synced` is its last_sync_date (now when None)."""
    conn = create_database(str(path))
    mail, reply, news, document = _at(2), _at(1), _at(3), _at(30)
    started, event, upcoming = _at(4), _at(5), _at(-3)
    conn.executescript(
        f"""
        INSERT INTO people (id, name, email) VALUES
            (1, 'Alice Smith', 'alice@example.com'),
            (2, 'Bob Jones', 'bob@example.com');
        INSERT INTO topics (id, name, display_name) VALUES (1, 'okapi', 'Okapi');

        INSERT INTO emails (id, message_id, date_received, subject, summary, content,
                            sender_name, sender_address, conversation_id, mailbox_name,
                            source_class) VALUES
            (1, 1, '{mail}', 'Okapi plan', 'The okapi plan', 'the okapi body',
             'Alice Smith', 'alice@example.com', 'c1', 'Inbox', 'mail'),
            (2, 2, '{reply}', 'Re: Okapi plan', 'Agreeing to the okapi plan', 'okapi reply',
             'Bob Jones', 'bob@example.com', 'c1', 'Sent Items', 'mail'),
            (3, 3, '{news}', 'Okapi markets', 'Okapi markets rally', 'okapi news',
             'News Desk', 'news-desk@example.com', 'news:digest:1', 'News', 'news'),
            (4, -4, '{document}', 'Okapi handbook', NULL, 'the okapi handbook',
             NULL, 'documents@example.com', NULL, 'External', 'document');
        INSERT INTO email_people (email_id, person_id, role_in_email) VALUES
            (1, 1, 'sender'), (1, 2, 'recipient'), (2, 2, 'sender'), (2, 1, 'recipient');
        INSERT INTO email_topics (email_id, topic_id) VALUES (1, 1), (2, 1);

        INSERT INTO attachments (id, email_id, message_id, filename, file_path, exported_at,
                                 mime_type, file_size)
            VALUES (1, 1, 1, 'okapi.pdf', '/x', '{EXPORTED_AT}', 'application/pdf', 1);
        INSERT INTO attachment_content (attachment_id, extracted_text, extraction_status,
                                        summary, llm_status)
            VALUES (1, 'the okapi figures', 'done', 'Okapi figures', 'extracted');

        INSERT INTO teams_chats (id, teams_chat_id, chat_kind, topic, first_seen_at)
            VALUES (1, '19:okapi', 'group', 'Okapi team', '{started}');
        INSERT INTO teams_threads (id, chat_id, thread_kind, started_at, ended_at,
                                   message_count, title, summary, extraction_status)
            VALUES (1, 1, 'chat_session', '{started}', '{started}', 2, 'Okapi thread',
                    'The okapi thread', 'extracted');
        INSERT INTO teams_messages (id, teams_message_id, chat_id, thread_id,
                                    sender_display_name, sender_person_id, composed_at,
                                    content_text, is_system)
            VALUES (1, 't1', 1, 1, 'Bob Jones', 2, '{started}', 'morning all', 0),
                   (2, 't2', 1, 1, 'Alice Smith', 1, '{started}', 'the okapi numbers', 0);

        INSERT INTO whatsapp_chats (id, chat_jid, name, chat_kind, first_seen_at)
            VALUES (1, '120000000000000001@g.us', 'Chat A', 'group', '{started}');
        INSERT INTO whatsapp_threads (id, chat_id, anchor_message_id, started_at, ended_at,
                                      message_count, participant_names, title, summary)
            VALUES (1, 1, 'w1', '{started}', '{started}', 2, '["Carol Example"]',
                    'Okapi chat', 'Planning the okapi visit');
        INSERT INTO whatsapp_messages (chat_jid, message_id, chat_id, thread_id, sender_jid,
                                       sender_name, is_from_me, sent_at, content)
            VALUES ('120000000000000001@g.us', 'w1', 1, 1, '30000000001@s.whatsapp.net',
                    'Carol Example', 0, '{started}', 'see you friday'),
                   ('120000000000000001@g.us', 'w2', 1, 1, '30000000002@s.whatsapp.net',
                    'Dan Example', 0, '{started}', 'okapi tickets bought'),
                   ('120000000000000001@g.us', 'w3', 1, 1, '30000000003@s.whatsapp.net',
                    'Alice Smith', 0, '{started}', 'see you there');

        INSERT INTO calendar_events (id, outlook_event_id, subject, body_summary,
                                     organizer_name, organizer_email, start_at, end_at,
                                     is_recurring, is_self_organized, is_cancelled,
                                     ingested_at, llm_status, response_status)
            VALUES (1, 'ev1', 'Okapi review', 'Purpose: the okapi review', 'Olga Organiser',
                    'olga@example.com', '{event}', '{event}', 0, 0, 0,
                    '{EVENT_INGESTED_AT}', 'extracted', 'Accepted'),
                   (2, 'ev2', 'Okapi follow-up', 'Purpose: the next okapi steps',
                    'Alice Smith', 'alice@example.com', '{upcoming}', '{upcoming}', 0, 0, 0,
                    '{EVENT_INGESTED_AT}', 'extracted', 'Accepted');
        INSERT INTO event_attendees (event_id, email, name, response_status, is_organizer,
                                     is_self)
            VALUES (1, 'olga@example.com', 'Olga Organiser', 'Organizer', 1, 0),
                   (1, 'alice@example.com', 'Alice Smith', 'Accepted', 0, 0),
                   (2, 'alice@example.com', 'Alice Smith', 'Organizer', 1, 0);

        INSERT INTO conversations (id, session_id, started_at, ended_at, project_name,
                                   turn_count, summary, created_at)
            VALUES (1, 's1', '{started}', '{started}', 'okapi', 1, 'An okapi session',
                    '{CONVERSATION_CREATED_AT}');
        INSERT INTO conversation_turns (conversation_id, turn_index, timestamp, speaker,
                                        content)
            VALUES (1, 0, '{started}', 'user', 'the okapi question');

        INSERT INTO decisions (id, email_id, teams_thread_id, decision, decided_by,
                               decision_date) VALUES
            (1, 1, NULL, 'Adopt the okapi plan', 'Alice Smith', date('now', '-2 days')),
            (2, NULL, 1, 'Pilot the okapi tracker', 'Bob Jones', date('now', '-4 days'));
        INSERT INTO action_items (id, email_id, teams_thread_id, task, owner, deadline,
                                  status) VALUES
            (1, 1, NULL, 'Draft the okapi budget', 'Alice Smith', date('now', '+5 days'),
             'open'),
            (2, NULL, 1, 'Book the okapi room', 'Bob Jones', date('now', '-3 days'), 'open');
        INSERT INTO commitments (email_id, commitment, by_person, to_person)
            VALUES (1, 'Send the okapi figures', 'Alice Smith', 'Bob Jones');
        INSERT INTO key_facts (email_id, fact) VALUES (1, 'The okapi plan costs 5');

        INSERT INTO inline_images (sha256, width, height, bytes, classification,
                                   classification_method, classified_at, vision_description)
            VALUES ('ab12', 800, 600, 1000, 'content', 'vision', '{mail}', 'An okapi chart');
        INSERT INTO inline_image_occurrences (sha256, message_id, sender_email,
                                              position_in_body)
            VALUES ('ab12', '1', 'alice@example.com', 0.5);

        INSERT INTO figure_series (id, stem, sender, name, editions, first_date, last_date,
                                   latest_period, latest_end)
            VALUES (1, 'okapi report', 'alice@example.com', 'Okapi report', 3, '{news}',
                    '{mail}', '2026-09', '2026-09-30');
        INSERT INTO figure_editions (attachment_id, series_id, edition_date, status, figures,
                                     parser)
            VALUES (1, 1, '{mail}', 'parsed', 2, 1);
        INSERT INTO figure_metrics (id, series_id, key, sheet, label, header)
            VALUES (1, 1, 7, 'KPIs', 'Okapi sightings', '');
        INSERT INTO figure_values (metric_id, period, period_end, period_source, value,
                                   attachment_id, cell, text_offset)
            VALUES (1, '2026-08', '2026-08-31', 'header', 40, 1, 'R2C2', 4),
                   (1, '2026-09', '2026-09-30', 'header', 42, 1, 'R2C3', 8);
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES ('last_sync_date', ?)",
        (synced or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    conn.commit()
    conn.close()
    return path


# One call per tool, each finding something in the store above.
CALLS = {
    "person_context": {"name_or_email": "alice@example.com"},
    "topic_context": {"topic": "okapi"},
    "sender_brief": {"name_or_email": "alice@example.com"},
    "email_thread": {"email_id": 1},
    "search_emails": {"query": "okapi"},
    "recall": {"query": "okapi", "include_context": True},
    "query_emails": {"keyword": "okapi"},
    "query_decisions": {},
    "query_actions": {},
    "stale_threads": {},
    "meeting_prep": {"people": "alice@example.com", "topic": "okapi"},
    "search_attachments": {"query": "okapi"},
    "figures": {"query": "okapi"},
    "query_calendar_events": {"keyword": "okapi"},
    "stats": {},
    "search_conversations": {"query": "okapi"},
    "conversation_context": {"session_id": "s1"},
    "sharepoint_index": {"operation": "list_stale"},
    "attachment_image_search": {"query": "okapi"},
    "outlook_live_search": {},
    "search_teams": {"query": "okapi"},
    "search_whatsapp": {"query": "okapi"},
    "teams_thread_context": {"thread_id": 1},
    "teams_chat_summary": {"chat_id": 1},
    "sql_query": {"sql": "SELECT id, subject FROM emails"},
    "sql_schema": {"table": "emails"},
    "get_item": {"kind": "email", "ref": 1},
}


def serve(path: Path, monkeypatch) -> None:
    """Point every tool at the store at `path`, through the opens a client's call takes."""
    from src import mcp_server
    from src.store import sql_readonly

    monkeypatch.setattr(mcp_server, "DEFAULT_DB", path)
    monkeypatch.setattr(sql_readonly, "DEFAULT_DB", path)
    monkeypatch.setattr("src.export.outlook_cli.run_outlook_cli", lambda args: [])
