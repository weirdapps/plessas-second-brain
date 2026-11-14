"""Read-path defects the audit of 2026-11-14 reproduced on the replica. Every value is invented.

since/until on an email listing were UTC days, though the filter says Athens time
decisions and actions taken from agent sessions were served by default
meeting_prep dropped a whole attendee when the meetings between them were heavy
an action due yesterday read as not overdue until 02:00 or 03:00 in Athens
"""

import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import mcp_budget, mcp_server
from src.store import ordering
from tests import seeded_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = seeded_store.build(tmp_path / "brain.db")
    seeded_store.serve(path, monkeypatch)
    return path


def _write(path, sql, *params):
    conn = sqlite3.connect(path)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def _mail(path, email_id, received):
    _write(
        path,
        "INSERT INTO emails (id, message_id, date_received, subject, summary, content,"
        " sender_name, sender_address, mailbox_name, source_class)"
        " VALUES (?, ?, ?, 'Zebu note', 'zebu', 'zebu', 'Alice Smith', 'alice@example.com',"
        " 'Inbox', 'mail')",
        email_id,
        email_id,
        received,
    )


def test_since_and_until_are_athens_days(store):
    """10 November in Athens (UTC+2) runs from 22:00Z on the 9th to 22:00Z on the 10th."""
    _mail(store, 50, "2026-11-09T21:59:59Z")  # 23:59 on the 9th in Athens
    _mail(store, 51, "2026-11-09T22:00:00Z")  # 00:00 on the 10th
    _mail(store, 52, "2026-11-10T21:59:59Z")  # 23:59 on the 10th
    _mail(store, 53, "2026-11-10T22:00:00Z")  # 00:00 on the 11th

    def ids(**bounds):
        rows = seeded_store.call("search", {"query": "zebu", "filter": bounds})["result"]
        return sorted(row["ref"] for row in rows)

    assert ids(since="2026-11-10", until="2026-11-10") == [51, 52]
    assert ids(since="2026-11-10T23:59:00", until="2026-11-11") == [52, 53]
    assert ids(until="2026-11-09") == [50]


def _session(path, conversation_id, speaker):
    _write(
        path,
        "INSERT INTO conversations (id, session_id, started_at, ended_at, project_name,"
        " turn_count, summary, created_at) VALUES (?, ?, '2026-11-01T09:00:00Z',"
        " '2026-11-01T09:00:00Z', 'gnu', 1, 'A gnu session', '2026-11-01T09:00:00Z')",
        conversation_id,
        f"s{conversation_id}",
    )
    _write(
        path,
        "INSERT INTO conversation_turns (id, conversation_id, turn_index, timestamp, speaker,"
        " content) VALUES (?, ?, 0, '2026-11-01T09:00:00Z', ?, 'the gnu turn')",
        conversation_id * 10,
        conversation_id,
        speaker,
    )
    _write(
        path,
        "INSERT INTO action_items (conversation_turn_id, task, owner, status)"
        " VALUES (?, ?, 'N/A', 'open')",
        conversation_id * 10,
        f"Audit the gnu files ({speaker})",
    )
    _write(
        path,
        "INSERT INTO decisions (conversation_turn_id, decision, decided_by, decision_date)"
        " VALUES (?, ?, 'N/A', '2026-11-01')",
        conversation_id * 10,
        f"Keep the gnu files ({speaker})",
    )


def test_items_from_an_agent_session_are_left_out(store):
    """A subagent's session is left out of every conversation read, and `read` refuses it:
    what it was told to do is not the owner's action (2,813 open actions, 2026-11-14)."""
    _session(store, 20, "user")
    _session(store, 21, "agent")

    actions = seeded_store.call("query_items", {"kind": "actions", "filter": {"person": "N/A"}})
    decisions = seeded_store.call("query_items", {"kind": "decisions", "filter": {"person": "N/A"}})
    recalled = seeded_store.call("recall", {"query": "gnu files"})

    assert [row["task"] for row in actions["result"]] == ["Audit the gnu files (user)"]
    assert [row["decision"] for row in decisions["result"]] == ["Keep the gnu files (user)"]
    assert [row["task"] for row in recalled["actions"]] == ["Audit the gnu files (user)"]
    assert [row["decision"] for row in recalled["decisions"]] == ["Keep the gnu files (user)"]


def test_an_owner_is_found_in_either_script(store):
    """The owner column is what the model wrote: Greek in one action, Latin in the next."""
    conn = sqlite3.connect(store)
    conn.executescript(
        """
        INSERT INTO people (name, email) VALUES
            ('Νίκος Παπαδόπουλος', 'np@example.com'), ('Nikos Papadopoulos', NULL);
        INSERT INTO action_items (email_id, task, owner, status) VALUES
            (1, 'Send the ibex list', 'Νίκος Παπαδόπουλος', 'open'),
            (1, 'Check the ibex count', 'Papadopoulos Nikos', 'open'),
            (1, 'Unrelated', 'Nikos Georgiou', 'open');
        """
    )
    conn.commit()
    conn.close()
    wanted = ["Check the ibex count", "Send the ibex list"]
    for name in ("Papadopoulos", "Παπαδόπουλος", "Nikos Papadopoulos", "Νίκος Παπαδόπουλος"):
        rows = seeded_store.call("query_items", {"kind": "actions", "filter": {"person": name}})
        assert sorted(row["task"] for row in rows["result"]) == wanted, name


def test_heavy_shared_lists_do_not_cost_an_attendee():
    """Each dossier fits its share; the meetings between the attendees did not fit what was
    left, and the last pass cut the heaviest list, which was the attendees themselves."""
    limit = 30_000
    dossier = {"name": "x", "emails": [{"summary": "s" * 400} for _ in range(20)]}
    out = {
        "attendees": [dict(dossier), dict(dossier)],
        "meetings": [{"subject": "m" * 500} for _ in range(40)],
        "series_pages": [{"body": "p" * 800} for _ in range(4)],
    }
    assert mcp_budget.json_chars(out) > limit

    held = mcp_server._budget_dossiers(out, "attendees", limit)

    assert len(held["attendees"]) == 2
    assert mcp_budget.json_chars(held) <= limit
    assert held["truncated"]["meetings"]["total"] == 40
    assert json.dumps(held)  # still one JSON answer


def test_overdue_is_judged_on_the_athens_day(store, monkeypatch):
    """At 00:30 in Athens it is still yesterday in UTC: an action due yesterday is overdue."""
    today = datetime.now(ZoneInfo("Europe/Athens")).date().isoformat()
    assert ordering.today_sql() == f"'{today}'"

    monkeypatch.setattr(ordering, "today_sql", lambda: "'2026-11-15'")
    monkeypatch.setattr("src.store.query.today_sql", lambda: "'2026-11-15'")
    _write(store, "UPDATE action_items SET deadline = '2026-11-14' WHERE id = 1")
    rows = seeded_store.call("query_items", {"kind": "actions"})["result"]
    assert {row["ref"]: row["overdue"] for row in rows}[1] == 1
