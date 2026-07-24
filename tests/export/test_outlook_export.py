from unittest.mock import patch

import pytest

from src.export.outlook_export import (
    fetch_bodies_concurrent,
    get_one_message_body,
    list_new_messages,
    parse_received_dt,
)
from src.export.state import (
    OutlookSyncState,
    load_outlook_sync_state,
    save_outlook_sync_state,
)


@patch("src.export.outlook_export.run_outlook_cli")
def test_list_new_messages_passes_since_when_state_has_cursor(mock_cli):
    mock_cli.return_value = []
    state = OutlookSyncState(last_seen_received_at="2026-04-22T07:00:00Z")
    list_new_messages(state, folder="Inbox", max_results=5000)
    args = mock_cli.call_args[0][0]
    assert "--since" in args
    assert "2026-04-22T07:00:00Z" in args
    assert "--all" in args
    assert "--max" in args
    assert "5000" in args


@patch("src.export.outlook_export.run_outlook_cli")
def test_list_new_messages_refuses_silent_bootstrap(mock_cli):
    """Without allow_bootstrap=True, missing-cursor must raise rather than
    silently fetch only the most-recent 100 messages (data-loss footgun)."""
    from src.export.outlook_export import BootstrapRequired

    mock_cli.return_value = []
    state = OutlookSyncState()
    with pytest.raises(BootstrapRequired):
        list_new_messages(state, folder="Inbox", max_results=5000)
    mock_cli.assert_not_called()


@patch("src.export.outlook_export.run_outlook_cli")
def test_list_new_messages_bootstraps_when_explicitly_allowed(mock_cli):
    mock_cli.return_value = []
    state = OutlookSyncState()
    list_new_messages(state, folder="Inbox", max_results=5000, allow_bootstrap=True)
    args = mock_cli.call_args[0][0]
    assert "--since" not in args
    assert "--top" in args  # bootstrap: just top N most recent


def test_parse_received_dt_handles_iso():
    assert parse_received_dt("2026-04-22T13:58:14Z") < parse_received_dt("2026-04-22T13:58:15Z")


@patch("src.export.outlook_export.run_outlook_cli")
def test_fetch_bodies_concurrent_returns_results_for_all_ids(mock_cli):
    mock_cli.side_effect = [
        {
            "Id": "a",
            "Body": {"Content": "hi-a"},
            "ReceivedDateTime": "2026-04-22T01:00:00Z",
        },
        {
            "Id": "b",
            "Body": {"Content": "hi-b"},
            "ReceivedDateTime": "2026-04-22T02:00:00Z",
        },
        {
            "Id": "c",
            "Body": {"Content": "hi-c"},
            "ReceivedDateTime": "2026-04-22T03:00:00Z",
        },
    ]
    results = list(fetch_bodies_concurrent(["a", "b", "c"], concurrency=2))
    assert {r["Id"] for r in results if r} == {"a", "b", "c"}


@patch("src.export.outlook_export.run_outlook_cli")
def test_fetch_bodies_concurrent_aborts_on_auth_required(mock_cli):
    from src.export.outlook_cli import OutlookCliAuthRequired

    mock_cli.side_effect = [
        {
            "Id": "a",
            "Body": {"Content": "hi"},
            "ReceivedDateTime": "2026-04-22T01:00:00Z",
        },
        OutlookCliAuthRequired("expired"),
    ]
    with pytest.raises(OutlookCliAuthRequired):
        list(fetch_bodies_concurrent(["a", "b", "c"], concurrency=1))


@patch("src.export.outlook_export.run_outlook_cli")
def test_get_one_message_body_returns_valid_message(mock_cli):
    mock_cli.return_value = {
        "Id": "a",
        "ReceivedDateTime": "2026-04-22T01:00:00Z",
        "Body": {"Content": "x"},
    }
    msg = get_one_message_body("a")
    assert msg is not None and msg["Id"] == "a"


@patch("src.export.outlook_export.run_outlook_cli")
def test_get_one_message_body_skips_incomplete(mock_cli):
    # outlook-cli occasionally returns a small object lacking ReceivedDateTime;
    # it must be skipped (None), not crash the run and wedge the cursor.
    mock_cli.return_value = {"status": "partial"}
    assert get_one_message_body("a") is None


@patch("src.export.outlook_export.run_outlook_cli")
def test_get_one_message_body_skips_unparseable(mock_cli):
    import json

    mock_cli.side_effect = json.JSONDecodeError("Extra data", "{}x", 2)
    assert get_one_message_body("a") is None


@patch("src.export.outlook_export.commit_messages_to_db")
@patch("src.export.outlook_export.run_outlook_cli")
def test_orchestrator_does_not_advance_cursor_if_db_commit_fails(
    mock_cli,
    mock_commit,
    tmp_path,
):
    state_path = tmp_path / "state.json"
    save_outlook_sync_state(
        state_path,
        OutlookSyncState(
            last_seen_received_at="2026-04-22T00:00:00Z",
        ),
    )
    mock_cli.side_effect = [
        {"ok": True},  # auth-check
        [{"Id": "a", "ReceivedDateTime": "2026-04-22T01:00:00Z"}],  # list-mail
        {
            "Id": "a",
            "Body": {"Content": "x"},
            "ReceivedDateTime": "2026-04-22T01:00:00Z",
        },
    ]
    mock_commit.side_effect = RuntimeError("DB blew up")

    from src.export.outlook_export import run_hourly_sync

    with pytest.raises(RuntimeError):
        run_hourly_sync(state_path=state_path)

    state = load_outlook_sync_state(state_path)
    assert state.last_seen_received_at == "2026-04-22T00:00:00Z"  # unchanged
    assert state.consecutive_failures == 1
