"""Tests for action-item lifecycle: dedup exact duplicates + age-out stale actions."""

from src.store.action_lifecycle import (
    dedup_exact_open_actions,
    expire_stale_actions,
    run_action_lifecycle,
)
from src.store.schema import create_database, get_connection


def _seed_emails(conn, ids=(1,)):
    for i in ids:
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, summary) VALUES (?, ?, ?, ?)",
            (i, i, "2026-01-01", "s"),
        )


def _add(conn, task, owner="X", deadline=None, email_id=1, status="open"):
    conn.execute(
        "INSERT INTO action_items (email_id, task, owner, deadline, status) VALUES (?, ?, ?, ?, ?)",
        (email_id, task, owner, deadline, status),
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
