"""Tests for the attachment_image_search MCP tool."""

import sqlite3
from unittest.mock import patch

import pytest


@pytest.fixture
def mock_conn():
    """In-memory DB seeded with inline_images + occurrences."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE inline_images (
            sha256 TEXT PRIMARY KEY,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            bytes INTEGER NOT NULL,
            classification TEXT NOT NULL,
            classification_method TEXT NOT NULL,
            classified_at TIMESTAMP NOT NULL,
            vision_description TEXT,
            user_overridden BOOLEAN NOT NULL DEFAULT 0
        );
        CREATE TABLE inline_image_occurrences (
            sha256 TEXT NOT NULL,
            message_id TEXT NOT NULL,
            sender_email TEXT NOT NULL,
            position_in_body REAL NOT NULL,
            PRIMARY KEY (sha256, message_id)
        );

        INSERT INTO inline_images
            (sha256, width, height, bytes, classification, classification_method,
             classified_at, vision_description)
        VALUES
            ('abc', 800, 600, 12345, 'content', 'vision', '2026-04-01T00:00:00Z',
             'Bar chart showing revenue dashboard for Q1'),
            ('def', 100, 100, 500, 'content', 'vision', '2026-04-02T00:00:00Z',
             'A signature graphic'),
            ('ghi', 1000, 800, 99999, 'noise', 'cascade', '2026-04-03T00:00:00Z',
             NULL);

        INSERT INTO inline_image_occurrences
            (sha256, message_id, sender_email, position_in_body)
        VALUES
            ('abc', 'OUTLOOK-MSG-1', 'alice@example.com', 0.4),
            ('abc', 'OUTLOOK-MSG-2', 'bob@example.com', 0.7),
            ('def', 'OUTLOOK-MSG-3', 'carol@example.com', 0.9);
        """
    )
    return conn


@patch("src.mcp_server._get_conn")
def test_image_search_finds_match_by_description(mock_get_conn, mock_conn):
    mock_get_conn.return_value = mock_conn
    from src.mcp_server import attachment_image_search

    result = attachment_image_search(query="dashboard")
    assert result["count"] >= 1
    shas = [r["sha256"] for r in result["results"]]
    assert "abc" in shas


@patch("src.mcp_server._get_conn")
def test_image_search_skips_noise(mock_get_conn, mock_conn):
    mock_get_conn.return_value = mock_conn
    from src.mcp_server import attachment_image_search

    result = attachment_image_search(query="anything")
    shas = [r["sha256"] for r in result["results"]]
    # 'ghi' is classification='noise' -> filtered out
    assert "ghi" not in shas


@patch("src.mcp_server._get_conn")
def test_image_search_includes_occurrences(mock_get_conn, mock_conn):
    mock_get_conn.return_value = mock_conn
    from src.mcp_server import attachment_image_search

    result = attachment_image_search(query="dashboard")
    assert result["count"] >= 1
    abc = next(r for r in result["results"] if r["sha256"] == "abc")
    assert len(abc["occurrences"]) == 2
    senders = {o["sender_email"] for o in abc["occurrences"]}
    assert senders == {"alice@example.com", "bob@example.com"}


@patch("src.mcp_server._get_conn")
def test_image_search_no_match(mock_get_conn, mock_conn):
    mock_get_conn.return_value = mock_conn
    from src.mcp_server import attachment_image_search

    result = attachment_image_search(query="nonexistent_xyz")
    assert result["count"] == 0
    assert result["results"] == []
