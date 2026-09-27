"""recent_conversations compares times, not strings.

started_at is stored as '2026-09-27T00:27:13.502Z' and datetime('now', '-1
days') renders '2026-09-26 13:00:00'. 'T' sorts after ' ', so every session
from earlier on the cutoff day compared as newer than the cutoff, and days=1
reached back almost 48 hours.
"""

from datetime import UTC, datetime, timedelta

from src.store.conversation_query import recent_conversations
from src.store.schema import create_database


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def test_a_session_just_before_the_cutoff_is_left_out():
    conn = create_database(":memory:")
    now = datetime.now(UTC)
    for session, moment in (
        ("old", now - timedelta(days=1, minutes=1)),
        ("new", now - timedelta(hours=1)),
    ):
        conn.execute(
            "INSERT INTO conversations (session_id, started_at, created_at, workspace, summary) "
            "VALUES (?, ?, ?, '/work/brain', 'a session')",
            (session, _stamp(moment), _stamp(moment)),
        )
    conn.commit()

    rows = recent_conversations(conn, days=1)

    assert [r["session_id"] for r in rows] == ["new"]
