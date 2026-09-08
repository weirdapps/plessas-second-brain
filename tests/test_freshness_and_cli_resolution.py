"""The staleness signal, and finding the adapters from a daemon's PATH.

Both exist because of the same afternoon. On 2026-09-07 the job that refreshes
this machine's replica stopped running, and for 29 hours every search tool
answered from a frozen corpus with nothing to distinguish that from "nothing has
happened". The escape hatch, outlook_live_search, could not run either: it
invoked `outlook-cli` by bare name, and that binary lives in ~/.local/bin, which
an MCP server's PATH does not include.
"""

import os
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from src.export.outlook_cli import OutlookCliError, _resolve_outlook_cli, run_outlook_cli
from src.store.query import STALE_AFTER_HOURS, get_freshness


def _db(last_sync=None, with_table=True):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    if with_table:
        conn.execute("CREATE TABLE sync_metadata (key TEXT PRIMARY KEY, value TEXT)")
        if last_sync is not None:
            conn.execute(
                "INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', ?)",
                (last_sync,),
            )
    return conn


class TestGetFreshness:
    def test_a_recent_sync_is_not_stale(self):
        recent = (datetime.now() - timedelta(minutes=20)).isoformat()
        out = get_freshness(_db(recent))
        assert out["stale"] is False
        assert out["age_hours"] < 1
        assert "stale_warning" not in out

    def test_an_old_sync_is_stale_and_says_so(self):
        old = (datetime.now() - timedelta(hours=29)).isoformat()
        out = get_freshness(_db(old))
        assert out["stale"] is True
        assert 28 < out["age_hours"] < 30
        assert "29" in out["stale_warning"] or "28" in out["stale_warning"]
        assert "outlook_live_search" in out["stale_warning"]

    def test_the_boundary_is_the_documented_threshold(self):
        just_under = (datetime.now() - timedelta(hours=STALE_AFTER_HOURS - 0.5)).isoformat()
        just_over = (datetime.now() - timedelta(hours=STALE_AFTER_HOURS + 0.5)).isoformat()
        assert get_freshness(_db(just_under))["stale"] is False
        assert get_freshness(_db(just_over))["stale"] is True

    def test_a_z_suffixed_timestamp_parses(self):
        """The producer writes naive local time, but a UTC 'Z' form is a
        plausible input and must not silently disable the whole signal.
        """
        out = get_freshness(_db("2020-01-01T00:00:00Z"))
        assert out["stale"] is True
        assert out["age_hours"] > 40000

    def test_missing_table_returns_unknown_not_false_confidence(self):
        """Absence of the cursor must read as 'unknown', never as 'fresh'."""
        out = get_freshness(_db(with_table=False))
        assert out["data_as_of"] is None
        assert out["age_hours"] is None
        assert out["stale"] is False

    def test_missing_row_returns_unknown(self):
        out = get_freshness(_db(last_sync=None))
        assert out["data_as_of"] is None
        assert out["age_hours"] is None

    def test_an_unparseable_timestamp_is_reported_not_guessed(self):
        out = get_freshness(_db("not-a-date"))
        assert out["data_as_of"] == "not-a-date"
        assert out["age_hours"] is None
        assert out["stale"] is False


class TestResolveOutlookCli:
    def test_explicit_override_wins(self, monkeypatch):
        monkeypatch.setenv("OUTLOOK_CLI_PATH", "/custom/outlook-cli")
        assert _resolve_outlook_cli() == "/custom/outlook-cli"

    def test_falls_back_to_which(self, monkeypatch):
        monkeypatch.delenv("OUTLOOK_CLI_PATH", raising=False)
        with patch("src.export.outlook_cli.shutil.which", return_value="/opt/bin/outlook-cli"):
            assert _resolve_outlook_cli() == "/opt/bin/outlook-cli"

    def test_falls_back_to_the_known_location_when_path_is_sanitized(self, monkeypatch):
        """The whole point: a daemon whose PATH lacks ~/.local/bin still resolves."""
        monkeypatch.delenv("OUTLOOK_CLI_PATH", raising=False)
        with patch("src.export.outlook_cli.shutil.which", return_value=None):
            resolved = _resolve_outlook_cli()
        assert resolved.endswith("/.local/bin/outlook-cli")
        assert os.path.isabs(resolved)

    def test_a_missing_binary_raises_a_typed_error_that_says_what_to_do(self, monkeypatch):
        """A bare FileNotFoundError reads to a caller as 'no results'."""
        monkeypatch.setenv("OUTLOOK_CLI_PATH", "/nonexistent/outlook-cli")
        with pytest.raises(OutlookCliError) as e:
            run_outlook_cli(["list-mail"])
        assert e.value.exit_code == 127
        assert e.value.retryable is False
        assert "OUTLOOK_CLI_PATH" in e.value.stderr
