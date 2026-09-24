"""The Outlook exporter and the Inbox reconcile keep their data under BRAIN_DATA_DIR.

They built their paths from the repository (repo/data/...) while extraction and
load read src.config's DATA_ROOT, which BRAIN_DATA_DIR moves, and DEPLOY.md
recommends setting it. On such a host the staged mail went where nothing read
it. tests/conftest.py points BRAIN_DATA_DIR at a temporary directory, so
DATA_ROOT here is not the repository's data/.
"""

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
