"""Tests for the outlook_live_search MCP tool."""

from unittest.mock import patch

from src.mcp_server import outlook_live_search


@patch("src.export.outlook_cli.run_outlook_cli")
def test_live_search_filters_by_subject(mock_cli):
    mock_cli.return_value = [
        {"Id": "1", "Subject": "Urgent — please review"},
        {"Id": "2", "Subject": "FYI"},
    ]
    result = outlook_live_search(subject_contains="urgent")
    assert result["count"] == 1
    assert result["messages"][0]["Id"] == "1"


@patch("src.export.outlook_cli.run_outlook_cli")
def test_live_search_returns_all_when_no_filter(mock_cli):
    mock_cli.return_value = [
        {"Id": "1", "Subject": "A"},
        {"Id": "2", "Subject": "B"},
    ]
    result = outlook_live_search()
    assert result["count"] == 2


@patch("src.export.outlook_cli.run_outlook_cli")
def test_live_search_passes_folder_and_since(mock_cli):
    mock_cli.return_value = []
    outlook_live_search(folder="Sent Items", since_minutes=30)
    args = mock_cli.call_args[0][0]
    assert "list-mail" in args
    assert "--folder" in args
    assert "Sent Items" in args
    assert "--since" in args
    assert "--all" in args
