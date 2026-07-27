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


def test_record_link_in_db_tracks_attempts(tmp_path):
    """Failed attempts accumulate; a successful fetch resets the counter — so the
    retry pass can give up on links that keep failing."""
    conn = _setup_db(tmp_path)
    record_link_in_db(conn, url="https://x", message_id="m1", status="stale")
    record_link_in_db(conn, url="https://x", message_id="m1", status="http-error")
    assert (
        conn.execute("SELECT attempts FROM sharepoint_links WHERE url='https://x'").fetchone()[0]
        == 2
    )
    record_link_in_db(
        conn,
        url="https://x",
        message_id="m1",
        status="ok",
        fetched_path="/f",
        file_name="f",
        file_size=1,
    )
    assert (
        conn.execute("SELECT attempts FROM sharepoint_links WHERE url='https://x'").fetchone()[0]
        == 0
    )


def test_process_sharepoint_gives_up_after_max_attempts(tmp_path):
    """A link that has already failed the max number of times is NOT retried."""
    import argparse

    from src.cli import cmd_process_sharepoint
    from src.export.sharepoint_fetcher import MAX_SHAREPOINT_ATTEMPTS

    conn = _setup_db(tmp_path)
    conn.execute(
        "INSERT INTO sharepoint_links (url, message_id, fetched_at, last_status, last_attempt_at, attempts) "
        "VALUES ('https://dead', 'm', NULL, 'stale', '2026-05-01T00:00:00', ?)",
        (MAX_SHAREPOINT_ATTEMPTS,),
    )
    conn.commit()
    conn.close()

    with patch("src.export.sharepoint_fetcher.fetch_sharepoint_link") as mock_fetch:
        args = argparse.Namespace(db=str(tmp_path / "test.db"), since=None, limit=0, dry_run=False)
        cmd_process_sharepoint(args)
        assert not mock_fetch.called  # exhausted link is not retried


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


@patch("src.export.sharepoint_fetcher.fetch_sharepoint_link")
def test_process_sharepoint_retries_known_unfetched_link(mock_fetch, tmp_path):
    """A link recorded earlier but never successfully fetched (fetched_at NULL,
    status 'stale') must be retried on the next pass even when its source email
    is no longer in the scanned window — otherwise old unfetched/stale links
    never clear."""
    import argparse

    from src.cli import cmd_process_sharepoint
    from src.export.sharepoint_fetcher import SharepointFetchResult
    from src.store.schema import create_database, get_connection, run_migrations

    url = "https://contoso.sharepoint.com/sites/foo/Estale"
    db_path = tmp_path / "test.db"
    conn = create_database(str(db_path))
    run_migrations(conn)
    # Previously-seen link that never fetched OK — and NO email in the DB
    # references it, so the email scan alone would never re-reach it.
    conn.execute(
        """INSERT INTO sharepoint_links (url, message_id, fetched_at, last_status, last_attempt_at)
           VALUES (?, ?, NULL, 'stale', '2026-05-01T00:00:00')""",
        (url, "m-old"),
    )
    conn.commit()
    conn.close()

    mock_fetch.return_value = SharepointFetchResult(
        url=url,
        status="ok",
        local_path=Path(str(tmp_path / "f.pdf")),
        file_name="f.pdf",
        file_size=10,
    )

    args = argparse.Namespace(db=str(db_path), since=None, limit=0, dry_run=False)
    cmd_process_sharepoint(args)

    conn = get_connection(str(db_path))
    row = conn.execute(
        "SELECT last_status, fetched_at FROM sharepoint_links WHERE url = ?", (url,)
    ).fetchone()
    conn.close()
    assert row[0] == "ok"  # retried and succeeded
    assert row[1] is not None  # fetched_at now populated
