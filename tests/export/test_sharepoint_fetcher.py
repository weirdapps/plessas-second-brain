import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from src.export.sharepoint_fetcher import (
    SharepointFetchResult,
    fetch_sharepoint_link,
    record_link_in_db,
)
from src.store.schema import create_database, run_migrations

# The tenant the fetcher unit tests treat as ours. The process-sharepoint tests
# further down use the config placeholder, contoso, through the environment.
MANAGED = "x.sharepoint.com"


@pytest.fixture(autouse=True)
def _configured_tenant(monkeypatch):
    """cmd_process_sharepoint refuses to fetch until a tenant is configured, so
    every test here runs as a correctly configured host unless it says not."""
    monkeypatch.setenv("SHAREPOINT_HOST", "contoso.sharepoint.com")


def _setup_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    conn = create_database(str(db_path))
    run_migrations(conn)
    return conn


def _cli_ok(filename="f.pdf", size=100):
    """sharepoint-cli `get --out` writes the bytes itself and returns metadata."""

    def _run(args, host, **_kw):
        out = Path(args[args.index("--out") + 1])
        out.write_bytes(b"x" * size)
        return {
            "source": args[1],
            "size": size,
            "contentType": "application/pdf",
            "outPath": str(out),
            "filename": filename,
        }

    return _run


@patch("src.export.sharepoint_fetcher.run_sharepoint_cli")
def test_fetch_invokes_sharepoint_cli_get(mock_cli, tmp_path):
    mock_cli.side_effect = _cli_ok()
    result = fetch_sharepoint_link(
        url="https://x.sharepoint.com/sites/foo/Eabc",
        out_dir=tmp_path,
        managed_host=MANAGED,
    )
    assert result.status == "ok"
    # Saved under the server-supplied name, inside out_dir.
    assert result.local_path == tmp_path / "f.pdf"
    assert result.local_path.exists()
    args = mock_cli.call_args[0][0]
    assert args[0] == "get"
    assert "--out" in args


@patch("src.export.sharepoint_fetcher.run_sharepoint_cli")
def test_fetch_passes_the_urls_own_host(mock_cli, tmp_path):
    """--host is required, and for an absolute URL it must be that URL's host,
    not the managed one: a OneDrive link lives on the tenant's -my twin."""
    mock_cli.side_effect = _cli_ok()
    fetch_sharepoint_link(
        url="https://x-my.sharepoint.com/personal/a/Eabc",
        out_dir=tmp_path,
        managed_host=MANAGED,
    )
    assert mock_cli.call_args.kwargs["host"] == "x-my.sharepoint.com"


@patch("src.export.sharepoint_fetcher.run_sharepoint_cli")
def test_fetch_leaves_no_temp_file_on_success(mock_cli, tmp_path):
    mock_cli.side_effect = _cli_ok()
    fetch_sharepoint_link(url="https://x.sharepoint.com/a", out_dir=tmp_path, managed_host=MANAGED)
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".sp-")] == []


@patch("src.export.sharepoint_fetcher.run_sharepoint_cli")
def test_fetch_records_404_as_stale(mock_cli, tmp_path):
    from src.export.sharepoint_cli import SharepointCliError

    mock_cli.side_effect = SharepointCliError(
        exit_code=5,
        stderr='{"error":"not_found","message":"SharePoint 404","status":404}',
        retryable=True,
    )
    result = fetch_sharepoint_link(
        url="https://x.sharepoint.com/missing", out_dir=tmp_path, managed_host=MANAGED
    )
    assert result.status == "stale"
    assert result.http_status == 404
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".sp-")] == []


@patch("src.export.sharepoint_fetcher.run_sharepoint_cli")
def test_fetch_maps_auth_required(mock_cli, tmp_path):
    """Exit 4 must surface as auth-required so a managed-host failure still
    prompts a re-login rather than being buried as a generic error."""
    from src.export.sharepoint_cli import SharepointCliAuthRequired

    mock_cli.side_effect = SharepointCliAuthRequired("session gone")
    result = fetch_sharepoint_link(
        url="https://x.sharepoint.com/a", out_dir=tmp_path, managed_host=MANAGED
    )
    assert result.status == "auth-required"


@patch("src.export.sharepoint_fetcher.run_sharepoint_cli")
def test_fetch_maps_access_denied_to_http_error(mock_cli, tmp_path):
    from src.export.sharepoint_cli import SharepointCliError

    mock_cli.side_effect = SharepointCliError(
        exit_code=5, stderr='{"error":"access_denied","status":403}', retryable=True
    )
    result = fetch_sharepoint_link("https://x.sharepoint.com/a", tmp_path, managed_host=MANAGED)
    assert result.status == "http-error"


@patch("src.export.sharepoint_fetcher.run_sharepoint_cli")
def test_fetch_falls_back_to_url_name_when_server_sends_none(mock_cli, tmp_path):
    def _run(args, host, **_kw):
        Path(args[args.index("--out") + 1]).write_bytes(b"data")
        return {"size": 4}

    mock_cli.side_effect = _run
    result = fetch_sharepoint_link(
        "https://x.sharepoint.com/sites/foo/%CE%AD%CE%BA.pdf", tmp_path, managed_host=MANAGED
    )
    assert result.file_name == "έκ.pdf"


@patch("src.export.sharepoint_fetcher.run_sharepoint_cli")
def test_fetch_never_escapes_out_dir_via_filename(mock_cli, tmp_path):
    """A server-supplied filename is untrusted input: it must be basenamed."""

    def _run(args, host, **_kw):
        Path(args[args.index("--out") + 1]).write_bytes(b"x")
        return {"size": 1, "filename": "../../escaped.pdf"}

    mock_cli.side_effect = _run
    result = fetch_sharepoint_link("https://x.sharepoint.com/a", tmp_path, managed_host=MANAGED)
    assert result.local_path is not None
    assert result.local_path.parent == tmp_path
    assert result.local_path.name == "escaped.pdf"


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


def _insert_exhausted_link(conn, last_attempt_at, last_status="auth-required"):
    """A link that has spent its whole attempt budget without ever fetching OK.

    The status matters now. The MCAS bearer outage these tests describe failed
    on auth, not on 404, so 'auth-required' is what that scenario actually
    wrote — the previous hardcoded 'stale' was incidental. A never-fetched
    'stale' means the document 404s and is gone, and is deliberately NOT
    resurrected by the cool-off.
    """
    from src.export.sharepoint_fetcher import MAX_SHAREPOINT_ATTEMPTS

    conn.execute(
        "INSERT INTO sharepoint_links (url, message_id, fetched_at, last_status, last_attempt_at, attempts) "
        "VALUES ('https://dead', 'm', NULL, ?, ?, ?)",
        (last_status, last_attempt_at, MAX_SHAREPOINT_ATTEMPTS),
    )
    conn.commit()
    conn.close()


def test_process_sharepoint_rests_a_link_that_just_hit_max_attempts(tmp_path):
    """The cap still throttles: no re-attempt while the cool-off is running."""
    import argparse
    from datetime import UTC, datetime

    from src.cli import cmd_process_sharepoint

    _insert_exhausted_link(_setup_db(tmp_path), datetime.now(UTC).isoformat())

    with patch("src.export.sharepoint_fetcher.fetch_sharepoint_link") as mock_fetch:
        args = argparse.Namespace(db=str(tmp_path / "test.db"), since=None, limit=0, dry_run=False)
        cmd_process_sharepoint(args)
        assert not mock_fetch.called


def test_process_sharepoint_retries_an_exhausted_link_after_the_cool_off(tmp_path):
    """...but the cap must not be permanent. 43 links spent their attempts on a
    fetcher broken by the MCAS bearer bug (2026-07-30 to 08-10) and were never
    offered again, still unfetched nine days after the auth was fixed."""
    import argparse
    from datetime import UTC, datetime, timedelta

    from src.cli import cmd_process_sharepoint
    from src.export.sharepoint_fetcher import SHAREPOINT_RETRY_COOL_OFF_DAYS

    long_ago = (datetime.now(UTC) - timedelta(days=SHAREPOINT_RETRY_COOL_OFF_DAYS + 1)).isoformat()
    _insert_exhausted_link(_setup_db(tmp_path), long_ago)

    with patch("src.export.sharepoint_fetcher.fetch_sharepoint_link") as mock_fetch:
        mock_fetch.return_value = SharepointFetchResult(url="https://dead", status="stale")
        args = argparse.Namespace(db=str(tmp_path / "test.db"), since=None, limit=0, dry_run=False)
        cmd_process_sharepoint(args)
        assert mock_fetch.called


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


# --- A foreign tenant does not always answer with exit 4 ---------------------
# 'unsupported-host' only fired on auth-required, but a tenant we hold no
# session for is just as likely to answer HTTP 403 (sharepoint-cli maps
# access_denied onto 'http-error'). Those links stay eligible for the retry
# pass and are re-requested every night, forever, against a host our login can
# never open. The managed-host guard is the whole safety of this: 403 from OUR
# OWN tenant is a per-item authorisation problem that can be granted later, and
# parking it as 'unsupported-host' would abandon it permanently, which is the
# exact bug being fixed here.


def _status_after_403(tmp_path, url):
    """Scan one email carrying `url`, whose fetch answers 403. Returns the
    status recorded against it."""
    import argparse

    from src.cli import cmd_process_sharepoint
    from src.store.schema import get_connection

    db_path = tmp_path / "test.db"
    conn = _setup_db(tmp_path)
    conn.execute(
        "INSERT INTO emails (message_id, date_received, content) VALUES (?, ?, ?)",
        ("m-403", "2026-08-29T12:00:00", f"the deck is at {url} enjoy"),
    )
    conn.commit()
    conn.close()

    with patch("src.export.sharepoint_fetcher.fetch_sharepoint_link") as mock_fetch:
        mock_fetch.return_value = SharepointFetchResult(
            url=url, status="http-error", http_status=403, error_message="access_denied"
        )
        args = argparse.Namespace(db=str(db_path), since=None, limit=0, dry_run=False)
        cmd_process_sharepoint(args)

    conn = get_connection(str(db_path))
    row = conn.execute("SELECT last_status FROM sharepoint_links WHERE url = ?", (url,)).fetchone()
    conn.close()
    return row[0]


def test_a_403_from_a_foreign_tenant_is_recorded_as_unsupported_host(tmp_path):
    """No session for that host exists and no login of ours can create one, so
    the link is permanently out of reach and must stop being retried."""
    url = "https://partner.sharepoint.com/sites/Org/Edoc"
    assert _status_after_403(tmp_path, url) == "unsupported-host"


def test_a_403_from_the_managed_host_stays_an_http_error(tmp_path):
    """Our own tenant refusing one item is transient: access can be granted, so
    the link must stay in the retry pool."""
    url = "https://contoso.sharepoint.com/sites/Team/Edoc"
    assert _status_after_403(tmp_path, url) == "http-error"


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


def test_process_sharepoint_never_resurrects_an_exhausted_404(tmp_path):
    """End-to-end on the 23 links prod actually carries: every attempt returned
    404, so the document is gone. The cool-off exists to rescue links an OUTAGE
    abandoned, not to re-request a deleted file every seven days forever."""
    import argparse
    from datetime import UTC, datetime, timedelta

    from src.cli import cmd_process_sharepoint
    from src.export.sharepoint_fetcher import SHAREPOINT_RETRY_COOL_OFF_DAYS

    long_ago = (datetime.now(UTC) - timedelta(days=SHAREPOINT_RETRY_COOL_OFF_DAYS + 1)).isoformat()
    _insert_exhausted_link(_setup_db(tmp_path), long_ago, last_status="stale")

    with patch("src.export.sharepoint_fetcher.fetch_sharepoint_link") as mock_fetch:
        args = argparse.Namespace(db=str(tmp_path / "test.db"), since=None, limit=0, dry_run=False)
        cmd_process_sharepoint(args)
        assert not mock_fetch.called


# --- The session is only ever presented to our own tenant --------------------
# sharepoint-cli retargets the one stored session at whatever --host it is given
# and attaches its cookies, and the scanner accepts any *.sharepoint.com link
# from any email body. The nightly process-sharepoint pass therefore handed the
# mailbox's SharePoint session to any tenant a sender linked to; the DB shows two
# foreign tenants contacted. PR #55 closed this for the MCP refetch tool only.
# The gate now lives inside fetch_sharepoint_link, so every caller inherits it.


def test_is_managed_sharepoint_host_accepts_the_onedrive_twin():
    """OneDrive for Business lives on the tenant's "-my" twin host and is reached
    with the same session, so it is ours in both directions."""
    from src.export.sharepoint_fetcher import is_managed_sharepoint_host

    assert is_managed_sharepoint_host(
        "https://contoso-my.sharepoint.com/personal/a/Eabc", "contoso.sharepoint.com"
    )
    assert is_managed_sharepoint_host(
        "https://contoso.sharepoint.com/sites/a/Eabc", "contoso-my.sharepoint.com"
    )


def test_is_managed_sharepoint_host_rejects_lookalikes():
    from src.export.sharepoint_fetcher import is_managed_sharepoint_host

    for url in (
        "https://evilcontoso.sharepoint.com/a",
        "https://contoso-my-x.sharepoint.com/a",
        "https://contoso.sharepoint.com.evil.example/a",
        "https://contoso.sharepoint.com@evil.example/a",
        "https://contoso.sharepoint.com:8443/a",
    ):
        assert not is_managed_sharepoint_host(url, "contoso.sharepoint.com"), url


@patch("src.export.sharepoint_fetcher.run_sharepoint_cli")
def test_fetch_refuses_a_foreign_tenant_without_invoking_the_cli(mock_cli, tmp_path):
    result = fetch_sharepoint_link(
        url="https://partner.sharepoint.com/:x:/s/Org/abc",
        out_dir=tmp_path,
        managed_host=MANAGED,
    )
    assert result.status == "unsupported-host"
    assert not mock_cli.called
    assert list(tmp_path.iterdir()) == []  # not even a temp file


def test_a_foreign_tenant_url_makes_zero_subprocess_calls(tmp_path, monkeypatch):
    """Asserted at the real process boundary, not at a mock of our own wrapper."""
    calls = []
    monkeypatch.setattr("src.export.sharepoint_cli.subprocess.run", lambda *a, **k: calls.append(a))
    result = fetch_sharepoint_link(
        "https://dummy.sharepoint.com/sites/x/Edoc", tmp_path, managed_host=MANAGED
    )
    assert result.status == "unsupported-host"
    assert calls == []


def test_fetch_defaults_to_the_configured_tenant(tmp_path, monkeypatch):
    """Callers that pass no managed_host get config.SHAREPOINT_HOST."""
    import src.config

    calls = []
    monkeypatch.setattr(src.config, "SHAREPOINT_HOST", "contoso.sharepoint.com")
    monkeypatch.setattr("src.export.sharepoint_cli.subprocess.run", lambda *a, **k: calls.append(a))
    result = fetch_sharepoint_link("https://x.sharepoint.com/sites/a/Edoc", tmp_path)
    assert result.status == "unsupported-host"
    assert calls == []


def test_process_sharepoint_refuses_to_fetch_without_a_configured_tenant(tmp_path, monkeypatch):
    """Unconfigured, the gate would compare every real link against the contoso
    placeholder and park each one as a permanent 'unsupported-host'. Refusing
    loudly is the only safe answer."""
    import argparse

    from src.cli import cmd_process_sharepoint
    from src.store.schema import get_connection

    monkeypatch.delenv("SHAREPOINT_HOST", raising=False)
    db_path = tmp_path / "test.db"
    conn = _setup_db(tmp_path)
    conn.execute(
        "INSERT INTO emails (message_id, date_received, content) VALUES (?, ?, ?)",
        ("m1", "2026-09-01T10:00:00", "doc https://contoso.sharepoint.com/sites/a/Edoc end"),
    )
    conn.commit()
    conn.close()

    with patch("src.export.sharepoint_fetcher.fetch_sharepoint_link") as mock_fetch:
        args = argparse.Namespace(db=str(db_path), since=None, limit=0, dry_run=False)
        with pytest.raises(SystemExit) as exc:
            cmd_process_sharepoint(args)
        assert exc.value.code == 2
        assert not mock_fetch.called

    conn = get_connection(str(db_path))
    assert conn.execute("SELECT COUNT(*) FROM sharepoint_links").fetchone()[0] == 0
    conn.close()


def test_process_sharepoint_dry_run_needs_no_configured_tenant(tmp_path, monkeypatch):
    import argparse

    from src.cli import cmd_process_sharepoint

    monkeypatch.delenv("SHAREPOINT_HOST", raising=False)
    _setup_db(tmp_path).close()
    args = argparse.Namespace(db=str(tmp_path / "test.db"), since=None, limit=0, dry_run=True)
    cmd_process_sharepoint(args)  # must not raise


def test_retry_pass_requeues_own_tenant_links_parked_as_unsupported_host(tmp_path):
    """With SHAREPOINT_HOST at its placeholder, a session expiry on our OWN tenant
    was recorded as the permanent 'unsupported-host' (5 managed-host links on
    2026-09-03, 17 on the OneDrive twin). Our tenant is never unsupported, so
    the retry pass takes those back, and still leaves foreign tenants parked."""
    import argparse

    from src.cli import cmd_process_sharepoint

    own = "https://contoso.sharepoint.com/sites/a/E1"
    twin = "https://contoso-my.sharepoint.com/personal/b/E2"
    foreign = "https://partner.sharepoint.com/sites/c/E3"
    conn = _setup_db(tmp_path)
    for url in (own, twin, foreign):
        conn.execute(
            "INSERT INTO sharepoint_links (url, message_id, fetched_at, last_status, "
            "last_attempt_at, attempts) VALUES (?, 'm', NULL, 'unsupported-host', "
            "'2026-09-03T10:36:34+00:00', 1)",
            (url,),
        )
    conn.commit()
    conn.close()

    with patch("src.export.sharepoint_fetcher.fetch_sharepoint_link") as mock_fetch:
        mock_fetch.side_effect = lambda url, out_dir: SharepointFetchResult(
            url=url, status="ok", local_path=tmp_path / "f.pdf", file_name="f.pdf", file_size=1
        )
        args = argparse.Namespace(db=str(tmp_path / "test.db"), since=None, limit=0, dry_run=False)
        cmd_process_sharepoint(args)

    assert sorted(c.args[0] for c in mock_fetch.call_args_list) == sorted([own, twin])


def test_process_sharepoint_counts_a_refused_foreign_link_as_skipped(tmp_path, capsys):
    """The fetcher answers 'unsupported-host' itself now; the pass records it as
    such and reports it as an external skip, not a failure."""
    import argparse

    from src.cli import cmd_process_sharepoint
    from src.store.schema import get_connection

    url = "https://partner.sharepoint.com/sites/Org/Edoc"
    conn = _setup_db(tmp_path)
    conn.execute(
        "INSERT INTO emails (message_id, date_received, content) VALUES (?, ?, ?)",
        ("m1", "2026-09-01T10:00:00", f"see {url} here"),
    )
    conn.commit()
    conn.close()

    with patch("src.export.sharepoint_fetcher.fetch_sharepoint_link") as mock_fetch:
        mock_fetch.return_value = SharepointFetchResult(url=url, status="unsupported-host")
        args = argparse.Namespace(db=str(tmp_path / "test.db"), since=None, limit=0, dry_run=False)
        cmd_process_sharepoint(args)

    out = capsys.readouterr().out
    assert "External hosts skipped (no session): 1" in out
    assert "URLs failed: 0" in out
    conn = get_connection(str(tmp_path / "test.db"))
    row = conn.execute("SELECT last_status FROM sharepoint_links WHERE url = ?", (url,)).fetchone()
    conn.close()
    assert row[0] == "unsupported-host"
