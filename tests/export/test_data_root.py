"""The Outlook exporter and the Inbox reconcile keep their data under BRAIN_DATA_DIR.

They built their paths from the repository (repo/data/...) while extraction and
load read src.config's DATA_ROOT, which BRAIN_DATA_DIR moves, and DEPLOY.md
recommends setting it. On such a host the staged mail went where nothing read
it. tests/conftest.py points BRAIN_DATA_DIR at a temporary directory, so
DATA_ROOT here is not the repository's data/.
"""

import json
import shutil
import sys

import pytest

from src import config
from src.export import inbox_reconcile, outlook_export


@pytest.fixture(autouse=True)
def _only_against_the_test_data_home():
    """These tests write, delete and overwrite under DATA_ROOT. conftest points it
    at a temporary directory unless BRAIN_DATA_DIR is already exported, and then
    it is someone's real data home: its attachments, cursors and staging."""
    if not config.DATA_ROOT.name.startswith("brain-test-data-"):
        pytest.skip("refuses to run against a real data home (BRAIN_DATA_DIR is exported)")


def test_outlook_batches_are_staged_under_the_data_root():
    message = {
        "Id": "AAMkexample",
        "Subject": "s",
        "ReceivedDateTime": "2026-09-24T07:00:00Z",
        "From": {"EmailAddress": {"Name": "A", "Address": "a@example.com"}},
    }

    batch = outlook_export.commit_messages_to_db([message], folder="Inbox")

    assert batch.parent == config.DATA_ROOT / "staging"


def test_attachments_default_under_the_data_root():
    # Removed first: another test may have made it, and the check would pass
    # whatever the exporter did.
    shutil.rmtree(config.ATTACHMENTS_DIR, ignore_errors=True)

    outlook_export.download_attachments_for_messages([])

    assert config.ATTACHMENTS_DIR.is_dir()


def test_the_sync_cursor_defaults_under_the_data_root():
    assert outlook_export._default_state_path() == config.DATA_ROOT / "state" / "outlook_sync.json"


def test_the_reconcile_reads_the_configured_database(monkeypatch):
    seen = []
    monkeypatch.setattr(sys, "argv", ["inbox_reconcile"])
    monkeypatch.setattr(inbox_reconcile.Path, "exists", lambda self: seen.append(self) or False)

    assert inbox_reconcile.main() == 1
    assert seen == [config.DEFAULT_DB]


def _cursor(received_at):
    return json.dumps({"last_seen_received_at": received_at, "last_seen_message_id": "m"})


def _legacy_cursor(tmp_path, monkeypatch, received_at, name="outlook_sync.json"):
    """A cursor where the exporter used to keep it: the repository's data/."""
    repo = tmp_path / "repo"
    legacy = repo / "data" / "state" / name
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(_cursor(received_at), encoding="utf-8")
    monkeypatch.setattr(outlook_export, "REPO_ROOT", repo)
    target = config.DATA_ROOT / "state" / name
    target.unlink(missing_ok=True)
    return legacy, target


def _run(monkeypatch, *args):
    """main() with these arguments; returns the cursor the sync would start from."""
    seen = {}

    def sync(state_path, **kwargs):
        seen["cursor"] = outlook_export.load_outlook_sync_state(state_path).last_seen_received_at
        return {}

    monkeypatch.setattr(outlook_export, "run_hourly_sync", sync)
    monkeypatch.setattr(sys, "argv", ["outlook_export", *args])
    assert outlook_export.main() == 0
    return seen["cursor"]


def test_a_cursor_left_in_the_repository_is_carried_over(tmp_path, monkeypatch):
    """A host that set BRAIN_DATA_DIR kept its cursor in the repository. Without
    it the next run exits 7, and the bootstrap that answers that fetches only
    the newest 100 messages."""
    legacy, target = _legacy_cursor(tmp_path, monkeypatch, "2026-09-01T00:00:00Z")

    assert _run(monkeypatch, "--folder", "Inbox") == "2026-09-01T00:00:00Z"
    assert json.loads(target.read_text())["last_seen_received_at"] == "2026-09-01T00:00:00Z"
    # Kept, renamed, so a rollback still has it and a later reset cannot.
    assert not legacy.exists()
    assert legacy.with_name(legacy.name + ".carried").exists()


def test_the_carry_over_happens_once(tmp_path, monkeypatch):
    """Deleting the cursor later is how a folder is bootstrapped again; a legacy
    file still in place would bring back a months-old cursor instead."""
    legacy, target = _legacy_cursor(tmp_path, monkeypatch, "2026-09-01T00:00:00Z")
    _run(monkeypatch, "--folder", "Inbox")
    target.unlink()

    assert _run(monkeypatch, "--folder", "Inbox") is None


def test_a_legacy_file_that_is_not_a_cursor_is_ignored(tmp_path, monkeypatch):
    legacy, target = _legacy_cursor(tmp_path, monkeypatch, "2026-09-01T00:00:00Z")
    legacy.write_text("null", encoding="utf-8")

    assert _run(monkeypatch, "--folder", "Inbox") is None


def test_the_wrappers_explicit_paths_are_carried_over_too(tmp_path, monkeypatch):
    """The wrapper passes --state-path for every folder, so a carry-over only on
    the default path never ran; Archive and Sent Items would bootstrap."""
    legacy, target = _legacy_cursor(
        tmp_path, monkeypatch, "2026-09-02T00:00:00Z", name="outlook_sync_archive.json"
    )

    cursor = _run(monkeypatch, "--folder", "Archive", "--state-path", str(target))

    assert cursor == "2026-09-02T00:00:00Z"


def test_a_cursorless_file_left_by_a_failed_run_counts_as_none(tmp_path, monkeypatch):
    """A failed run saves its state without a cursor, and an existence check then
    kept the carry-over off for good."""
    legacy, target = _legacy_cursor(tmp_path, monkeypatch, "2026-09-01T00:00:00Z")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"last_seen_received_at": None, "consecutive_failures": 2}))

    assert _run(monkeypatch, "--folder", "Inbox") == "2026-09-01T00:00:00Z"


def test_a_cursor_already_in_the_data_root_is_not_overwritten(tmp_path, monkeypatch):
    legacy, target = _legacy_cursor(tmp_path, monkeypatch, "2026-09-01T00:00:00Z")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_cursor("2026-09-20T00:00:00Z"), encoding="utf-8")

    assert _run(monkeypatch, "--folder", "Inbox") == "2026-09-20T00:00:00Z"


def test_a_path_outside_the_data_root_is_left_alone(tmp_path, monkeypatch):
    legacy, _target = _legacy_cursor(tmp_path, monkeypatch, "2026-09-01T00:00:00Z")
    elsewhere = tmp_path / "elsewhere" / "outlook_sync.json"

    assert _run(monkeypatch, "--folder", "Inbox", "--state-path", str(elsewhere)) is None
