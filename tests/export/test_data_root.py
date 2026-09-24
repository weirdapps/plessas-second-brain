"""The Outlook exporter and the Inbox reconcile keep their data under BRAIN_DATA_DIR.

They built their paths from the repository (repo/data/...) while extraction and
load read src.config's DATA_ROOT, which BRAIN_DATA_DIR moves, and DEPLOY.md
recommends setting it. On such a host the staged mail went where nothing read
it. tests/conftest.py points BRAIN_DATA_DIR at a temporary directory, so
DATA_ROOT here is not the repository's data/.
"""

import shutil
import sys

from src import config
from src.export import inbox_reconcile, outlook_export


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


def _legacy_cursor(tmp_path, monkeypatch, text):
    """A cursor where the exporter used to keep it: the repository's data/."""
    repo = tmp_path / "repo"
    legacy = repo / "data" / "state" / "outlook_sync.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(text, encoding="utf-8")
    monkeypatch.setattr(outlook_export, "REPO_ROOT", repo)
    target = config.DATA_ROOT / "state" / "outlook_sync.json"
    target.unlink(missing_ok=True)
    return legacy, target


def test_a_cursor_left_in_the_repository_is_carried_over(tmp_path, monkeypatch):
    """A host that set BRAIN_DATA_DIR kept its cursor in the repository. Without
    it the next run exits 7, and the bootstrap that answers that fetches only
    the newest 100 messages."""
    legacy, target = _legacy_cursor(tmp_path, monkeypatch, '{"Inbox": {"delta": "x"}}')

    assert outlook_export._default_state_path() == target
    assert target.read_text(encoding="utf-8") == '{"Inbox": {"delta": "x"}}'
    assert legacy.exists()


def test_a_cursor_already_in_the_data_root_is_not_overwritten(tmp_path, monkeypatch):
    legacy, target = _legacy_cursor(tmp_path, monkeypatch, '{"Inbox": {"delta": "old"}}')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"Inbox": {"delta": "new"}}', encoding="utf-8")

    assert outlook_export._default_state_path() == target
    assert target.read_text(encoding="utf-8") == '{"Inbox": {"delta": "new"}}'
