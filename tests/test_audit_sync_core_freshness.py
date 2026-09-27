"""Freshness reflects mail arriving, not only `sync` running.

get_freshness read last_sync_date alone, which every sync stamps whether or not
any mail was exported. While the Outlook session was dead the 07:05 daily sync
still stamped it, and for three hours the replica's stats said stale=false and
recall attached no warning while the newest mail was a day old. The Inbox
export stamps last_sync_completed_at only when it succeeds (empty runs
included); sync now copies it into the store, the one file a replica receives,
and freshness is the older of the two.
"""

import json
import sqlite3
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from src.store.query import get_freshness


def _db(last_sync=None, export_ok=None):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sync_metadata (key TEXT PRIMARY KEY, value TEXT)")
    for key, value in (("last_sync_date", last_sync), ("mail_export_ok_at", export_ok)):
        if value is not None:
            conn.execute("INSERT INTO sync_metadata (key, value) VALUES (?, ?)", (key, value))
    return conn


def _local_ago(hours):
    """As the producer writes last_sync_date: naive local time."""
    return (datetime.now() - timedelta(hours=hours)).isoformat()


def _utc_ago(hours):
    """As the export writes last_sync_completed_at: UTC with a Z."""
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


class TestGetFreshness:
    def test_a_fresh_sync_over_a_dead_export_is_stale_and_says_which(self):
        export_ok = _utc_ago(19)
        out = get_freshness(_db(_local_ago(0.2), export_ok))

        assert out["stale"] is True
        assert out["data_as_of"] == export_ok
        assert 18.5 < out["age_hours"] < 19.5
        assert "export" in out["stale_warning"].lower()
        assert "outlook_live_search" in out["stale_warning"]
        assert out["mail_export_ok_at"] == export_ok

    def test_both_recent_is_fresh(self):
        out = get_freshness(_db(_local_ago(0.2), _utc_ago(0.3)))

        assert out["stale"] is False
        assert "stale_warning" not in out

    def test_an_old_sync_over_a_recent_export_names_the_sync(self):
        last_sync = _local_ago(29)
        out = get_freshness(_db(last_sync, _utc_ago(0.5)))

        assert out["stale"] is True
        assert out["data_as_of"] == last_sync
        assert "last updated" in out["stale_warning"]
        assert "export" not in out["stale_warning"].lower()

    def test_the_existing_keys_are_all_still_there(self):
        out = get_freshness(_db(_local_ago(0.2), _utc_ago(19)))

        assert {"data_as_of", "age_hours", "stale", "stale_warning"} <= set(out)
        assert out["last_sync_date"]

    def test_without_an_export_stamp_nothing_changes(self):
        last_sync = _local_ago(0.2)
        out = get_freshness(_db(last_sync))

        assert out["data_as_of"] == last_sync
        assert out["stale"] is False

    def test_an_unparseable_export_stamp_is_not_guessed_at(self):
        last_sync = _local_ago(0.2)
        out = get_freshness(_db(last_sync, "not-a-date"))

        assert out["data_as_of"] == last_sync
        assert out["stale"] is False


def _sync(tmp_path, monkeypatch):
    from src import cli
    from src.store.schema import create_database

    db_path = tmp_path / "brain.db"
    conn = create_database(str(db_path))
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("src.cli.DATA_ROOT", tmp_path)
    args = types.SimpleNamespace(
        db=db_path, limit=None, engine="claude", workers=1, skip_export=True
    )
    ok = {"extracted": 0, "failed": 0, "quota_paused": False}
    with (
        patch("src.extract.local.run_extraction", return_value=ok),
        patch("src.store.loader.load_extractions", return_value=0),
        patch("src.extract.attachment_pipeline.run_phase1", return_value={"processed": 0}),
        patch("src.export.conversation_export.export_conversations", return_value={"exported": 0}),
        patch("src.extract.image_pipeline.run_backfill", return_value={}),
    ):
        cli.cmd_sync(args)
    conn = sqlite3.connect(db_path)
    rows = dict(conn.execute("SELECT key, value FROM sync_metadata"))
    conn.close()
    return rows


def test_sync_copies_the_inbox_exports_last_success_into_the_store(tmp_path, monkeypatch):
    cursor = tmp_path / "state" / "outlook_sync.json"
    cursor.parent.mkdir(parents=True)
    cursor.write_text(
        json.dumps(
            {
                "last_sync_completed_at": "2026-09-27T04:00:00Z",
                "last_seen_received_at": "2026-09-27T03:59:00Z",
                "consecutive_failures": 7,
            }
        )
    )

    rows = _sync(tmp_path, monkeypatch)

    assert rows["mail_export_ok_at"] == "2026-09-27T04:00:00Z"


def test_sync_without_an_export_cursor_writes_no_stamp(tmp_path, monkeypatch):
    rows = _sync(tmp_path, monkeypatch)

    assert "mail_export_ok_at" not in rows
    assert rows["last_sync_date"] != "2026-01-01T00:00:00"


def test_an_unreadable_export_cursor_does_not_fail_the_sync(tmp_path, monkeypatch):
    cursor = tmp_path / "state" / "outlook_sync.json"
    cursor.parent.mkdir(parents=True)
    cursor.write_text('{"last_sync_completed_at": "2026-')

    rows = _sync(tmp_path, monkeypatch)

    assert "mail_export_ok_at" not in rows
