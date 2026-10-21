"""stats' WhatsApp coverage starts where the history does.

When the bridge was first linked, WhatsApp handed it the last message of each older
chat and nothing else, so the store held 21 messages from 2022-06-15 to 2026-06-30
and then everything from 2026-07-02. Coverage read 'first: 2022-06-15', and an empty
answer said WhatsApp had been searched back to 2022 (audit 2026-10-20). The span now
starts after the last long silence when what lies before it is a sliver, and says what
it left out; a long history with a gap in it keeps its first date.
"""

from datetime import UTC, datetime, timedelta

from src.store.query import get_coverage


def _add(db, times):
    db.execute(
        "INSERT OR IGNORE INTO whatsapp_chats (id, chat_jid, chat_kind, first_seen_at)"
        " VALUES (1, '120000000000000001@g.us', 'group', '2026-09-28T00:00:00Z')"
    )
    start = db.execute("SELECT COUNT(*) FROM whatsapp_messages").fetchone()[0]
    for n, at in enumerate(times, start=start):
        db.execute(
            "INSERT INTO whatsapp_messages (chat_jid, message_id, chat_id, sent_at, content)"
            " VALUES ('120000000000000001@g.us', ?, 1, ?, 'x')",
            (f"m{n}", at.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
    db.commit()


def _daily(start, days):
    return [start + timedelta(days=d) for d in range(days)]


def test_stray_old_messages_do_not_start_the_coverage(db):
    _add(db, [datetime(2022, 6, 15, tzinfo=UTC), datetime(2025, 4, 11, tzinfo=UTC)])
    _add(db, _daily(datetime(2026, 7, 2, 8, tzinfo=UTC), 100))

    span = get_coverage(db)["whatsapp"]

    assert span["first"] == "2026-07-02T08:00:00Z"
    assert span["items"] == 102
    assert span["earliest"] == "2022-06-15T00:00:00Z"
    assert span["earlier_items"] == 2


def test_a_continuous_history_reads_as_before(db):
    _add(db, _daily(datetime(2026, 7, 2, tzinfo=UTC), 50))

    span = get_coverage(db)["whatsapp"]

    assert span == {"first": "2026-07-02T00:00:00Z", "last": "2026-08-20T00:00:00Z", "items": 50}


def test_a_long_history_with_a_gap_keeps_its_first_date(db):
    _add(db, _daily(datetime(2025, 1, 1, tzinfo=UTC), 200))
    _add(db, _daily(datetime(2026, 1, 1, tzinfo=UTC), 100))

    span = get_coverage(db)["whatsapp"]

    assert span["first"] == "2025-01-01T00:00:00Z"
    assert "earliest" not in span


def test_no_whatsapp_reads_as_before(db):
    assert get_coverage(db)["whatsapp"] == {"first": None, "last": None, "items": 0}
