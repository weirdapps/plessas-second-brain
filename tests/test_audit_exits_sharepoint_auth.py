"""process-sharepoint exits 4 when the SharePoint session has expired (cli-5).

The pass stopped on the first managed-host link that needed a login and
printed a warning, but returned None, so the nightly 'SharePoint fetch' stage
passed and new links piled up unfetched with sb-attachments green. 4 is the
estate-wide "re-authenticate" code, and calendar-sync already returns it.
"""

import argparse
from unittest.mock import patch

import pytest

from src import cli
from src.export.sharepoint_fetcher import SharepointFetchResult
from src.store.schema import create_database

URL = "https://contoso.sharepoint.com/sites/Team/Edeck"


@pytest.fixture(autouse=True)
def _configured_tenant(monkeypatch):
    monkeypatch.setenv("SHAREPOINT_HOST", "contoso.sharepoint.com")


def _db_with_one_link(tmp_path):
    db_path = tmp_path / "test.db"
    conn = create_database(str(db_path))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, content) VALUES (?, ?, ?)",
        ("m-1", "2026-09-20T09:00:00", f"the deck is at {URL} thanks"),
    )
    conn.commit()
    conn.close()
    return db_path


def _run(db_path, status):
    with patch("src.export.sharepoint_fetcher.fetch_sharepoint_link") as fetch:
        fetch.return_value = SharepointFetchResult(url=URL, status=status)
        return cli.cmd_process_sharepoint(
            argparse.Namespace(db=str(db_path), since=None, limit=0, dry_run=False)
        )


def test_an_expired_session_returns_the_reauth_code(tmp_path):
    assert _run(_db_with_one_link(tmp_path), "auth-required") == cli.EXIT_REAUTH == 4


def test_an_ordinary_fetch_failure_still_returns_zero(tmp_path):
    """A per-link error stays in the retry pool; it is not a failed stage."""
    assert _run(_db_with_one_link(tmp_path), "http-error") == 0


def test_a_clean_pass_returns_zero(tmp_path):
    assert _run(_db_with_one_link(tmp_path), "ok") == 0
