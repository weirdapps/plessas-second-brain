"""Each Outlook folder keeps its own cursor, and a cursor knows its folder.

The default cursor path ignored --folder, so an operator who followed the docs
and ran the exporter for Sent Items after Inbox shared one file between them:
the Sent run listed from the Inbox cursor and saved its own newest time into
it, and the next Inbox run skipped the mail in between, permanently and with
exit 0. Production escaped only because its wrapper passes a --state-path per
folder. Finding docs-1.
"""

import json
import sys
from unittest.mock import patch

import pytest

from src import config
from src.export import outlook_export
from src.export.state import OutlookSyncState, load_outlook_sync_state, save_outlook_sync_state


def test_default_cursor_paths_match_the_production_names():
    state = config.DATA_ROOT / "state"
    assert outlook_export._default_state_path("Inbox") == state / "outlook_sync.json"
    assert outlook_export._default_state_path("Archive") == state / "outlook_sync_archive.json"
    assert outlook_export._default_state_path("Sent Items") == state / "outlook_sync_sent.json"
    assert outlook_export._default_state_path() == state / "outlook_sync.json"


def test_any_other_folder_gets_a_cursor_of_its_own():
    path = outlook_export._default_state_path("Project X/Drafts")
    assert path.parent == config.DATA_ROOT / "state"
    assert path.name not in (
        "outlook_sync.json",
        "outlook_sync_archive.json",
        "outlook_sync_sent.json",
    )
    assert path.name.startswith("outlook_sync_") and path.suffix == ".json"


def test_main_uses_the_folder_default_when_no_state_path_is_given(monkeypatch):
    seen = {}

    def fake_sync(state_path, folder, **_):
        seen["path"], seen["folder"] = state_path, folder
        return {"messages": 0, "status": "ok"}

    monkeypatch.setattr(outlook_export, "run_hourly_sync", fake_sync)
    # The carry-over reads and renames the repository's own data/state, which
    # is real data on a host that has one; this test is about the path alone.
    monkeypatch.setattr(outlook_export, "_carry_over", lambda path: path)
    monkeypatch.setattr(sys, "argv", ["outlook_export", "--folder", "Sent Items"])

    assert outlook_export.main() == 0
    assert seen["path"] == config.DATA_ROOT / "state" / "outlook_sync_sent.json"
    assert seen["folder"] == "Sent Items"


def test_state_round_trips_its_folder(tmp_path):
    path = tmp_path / "s.json"
    save_outlook_sync_state(path, OutlookSyncState(last_seen_received_at="x", folder="Archive"))
    assert load_outlook_sync_state(path).folder == "Archive"


def _ok_run(mock_cli):
    mock_cli.side_effect = [
        {"ok": True},  # auth-check
        [],  # list-mail: nothing new
    ]


@patch("src.export.outlook_export.run_outlook_cli")
def test_a_cursor_saved_for_another_folder_is_refused(mock_cli, tmp_path):
    path = tmp_path / "shared.json"
    save_outlook_sync_state(
        path, OutlookSyncState(last_seen_received_at="2026-09-27T08:00:00Z", folder="Inbox")
    )
    before = path.read_text()
    _ok_run(mock_cli)

    with pytest.raises(outlook_export.CursorFolderMismatch):
        outlook_export.run_hourly_sync(state_path=path, folder="Sent Items")

    mock_cli.assert_not_called()
    assert path.read_text() == before, "the other folder's cursor must be left untouched"


@patch("src.export.outlook_export.run_outlook_cli")
def test_main_exits_nonzero_on_a_folder_mismatch(mock_cli, tmp_path, monkeypatch):
    path = tmp_path / "shared.json"
    save_outlook_sync_state(
        path, OutlookSyncState(last_seen_received_at="2026-09-27T08:00:00Z", folder="Inbox")
    )
    monkeypatch.setattr(
        sys, "argv", ["outlook_export", "--folder", "Archive", "--state-path", str(path)]
    )
    rc = outlook_export.main()
    assert rc not in (0, 7), "7 would read as 'no cursor' and invite a --bootstrap"
    mock_cli.assert_not_called()


@patch("src.export.outlook_export.run_outlook_cli")
def test_a_cursor_without_a_folder_adopts_the_current_one(mock_cli, tmp_path):
    """Every production cursor predates the field: none may start refusing."""
    path = tmp_path / "outlook_sync_archive.json"
    path.write_text(
        json.dumps({"last_seen_received_at": "2026-09-27T08:00:00Z", "schema_version": 2})
    )
    _ok_run(mock_cli)

    assert outlook_export.run_hourly_sync(state_path=path, folder="Archive")["status"] == "ok"

    assert load_outlook_sync_state(path).folder == "Archive"
    assert mock_cli.call_args_list[1][0][0][:3] == ["list-mail", "--folder", "Archive"]


@patch("src.export.outlook_export.run_outlook_cli")
def test_a_matching_folder_runs(mock_cli, tmp_path):
    path = tmp_path / "s.json"
    save_outlook_sync_state(
        path, OutlookSyncState(last_seen_received_at="2026-09-27T08:00:00Z", folder="Sent Items")
    )
    _ok_run(mock_cli)
    assert outlook_export.run_hourly_sync(state_path=path, folder="Sent Items")["status"] == "ok"
