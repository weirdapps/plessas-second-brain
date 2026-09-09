"""Tests for the sharepoint_index MCP tool."""

import sqlite3
from unittest.mock import patch

import pytest


@pytest.fixture
def mock_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sharepoint_links (
            url TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            fetched_at TIMESTAMP,
            fetched_path TEXT,
            last_status TEXT,
            last_attempt_at TIMESTAMP,
            file_name TEXT,
            file_size INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0
        );

        INSERT INTO sharepoint_links
            (url, message_id, fetched_at, fetched_path, last_status,
             last_attempt_at, file_name, file_size)
        VALUES
            ('https://sp/ok',     'M1', '2026-04-01T00:00:00Z', '/p/ok.pdf',
             'ok',         '2026-04-01T00:00:00Z', 'ok.pdf',     100),
            ('https://sp/stale',  'M2', NULL, NULL, 'stale',
             '2026-04-02T00:00:00Z', NULL, NULL),
            ('https://sp/herror', 'M3', NULL, NULL, 'http-error',
             '2026-04-03T00:00:00Z', NULL, NULL),
            ('https://sp/auth',   'M4', NULL, NULL, 'auth-required',
             '2026-04-04T00:00:00Z', NULL, NULL);
        """
    )
    return conn


# The refetch tests below use the value src.config.SHAREPOINT_HOST actually holds
# at import time, so they exercise the real allowlist rather than a mock of it.
def _managed_url(path: str) -> str:
    from src.config import SHAREPOINT_HOST

    return f"https://{SHAREPOINT_HOST}/{path}"


@patch("src.mcp_server._get_conn")
def test_list_stale_returns_stale_and_http_error(mock_get_conn, mock_conn):
    mock_get_conn.return_value = mock_conn
    from src.mcp_server import sharepoint_index

    result = sharepoint_index(operation="list_stale")
    urls = [row["url"] for row in result["links"]]
    assert "https://sp/stale" in urls
    assert "https://sp/herror" in urls
    assert "https://sp/ok" not in urls


@patch("src.mcp_server._get_conn")
def test_list_stale_returns_every_non_ok_status(mock_get_conn, mock_conn):
    """The hardcoded IN ('stale', 'http-error') was a list that stopped being
    complete the moment a status was added: 'auth-required' and
    'unsupported-host' both occur in sharepoint_links, and neither was visible
    here. Enumerate what is healthy instead, so a new status shows up as a
    problem rather than disappearing. scripts/health_check.py counts this way.
    """
    mock_get_conn.return_value = mock_conn
    from src.mcp_server import sharepoint_index

    result = sharepoint_index(operation="list_stale")
    urls = [row["url"] for row in result["links"]]
    assert "https://sp/auth" in urls
    assert "https://sp/ok" not in urls


@patch("src.mcp_server._get_conn")
def test_list_unfetched_returns_no_local_path(mock_get_conn, mock_conn):
    mock_get_conn.return_value = mock_conn
    from src.mcp_server import sharepoint_index

    result = sharepoint_index(operation="list_unfetched")
    urls = [row["url"] for row in result["links"]]
    assert "https://sp/stale" in urls
    assert "https://sp/herror" in urls
    assert "https://sp/auth" in urls
    assert "https://sp/ok" not in urls


@patch("src.mcp_server._get_conn")
def test_refetch_requires_url(mock_get_conn, mock_conn):
    mock_get_conn.return_value = mock_conn
    from src.mcp_server import sharepoint_index

    result = sharepoint_index(operation="refetch")
    assert "error" in result


@patch("src.mcp_server._get_conn")
@patch("src.export.sharepoint_fetcher.fetch_sharepoint_link")
def test_refetch_calls_fetcher(mock_fetch, mock_get_conn, mock_conn):
    from pathlib import Path

    from src.export.sharepoint_fetcher import SharepointFetchResult

    url = _managed_url("stale")
    mock_conn.execute(
        "INSERT INTO sharepoint_links (url, message_id, last_status) VALUES (?, 'M5', 'stale')",
        (url,),
    )
    mock_fetch.return_value = SharepointFetchResult(
        url=url,
        status="ok",
        local_path=Path("/tmp/refetched.pdf"),
        file_name="refetched.pdf",
        file_size=42,
    )
    mock_get_conn.return_value = mock_conn

    from src.mcp_server import sharepoint_index

    result = sharepoint_index(operation="refetch", url=url)
    assert result["status"] == "ok"
    assert result["local_path"] == "/tmp/refetched.pdf"
    mock_fetch.assert_called_once()


@patch("src.mcp_server._get_conn")
@patch("src.export.sharepoint_fetcher.fetch_sharepoint_link")
def test_refetch_refuses_url_not_in_the_store(mock_fetch, mock_get_conn, mock_conn):
    """`url` is model-supplied and becomes sharepoint-cli's `--host`, which makes
    the CLI aim the stored rtFa/FedAuth cookies at whatever tenant it is given.
    A search result carries attacker-authored email text to the model, so an
    unknown URL here is a cookie-exfiltration primitive. Refetch is only ever
    meant to re-attempt a link this store already indexed.
    """
    mock_get_conn.return_value = mock_conn
    from src.mcp_server import sharepoint_index

    result = sharepoint_index(operation="refetch", url="https://dummy.sharepoint.com/x/evil.docx")
    assert "error" in result
    mock_fetch.assert_not_called()


@patch("src.mcp_server._get_conn")
@patch("src.export.sharepoint_fetcher.fetch_sharepoint_link")
def test_refetch_refuses_foreign_host_even_when_recorded(mock_fetch, mock_get_conn, mock_conn):
    """Defence in depth: the store also holds links on tenants we do not own
    (that is what the 'unsupported-host' status is for), so being in the table
    is not on its own a reason to point our session at a host.
    """
    mock_conn.execute(
        "INSERT INTO sharepoint_links (url, message_id, last_status) "
        "VALUES ('https://test.sharepoint.com/f.docx', 'M6', 'unsupported-host')"
    )
    mock_get_conn.return_value = mock_conn
    from src.mcp_server import sharepoint_index

    result = sharepoint_index(operation="refetch", url="https://test.sharepoint.com/f.docx")
    assert "error" in result
    mock_fetch.assert_not_called()


@patch("src.mcp_server._get_conn")
def test_unknown_operation(mock_get_conn, mock_conn):
    mock_get_conn.return_value = mock_conn
    from src.mcp_server import sharepoint_index

    result = sharepoint_index(operation="bogus")
    assert "error" in result
