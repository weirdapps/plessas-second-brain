"""How many attachments wait for their text or their summary, and for how long (issue #151).

`stats` reported mailbox age only, so a document mailed in the morning was missing from
search_attachments and recall until the 02:00 pass, and nothing said so. It now says how many
attachments wait and how long the oldest has waited, from the same two queries as the nightly
health check's Attachment lag row (#189), so the two cannot disagree.
"""

import importlib.util
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

HEALTH_CHECK_PATH = Path(__file__).parent.parent / "scripts" / "health_check.py"


@pytest.fixture
def hc():
    spec = importlib.util.spec_from_file_location("health_check", HEALTH_CHECK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _store(tmp_path, *, empty=False):
    """Two attachments without text (registered 5 h and 2 h ago), one whose text has waited 3 h
    for its summary, and one finished, which neither queue may count. Stamps are naive local
    time, as the producer writes them."""
    from src.store.schema import create_database

    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    now = datetime.now()

    def attachment(name, hours_ago):
        return conn.execute(
            "INSERT INTO attachments (message_id, filename, mime_type, file_size, file_path,"
            " exported_at) VALUES (?, ?, 'application/pdf', 1, ?, ?)",
            (name, name, f"m/{name}", (now - timedelta(hours=hours_ago)).isoformat()),
        ).lastrowid

    def content(att_id, hours_ago, llm_status):
        conn.execute(
            "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
            " extraction_status, extracted_at, llm_status) VALUES (?, 'text', 'pymupdf',"
            " 'extracted', ?, ?)",
            (att_id, (now - timedelta(hours=hours_ago)).isoformat(), llm_status),
        )

    content(attachment("finished.pdf", 40), 39, "extracted")
    if not empty:
        attachment("no-text-5h.pdf", 5)
        attachment("no-text-2h.pdf", 2)
        content(attachment("no-summary.pdf", 6), 3, "pending")
    conn.commit()
    conn.close()
    return path


def _stats(tmp_path, monkeypatch, **kw):
    from src import mcp_server
    from src.store.schema import get_connection

    path = _store(tmp_path, **kw)
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: get_connection(str(path)))
    return mcp_server.stats()


def test_stats_says_how_many_attachments_wait_and_since_when(tmp_path, monkeypatch):
    out = _stats(tmp_path, monkeypatch)

    assert out["attachments_awaiting_text"] == 2
    assert out["attachments_awaiting_text_oldest_hours"] == pytest.approx(5.0, abs=0.2)
    assert out["attachments_awaiting_summary"] == 1
    assert out["attachments_awaiting_summary_oldest_hours"] == pytest.approx(3.0, abs=0.2)


def test_stats_reads_no_age_for_an_empty_queue(tmp_path, monkeypatch):
    out = _stats(tmp_path, monkeypatch, empty=True)

    assert (out["attachments_awaiting_text"], out["attachments_awaiting_summary"]) == (0, 0)
    assert out["attachments_awaiting_text_oldest_hours"] is None
    assert out["attachments_awaiting_summary_oldest_hours"] is None


def test_stats_and_the_health_check_count_the_same_queues(tmp_path, monkeypatch, hc):
    out = _stats(tmp_path, monkeypatch)

    lag = hc.check_attachment_lag(sqlite3.connect(tmp_path / "brain.db"))

    assert lag["text_pending"] == out["attachments_awaiting_text"]
    assert lag["summary_pending"] == out["attachments_awaiting_summary"]
    hours = lag["text_age"].total_seconds() / 3600
    assert hours == pytest.approx(out["attachments_awaiting_text_oldest_hours"], abs=0.2)


@pytest.mark.parametrize(
    "stamp, hours",
    [
        ("2026-10-11T09:00:00Z", 3.0),  # UTC, as Graph writes
        ("2026-10-11T10:30:00+03:00", 4.5),  # with an offset
        ("2026-10-11T13:00:00Z", 0.0),  # newer than now: no older than we can tell
        (None, None),
        ("yesterday", None),
    ],
)
def test_hours_since(stamp, hours):
    from src.store.attachment_backlog import hours_since

    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)

    assert hours_since(stamp, now) == hours
