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

AWAITING_TEXT_SQL = (
    "SELECT COUNT(*), MIN(a.exported_at) FROM attachments a "
    "LEFT JOIN attachment_content ac ON ac.attachment_id = a.id WHERE ac.id IS NULL"
)
AWAITING_SUMMARY_SQL = (
    "SELECT COUNT(*), MIN(extracted_at) FROM attachment_content "
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
    parse. A naive stamp is local time, as the producer writes exported_at and extracted_at (and
    as get_freshness reads last_sync_date). A stamp newer than `now` reads 0.0."""
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.astimezone()
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
