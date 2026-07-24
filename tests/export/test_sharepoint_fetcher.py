import sqlite3
from pathlib import Path
from unittest.mock import patch

from src.export.sharepoint_fetcher import (
    fetch_sharepoint_link,
    record_link_in_db,
)
from src.store.schema import create_database, run_migrations


def _setup_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    conn = create_database(str(db_path))
    run_migrations(conn)
    return conn


@patch("src.export.sharepoint_fetcher.run_outlook_cli")
def test_fetch_calls_download_sharepoint_link(mock_cli, tmp_path):
    mock_cli.return_value = {
        "saved": [{"path": str(tmp_path / "f.pdf"), "name": "f.pdf", "size": 100}],
        "skipped": [],
    }
    result = fetch_sharepoint_link(
        url="https://x.sharepoint.com/sites/foo/Eabc",
        out_dir=tmp_path,
    )
    assert result.status == "ok"
    assert result.local_path == tmp_path / "f.pdf"
    args = mock_cli.call_args[0][0]
    assert args[0] == "download-sharepoint-link"
    assert "--out" in args


@patch("src.export.sharepoint_fetcher.run_outlook_cli")
def test_fetch_records_404_as_stale(mock_cli, tmp_path):
    mock_cli.return_value = {
        "saved": [],
        "skipped": [{"url": "x", "reason": "not-found", "status": 404}],
    }
    result = fetch_sharepoint_link(
        url="https://x.sharepoint.com/missing",
        out_dir=tmp_path,
    )
    assert result.status == "stale"
    assert result.http_status == 404


def test_record_link_in_db_upserts(tmp_path):
    conn = _setup_db(tmp_path)
    record_link_in_db(
        conn,
        url="https://x",
        message_id="m1",
        status="ok",
        fetched_path="/tmp/f.pdf",
        file_name="f.pdf",
        file_size=100,
    )
    record_link_in_db(
        conn,
        url="https://x",
        message_id="m1",
        status="ok",
        fetched_path="/tmp/f.pdf",
        file_name="f.pdf",
        file_size=100,
    )
    rows = conn.execute("SELECT COUNT(*) FROM sharepoint_links").fetchone()
    assert rows[0] == 1  # upsert, not duplicate


def test_is_managed_sharepoint_host_matches_configured_host():
    """An auth failure on the host we hold a session for is fixable via
    re-login, so it must be recognised as 'managed'."""
    from src.export.sharepoint_fetcher import is_managed_sharepoint_host

    assert (
        is_managed_sharepoint_host(
            "https://contoso.sharepoint.com/sites/foo/Eabc",
            "contoso.sharepoint.com",
        )
        is True
    )


def test_is_managed_sharepoint_host_rejects_external_host():
    """A different tenant (e.g. a partner's SharePoint) is one we can never
    authenticate to via our login — it must NOT be treated as managed."""
    from src.export.sharepoint_fetcher import is_managed_sharepoint_host

    assert (
        is_managed_sharepoint_host(
            "https://mastercard.sharepoint.com/:x:/s/Org/abc",
            "contoso.sharepoint.com",
        )
        is False
    )


@patch("src.export.sharepoint_fetcher.fetch_sharepoint_link")
def test_process_sharepoint_skips_external_host_and_continues(mock_fetch, tmp_path):
    """An auth failure on an external host must NOT abort the whole pass;
    the run continues and still fetches managed-host URLs."""
    import argparse

    from src.cli import cmd_process_sharepoint
    from src.export.sharepoint_fetcher import SharepointFetchResult
    from src.store.schema import create_database, get_connection, run_migrations

    db_path = tmp_path / "test.db"
    conn = create_database(str(db_path))
    run_migrations(conn)
    # External-host email dated LATER → processed first (ORDER BY date DESC).
    # If the old "break on any auth-required" behaviour were still present,
    # the managed-host email below would never be reached.
    conn.execute(
        "INSERT INTO emails (message_id, date_received, content) VALUES (?, ?, ?)",
        (
            "m-ext",
            "2026-05-30T12:00:00",
            "see https://mastercard.sharepoint.com/:x:/s/Org/abc here",
        ),
    )
    conn.execute(
        "INSERT INTO emails (message_id, date_received, content) VALUES (?, ?, ?)",
        (
            "m-mgd",
            "2026-05-30T11:00:00",
            "doc https://contoso.sharepoint.com/sites/foo/Eabc end",
        ),
    )
    conn.commit()
    conn.close()

    def fake_fetch(url, out_dir):
        if "mastercard" in url:
            return SharepointFetchResult(url=url, status="auth-required")
        return SharepointFetchResult(
            url=url,
            status="ok",
            local_path=Path(str(tmp_path / "f.pdf")),
            file_name="f.pdf",
            file_size=10,
        )

    mock_fetch.side_effect = fake_fetch

    args = argparse.Namespace(db=str(db_path), since=None, limit=0, dry_run=False)
    cmd_process_sharepoint(args)

    conn = get_connection(str(db_path))
    rows = dict(conn.execute("SELECT url, last_status FROM sharepoint_links"))
    conn.close()
    managed = [s for u, s in rows.items() if "contoso" in u]
    external = [s for u, s in rows.items() if "mastercard" in u]
    # Managed URL was reached and fetched → the external one did NOT break the pass.
    assert managed == ["ok"]
    # External URL is recorded distinctly (not as a re-loginable auth issue).
    assert external == ["unsupported-host"]
