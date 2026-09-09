"""query_actions and query_decisions: what they cover, and what leads the page.

Three defects, all measured on the live corpus before the fix:

  * Both INNER JOINed emails, so every item whose parent was a Teams thread, a
    calendar event or a conversation turn was unreachable: 10,654 open actions
    and 13,785 decisions. The 2,555 calendar actions and 4,674 calendar
    decisions had no read path anywhere in the codebase.
  * Ingested news articles contributed 10,314 open actions and 15,602 decisions
    to what is supposed to be one person's own commitments.
  * 3,157 open actions carry a free-text deadline the model wrote ("1 day
    before", "2-3 days after"). Not being NULL, they sorted ahead of every real
    date, so the default page of "what do I owe" contained no live item at all.
"""

import pytest

from src.store.query import query_action_items, query_decisions
from src.store.schema import create_database, get_connection


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "b.db"
    create_database(str(path)).close()
    conn = get_connection(str(path))
    conn.executescript("""
        INSERT INTO emails (id, message_id, date_received, subject, mailbox_name) VALUES
            (1, 1, '2026-09-01T00:00:00Z', 'Real mail', 'Inbox'),
            (2, 2, '2026-09-01T00:00:00Z', 'Market wrap', 'News');
        INSERT INTO teams_chats (id, teams_chat_id, chat_kind, first_seen_at) VALUES
            (1, 'C1', 'group', '2026-09-01T00:00:00Z');
        INSERT INTO teams_threads (id, chat_id, thread_kind, title, started_at, ended_at) VALUES
            (1, 1, 'chat_session', 'Thread title', '2026-09-01T00:00:00Z', '2026-09-01T01:00:00Z');
        INSERT INTO calendar_events (id, outlook_event_id, subject, start_at, end_at, ingested_at) VALUES
            (1, 'E1', 'Event subject', '2026-09-01T00:00:00Z', '2026-09-01T01:00:00Z', '2026-09-01T02:00:00Z');

        INSERT INTO action_items (id, email_id, task, status, deadline) VALUES
            (1, 1,    'from email',    'open', '2099-01-01'),
            (2, 2,    'from news',     'open', '2099-01-01'),
            (3, NULL, 'ancient',       'open', '2020-01-01'),
            (4, NULL, 'free text',     'open', '1 day before'),
            (5, NULL, 'undated',       'open', NULL);
        UPDATE action_items SET teams_thread_id = 1 WHERE id = 3;
        INSERT INTO action_items (id, teams_thread_id, task, status, deadline)
            VALUES (6, 1, 'from teams', 'open', '2099-01-02');
        INSERT INTO action_items (id, event_id, task, status, deadline)
            VALUES (7, 1, 'from calendar', 'open', '2099-01-03');

        INSERT INTO decisions (id, email_id, decision) VALUES
            (1, 1, 'real decision'), (2, 2, 'news decision');
        INSERT INTO decisions (id, teams_thread_id, decision) VALUES (3, 1, 'teams decision');
        INSERT INTO decisions (id, event_id, decision) VALUES (4, 1, 'calendar decision');
    """)
    conn.commit()
    return conn


class TestSourceCoverage:
    def test_actions_reach_every_parent_kind(self, db):
        got = {r["task"]: r["source"] for r in query_action_items(db, limit=50)}
        assert got.get("from email") == "email"
        assert got.get("from teams") == "teams"
        assert got.get("from calendar") == "calendar"

    def test_decisions_reach_every_parent_kind(self, db):
        got = {r["decision"]: r["source"] for r in query_decisions(db, limit=50)}
        assert got.get("real decision") == "email"
        assert got.get("teams decision") == "teams"
        assert got.get("calendar decision") == "calendar"

    def test_sources_filter_narrows(self, db):
        only = query_action_items(db, limit=50, sources=("calendar",))
        assert [r["task"] for r in only] == ["from calendar"]


class TestNewsExclusion:
    def test_news_actions_are_excluded_by_default(self, db):
        assert "from news" not in [r["task"] for r in query_action_items(db, limit=50)]

    def test_news_actions_are_available_on_request(self, db):
        assert "from news" in [
            r["task"] for r in query_action_items(db, limit=50, include_news=True)
        ]

    def test_news_decisions_are_excluded_by_default(self, db):
        assert "news decision" not in [r["decision"] for r in query_decisions(db, limit=50)]

    def test_non_email_items_survive_the_news_filter(self, db):
        """The news predicate keys on emails.mailbox_name, and a Teams item has
        no email row at all. A naive `e.mailbox_name <> 'News'` would drop it.
        """
        assert "from teams" in [r["task"] for r in query_action_items(db, limit=50)]


class TestOrdering:
    def test_live_deadlines_come_before_overdue_and_undated(self, db):
        tasks = [r["task"] for r in query_action_items(db, limit=50)]
        assert tasks[:3] == ["from email", "from teams", "from calendar"]
        assert tasks.index("ancient") > tasks.index("from calendar")

    def test_free_text_deadlines_do_not_lead_the_page(self, db):
        tasks = [r["task"] for r in query_action_items(db, limit=50)]
        assert tasks.index("free text") > tasks.index("from email")

    def test_overdue_flag_is_set_only_for_past_real_dates(self, db):
        flags = {r["task"]: r["overdue"] for r in query_action_items(db, limit=50)}
        assert flags["ancient"] == 1
        assert flags["from email"] == 0
        assert flags["free text"] == 0
        assert flags["undated"] == 0
