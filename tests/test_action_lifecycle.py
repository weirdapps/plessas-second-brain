"""Tests for action-item lifecycle: dedup exact duplicates + age-out stale actions."""

from datetime import UTC, datetime

from src.store.action_lifecycle import (
    dedup_exact_open_actions,
    expire_stale_actions,
    expire_undated_actions,
    run_action_lifecycle,
)
from src.store.schema import create_database, get_connection

OLD = "2020-01-01T10:00:00"


def _now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _seed_emails(conn, ids=(1,)):
    for i in ids:
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, summary) VALUES (?, ?, ?, ?)",
            (i, i, "2026-01-01", "s"),
        )


def _add(conn, task, owner="X", deadline=None, email_id=1, status="open", **parent):
    conn.execute(
        "INSERT INTO action_items (email_id, task, owner, deadline, status, teams_thread_id, "
        "event_id, conversation_turn_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            email_id,
            task,
            owner,
            deadline,
            status,
            parent.get("teams_thread_id"),
            parent.get("event_id"),
            parent.get("conversation_turn_id"),
        ),
    )


def _thread(conn, thread_id, started_at, ended_at):
    conn.execute(
        "INSERT OR IGNORE INTO teams_chats (id, teams_chat_id, chat_kind, first_seen_at) "
        "VALUES (1, '19:x', 'channel', ?)",
        (OLD,),
    )
    conn.execute(
        "INSERT INTO teams_threads (id, chat_id, thread_kind, started_at, ended_at, "
        "message_count, extraction_status) VALUES (?, 1, 'channel_post', ?, ?, 1, 'extracted')",
        (thread_id, started_at, ended_at),
    )


def _meeting(conn, event_id, start_at):
    conn.execute(
        "INSERT INTO calendar_events (id, outlook_event_id, start_at, end_at, is_recurring, "
        "is_self_organized, is_cancelled, ingested_at, llm_status) "
        "VALUES (?, ?, ?, ?, 0, 0, 0, ?, 'extracted')",
        (event_id, f"ev{event_id}", start_at, start_at, OLD),
    )


def _turn(conn, turn_id, timestamp):
    conn.execute(
        "INSERT INTO conversations (id, session_id, started_at, created_at) VALUES (?, ?, ?, ?)",
        (turn_id, f"s{turn_id}", timestamp, timestamp),
    )
    conn.execute(
        "INSERT INTO conversation_turns (id, conversation_id, turn_index, timestamp, speaker, "
        "content) VALUES (?, ?, 0, ?, 'user', 'x')",
        (turn_id, turn_id, timestamp),
    )


class TestDedup:
    def test_removes_exact_duplicates_keeping_one(self):
        conn = create_database(":memory:")
        _seed_emails(conn)
        for _ in range(3):
            _add(conn, "review deck", "Maria", "2026-05-01", email_id=1)
        conn.commit()
        assert dedup_exact_open_actions(conn) == 2
        assert conn.execute("SELECT COUNT(*) FROM action_items").fetchone()[0] == 1
        conn.close()

    def test_keeps_distinct_actions(self):
        conn = create_database(":memory:")
        _seed_emails(conn, (1, 2))
        _add(conn, "review deck", "Maria", email_id=1)
        _add(conn, "send report", "Maria", email_id=1)  # different task
        _add(conn, "review deck", "Maria", email_id=2)  # different email
        conn.commit()
        assert dedup_exact_open_actions(conn) == 0
        assert conn.execute("SELECT COUNT(*) FROM action_items").fetchone()[0] == 3
        conn.close()

    def test_the_same_task_under_two_non_email_parents_is_not_a_duplicate(self):
        """email_id is NULL on every Teams, meeting and conversation action, and
        GROUP BY puts NULLs together: the same task in two threads, or in a
        thread and a meeting, was deleted as a duplicate of the other."""
        conn = create_database(":memory:")
        for i in (1, 2):
            _thread(conn, i, OLD, OLD)
            _meeting(conn, i, OLD)
            _turn(conn, i, OLD)
            _add(conn, "send the deck", email_id=None, teams_thread_id=i)
            _add(conn, "send the deck", email_id=None, event_id=i)
            _add(conn, "send the deck", email_id=None, conversation_turn_id=i)
        conn.commit()
        assert dedup_exact_open_actions(conn) == 0
        conn.close()

    def test_duplicates_under_one_non_email_parent_are_still_removed(self):
        conn = create_database(":memory:")
        _thread(conn, 1, OLD, OLD)
        _add(conn, "send the deck", email_id=None, teams_thread_id=1)
        _add(conn, "send the deck", email_id=None, teams_thread_id=1)
        conn.commit()
        assert dedup_exact_open_actions(conn) == 1
        conn.close()


class TestExpire:
    def test_expires_only_old_dated_open_actions(self):
        conn = create_database(":memory:")
        _seed_emails(conn)
        _add(conn, "old", deadline="2020-01-01")
        _add(conn, "recent", deadline="2099-01-01")
        _add(conn, "nodate", deadline=None)
        _add(conn, "freetext", deadline="ASAP")
        conn.commit()
        assert expire_stale_actions(conn, days=180) == 1
        statuses = dict(conn.execute("SELECT task, status FROM action_items"))
        assert statuses["old"] == "expired"
        assert statuses["recent"] == "open"
        assert statuses["nodate"] == "open"
        assert statuses["freetext"] == "open"
        conn.close()

    def test_a_fresh_action_with_a_long_past_deadline_is_kept(self):
        """A model that writes last year for 'by 30/9' dates a fresh action a
        year overdue, and dated expiry now runs every day: it vanished the
        morning after it arrived."""
        conn = create_database(":memory:")
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received) VALUES (1, 1, ?), (2, 2, ?)",
            (_now(), OLD),
        )
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received) VALUES (3, 3, '2106-02-07T00:00:00')"
        )
        _add(conn, "fresh email", deadline="2020-09-30", email_id=1)
        _add(conn, "old email", deadline="2020-09-30", email_id=2)
        _add(conn, "no parent", deadline="2020-09-30", email_id=None)
        # A parent dated in the future is a bad date, not a fresh one.
        _add(conn, "future parent", deadline="2020-09-30", email_id=3)
        conn.commit()

        assert expire_stale_actions(conn, days=180) == 3
        assert dict(conn.execute("SELECT task, status FROM action_items")) == {
            "fresh email": "open",
            "old email": "expired",
            "no parent": "expired",
            "future parent": "expired",
        }
        conn.close()


class TestExpireUndated:
    def test_an_undated_action_ages_with_its_email(self):
        """expire_stale_actions needs a deadline, and 128K of the 154K open actions
        had none, so they stayed open for good. A free-text deadline is no date."""
        conn = create_database(":memory:")
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received) VALUES (1, 1, ?), (2, 2, ?)",
            (OLD, _now()),
        )
        _add(conn, "old, no date", email_id=1)
        _add(conn, "old, free text", deadline="ASAP", email_id=1)
        _add(conn, "old, due in the future", deadline="2099-01-01", email_id=1)
        _add(conn, "recent, no date", email_id=2)
        _add(conn, "old, completed", email_id=1, status="completed")
        conn.commit()

        assert expire_undated_actions(conn, days=90) == 2
        assert dict(conn.execute("SELECT task, status FROM action_items")) == {
            "old, no date": "expired",
            "old, free text": "expired",
            "old, due in the future": "open",
            "recent, no date": "open",
            "old, completed": "completed",
        }
        conn.close()

    def test_a_thread_ages_from_its_last_message(self):
        conn = create_database(":memory:")
        _thread(conn, 1, OLD, _now())
        _thread(conn, 2, OLD, OLD)
        _add(conn, "still talked about", email_id=None, teams_thread_id=1)
        _add(conn, "long quiet", email_id=None, teams_thread_id=2)
        conn.commit()

        assert expire_undated_actions(conn, days=90) == 1
        statuses = dict(conn.execute("SELECT task, status FROM action_items"))
        assert statuses == {"still talked about": "open", "long quiet": "expired"}
        conn.close()

    def test_meetings_and_conversations_age_too(self):
        conn = create_database(":memory:")
        _meeting(conn, 1, OLD)
        _meeting(conn, 2, "2099-01-01T10:00:00")
        _turn(conn, 1, OLD)
        _turn(conn, 2, _now())
        _add(conn, "old meeting", email_id=None, event_id=1)
        _add(conn, "future meeting", email_id=None, event_id=2)
        _add(conn, "old turn", email_id=None, conversation_turn_id=1)
        _add(conn, "recent turn", email_id=None, conversation_turn_id=2)
        conn.commit()

        assert expire_undated_actions(conn, days=90) == 2
        assert dict(conn.execute("SELECT task, status FROM action_items")) == {
            "old meeting": "expired",
            "future meeting": "open",
            "old turn": "expired",
            "recent turn": "open",
        }
        conn.close()

    def test_a_parent_without_a_date_leaves_its_action_alone(self):
        """'' is how a missing date is stored, and it sorted before every date,
        so an undatable parent counted as infinitely old."""
        conn = create_database(":memory:")
        conn.execute("INSERT INTO emails (id, message_id, date_received) VALUES (1, 1, '')")
        _turn(conn, 1, "")
        _add(conn, "email without a date", email_id=1)
        _add(conn, "turn without a date", email_id=None, conversation_turn_id=1)
        conn.commit()

        assert expire_undated_actions(conn, days=90) == 0
        conn.close()

    def test_a_free_text_deadline_in_a_coming_year_is_kept(self):
        """'31/12/2027' is no ISO date, but it is still to come."""
        conn = create_database(":memory:")
        conn.execute("INSERT INTO emails (id, message_id, date_received) VALUES (1, 1, ?)", (OLD,))
        year = datetime.now(UTC).year
        _add(conn, "due next year", deadline=f"31/12/{year + 1}", email_id=1)
        _add(conn, "due next year, short", deadline=f"31/12/{(year + 1) % 100:02d}", email_id=1)
        _add(conn, "due in eight years", deadline=f"by end {year + 8}", email_id=1)
        _add(conn, "due long ago", deadline="31/12/2020", email_id=1)
        # A year inside a longer number is no year.
        _add(conn, "after a PO", deadline=f"after PO 1{year}5 is approved", email_id=1)
        conn.commit()

        assert expire_undated_actions(conn, days=90) == 2
        assert dict(conn.execute("SELECT task, status FROM action_items")) == {
            "due next year": "open",
            "due next year, short": "open",
            "due in eight years": "open",
            "due long ago": "expired",
            "after a PO": "expired",
        }
        conn.close()

    def test_a_deadline_shaped_like_a_date_but_not_one_is_undated(self):
        """'2026-13-01' passed for a date, so the dated pass could not parse it
        and the undated pass would not touch it: open for good."""
        conn = create_database(":memory:")
        conn.execute("INSERT INTO emails (id, message_id, date_received) VALUES (1, 1, ?)", (OLD,))
        _add(conn, "month thirteen", deadline="2020-13-01", email_id=1)
        conn.commit()

        assert expire_undated_actions(conn, days=90) == 1
        conn.close()

    def test_an_action_with_no_parent_is_left_alone(self):
        conn = create_database(":memory:")
        _add(conn, "orphan", email_id=None)
        conn.commit()

        assert expire_undated_actions(conn, days=90) == 0
        conn.close()


class TestRunLifecycle:
    def test_dry_run_rolls_back(self, tmp_path):
        db = str(tmp_path / "b.db")
        conn = create_database(db)
        _seed_emails(conn)
        for _ in range(3):
            _add(conn, "dup", "Maria", "2020-01-01")
        conn.commit()
        conn.close()

        run_action_lifecycle(db, dry_run=True)
        conn = get_connection(db)
        assert conn.execute("SELECT COUNT(*) FROM action_items").fetchone()[0] == 3  # unchanged
        assert (
            conn.execute("SELECT COUNT(*) FROM action_items WHERE status='open'").fetchone()[0] == 3
        )
        conn.close()

    def test_applies_dedup_then_expire(self, tmp_path):
        db = str(tmp_path / "b.db")
        conn = create_database(db)
        _seed_emails(conn)
        for _ in range(3):
            _add(conn, "dup", "Maria", "2020-01-01")  # 3 identical, long-overdue
        conn.commit()
        conn.close()

        res = run_action_lifecycle(db)
        assert res["deduped"] == 2
        assert res["expired"] == 1  # the surviving one ages out
        assert res["after_open"] == 0
        conn = get_connection(db)
        assert conn.execute("SELECT status FROM action_items").fetchone()[0] == "expired"
        conn.close()

    def test_expires_undated_actions_by_parent_age(self, tmp_path):
        db = str(tmp_path / "b.db")
        conn = create_database(db)
        _seed_emails(conn)  # dated 2026-01-01
        _add(conn, "no date")
        conn.commit()
        conn.close()

        kept = run_action_lifecycle(db, dry_run=True, undated_expire_days=100_000)
        res = run_action_lifecycle(db, undated_expire_days=90)

        assert kept["expired_undated"] == 0
        assert (res["expired_undated"], res["after_open"]) == (1, 0)
