"""stale_threads lists threads someone owes the owner a reply on, nothing else.

It tested only that the owner sent last, so about 80% of what it listed was
mail the owner sent to himself (health checks, digests) and meeting responses,
neither of which anyone will ever answer. The window was also cut in local time
against UTC timestamps, so a thread 4.9 days old appeared under days=5.
"""

import pytest

from src.store.schema import create_database

OWNER = "owner@example.com"


def _email(conn, i, subject, *, recipients=(), cc=(), hours_ago=7 * 24):
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, sender_address, subject, "
        "conversation_id, mailbox_name) VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?), "
        "?, ?, ?, 'Sent Items')",
        (i, i, f"-{hours_ago} hours", OWNER, subject, f"conv{i}"),
    )
    for role, addresses in (("recipient", recipients), ("cc", cc)):
        for address in addresses:
            row = conn.execute("SELECT id FROM people WHERE email = ?", (address,)).fetchone()
            pid = (
                row[0]
                if row
                else conn.execute(
                    "INSERT INTO people (name, email) VALUES (?, ?)", (address, address)
                ).lastrowid
            )
            conn.execute(
                "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, ?)",
                (i, pid, role),
            )


@pytest.fixture
def conn(monkeypatch):
    import src.store.query as query

    monkeypatch.setattr(query, "USER_EMAIL_PATTERN", OWNER)
    c = create_database(":memory:")
    _email(c, 1, "Budget question", recipients=["colleague@example.com"])
    _email(c, 2, "Copied in", recipients=[OWNER], cc=["peer@example.com"])
    _email(c, 3, "[VPS] healthchecks: sync up", recipients=[OWNER])
    _email(c, 4, "Note to self")  # no recipient at all
    _email(c, 5, "Accepted: Quarterly review", recipients=["organiser@example.com"])
    _email(c, 6, "Tentative: Offsite", recipients=["organiser@example.com"])
    _email(c, 7, "Declined: Fraud update", recipients=["organiser@example.com"])
    _email(c, 8, "Αποδεκτή: Σύσκεψη καρτών", recipients=["organiser@example.com"])
    _email(c, 9, "Αποδοχή: Ενημέρωση", recipients=["organiser@example.com"])
    c.commit()
    return c


def test_only_threads_addressed_to_someone_else_and_not_meeting_responses(conn):
    from src.store.query import count_stale_threads, find_stale_threads

    rows = find_stale_threads(conn, days=5, max_days=30, limit=20)

    assert sorted(r["conversation_id"] for r in rows) == ["conv1", "conv2"]
    assert count_stale_threads(conn, days=5, max_days=30) == 2


def test_the_window_is_cut_in_utc(conn):
    """A thread a few hours short of `days` old is not stale yet, whatever the
    local offset from UTC."""
    from src.store.query import count_stale_threads, find_stale_threads

    _email(conn, 20, "Almost stale", recipients=["colleague@example.com"], hours_ago=5 * 24 - 2)
    conn.commit()

    ids = {r["conversation_id"] for r in find_stale_threads(conn, days=5, limit=20)}

    assert "conv20" not in ids
    assert count_stale_threads(conn, days=5) == 2


def test_the_cutoff_is_computed_in_utc():
    """Naive local time is hours off the UTC timestamps the store holds."""
    import os
    import time
    from datetime import UTC, datetime, timedelta

    import src.store.query as query

    saved = os.environ.get("TZ")
    os.environ["TZ"] = "Pacific/Kiritimati"  # UTC+14, far from any CI clock
    time.tzset()
    try:
        _sql, params = query._stale_threads_sql("COUNT(*)", 1, 30)
    finally:
        if saved is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = saved
        time.tzset()
    cutoff = datetime.strptime(params[-2], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)

    assert abs(cutoff - (datetime.now(UTC) - timedelta(days=1))) < timedelta(minutes=1)
