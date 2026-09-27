"""chat_summary's cutoff compares time values, not strings.

The cutoff came from datetime('now', ...), which writes a space between date and
time, while ended_at and composed_at are stored with a 'T'. 'T' sorts after ' ',
so everything from earlier on the cutoff day passed the filter (audit
db-integrity-2, Teams part).
"""

from datetime import UTC, datetime, timedelta

from src.store.teams_query import chat_summary


def test_activity_earlier_on_the_cutoff_day_is_left_out(db):
    days = 2
    cutoff = datetime.now(UTC) - timedelta(days=days)
    # Same calendar day as the cutoff, but hours before it.
    before = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
    before_iso = before.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    recent_iso = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    db.execute(
        "INSERT INTO teams_chats(teams_chat_id, chat_kind, topic, first_seen_at) "
        "VALUES ('19:summary@thread.v2', 'group', 'x', '2026-01-01T00:00:00')"
    )
    chat_id = db.execute("SELECT id FROM teams_chats").fetchone()["id"]
    for title, ts in (("old", before_iso), ("new", recent_iso)):
        db.execute(
            "INSERT INTO teams_threads(chat_id, thread_kind, anchor_message_id, started_at, "
            "ended_at, message_count, title, extraction_status) "
            "VALUES (?, 'chat_session', ?, ?, ?, 1, ?, 'done')",
            (chat_id, title, ts, ts, title),
        )
        db.execute(
            "INSERT INTO teams_messages(teams_message_id, chat_id, composed_at, "
            "sender_display_name, content_text, is_system) VALUES (?, ?, ?, ?, 'text', 0)",
            (f"{chat_id}::{title}", chat_id, ts, f"Sender {title}"),
        )
    db.commit()

    out = chat_summary(db, chat_id, days=days)

    assert [t["title"] for t in out["threads"]] == ["new"]
    assert [s["name"] for s in out["top_senders"]] == ["Sender new"]
