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
            file_size INTEGER
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

    mock_fetch.return_value = SharepointFetchResult(
        url="https://sp/stale",
        status="ok",
        local_path=Path("/tmp/refetched.pdf"),
        file_name="refetched.pdf",
        file_size=42,
    )
    mock_get_conn.return_value = mock_conn

    from src.mcp_server import sharepoint_index

    result = sharepoint_index(operation="refetch", url="https://sp/stale")
    assert result["status"] == "ok"
    assert result["local_path"] == "/tmp/refetched.pdf"
    mock_fetch.assert_called_once()


@patch("src.mcp_server._get_conn")
def test_unknown_operation(mock_get_conn, mock_conn):
    mock_get_conn.return_value = mock_conn
    from src.mcp_server import sharepoint_index

    result = sharepoint_index(operation="bogus")
    assert "error" in result
