"""How many attachments wait for their text or their summary, and since when.

The text queue is Phase 1's: an attachment with no attachment_content row yet, timed from its
registration (attachments.exported_at). The summary queue is Phase 2's: extracted text still
waiting for the model, timed from its extraction. The nightly health check's Attachment lag row
(scripts/health_check.py) and the MCP `stats` tool both read them here, so the two cannot
disagree. Neither query reads a finished attachment's stored text, 3 GB on the producer: the
first is answered from the attachment_id index, and the second reads only the rows still waiting.
"""

import sqlite3
from datetime import UTC, datetime
from typing import NamedTuple

from src.store.stamps import instant, instant_sql, iso_sql

AWAITING_TEXT_SQL = (
    "SELECT COUNT(*), MIN(a.exported_at) FROM attachments a "
    "LEFT JOIN attachment_content ac ON ac.attachment_id = a.id WHERE ac.id IS NULL"
)
# The oldest as an instant, in UTC: extracted_at holds local time with no offset until `brain
# maint utc-stamps` has run, and UTC with one after 2026-10-16, and MIN() of the text would take
# a UTC stamp for three hours older than it is (src/store/stamps.py).
_OLDEST_EXTRACTED = iso_sql("MIN(" + instant_sql("extracted_at") + ")")
AWAITING_SUMMARY_SQL = (
    f"SELECT COUNT(*), {_OLDEST_EXTRACTED} FROM attachment_content "
    "WHERE extraction_status = 'extracted' AND llm_status = 'pending'"
)


class Queue(NamedTuple):
    waiting: int  # how many wait
    oldest: str | None  # the oldest one's stamp, as stored


def attachment_backlog(conn: sqlite3.Connection) -> tuple[Queue, Queue]:
    """The text queue and the summary queue."""
    text = conn.execute(AWAITING_TEXT_SQL).fetchone()
    summary = conn.execute(AWAITING_SUMMARY_SQL).fetchone()
    return Queue(text[0], text[1]), Queue(summary[0], summary[1])


def hours_since(stamp: str | None, now: datetime | None = None) -> float | None:
    """Hours from a stored stamp to `now`, to one decimal; None for no stamp or one that does not
    parse. A naive stamp is local time, as the producer writes exported_at and wrote extracted_at
    until 2026-10-16 (src/store/stamps.py instant). A stamp newer than `now` reads 0.0."""
    then = instant(stamp)
    if then is None:
        return None
    now = now or datetime.now(UTC)
    return max(0.0, round((now - then).total_seconds() / 3600.0, 1))


def backlog_stats(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """The `stats` keys: how many attachments wait for their text and for their summary, and how
    long the oldest of each has waited, in hours (None when none waits)."""
    text, summary = attachment_backlog(conn)
    return {
        "attachments_awaiting_text": text.waiting,
        "attachments_awaiting_text_oldest_hours": hours_since(text.oldest, now),
        "attachments_awaiting_summary": summary.waiting,
        "attachments_awaiting_summary_oldest_hours": hours_since(summary.oldest, now),
    }
