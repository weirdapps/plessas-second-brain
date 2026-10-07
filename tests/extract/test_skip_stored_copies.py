"""A staged copy of an email the store already holds never reaches the model.

Moving a message to Archive gives it a new Graph id, and the Archive export stages
it under that id. Extraction filtered pending work by message_id alone, so the
model extracted the copy, and the loader then dropped it as a duplicate of the
stored email (same RFC822 Message-ID): 11,636 wasted extractions by 2026-10-07,
about 100 a day. The copy is now skipped, and left staged for the load, which
notes its move and its alias (src/store/loader.py).
"""

import pytest

from src.extract import local
from src.store.loader import load_single_email
from src.store.schema import create_database

STORED = {
    "message_id": "AAMk-inbox-1",
    "internet_message_id": "<one@example.com>",
    "date_received": "2026-10-01T08:00:00Z",
    "subject": "s",
    "mailbox_name": "Inbox",
    "content": "x",
}


@pytest.fixture
def run(monkeypatch, tmp_path):
    """run(emails, db_path=...): one scheduled extraction run over `emails`."""
    monkeypatch.setattr(local, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(local, "EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    monkeypatch.setattr(
        "src.extract.claude_extract._get_client_and_model", lambda: (object(), "model")
    )
    calls: list[str] = []

    def extract(email, api_key, engine="claude"):
        calls.append(email["message_id"])
        return email["message_id"], {"summary": "s"}, False, None

    monkeypatch.setattr(local, "extract_inline", extract)

    def go(emails, workers=1, **kwargs):
        monkeypatch.setattr(local, "collect_emails", lambda: emails)
        return local.run_extraction(workers=workers, deadline_s=600.0, **kwargs)

    go.calls = calls
    go.log = lambda: (tmp_path / "extract.log").read_text()
    go.state = lambda: local.load_state()
    return go


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "b.db"
    conn = create_database(str(path))
    assert load_single_email(conn, STORED, {"summary": "s"})
    conn.commit()
    conn.close()
    return path


def _mail(message_id, imid=None, mailbox="Archive"):
    return {"message_id": message_id, "internet_message_id": imid, "mailbox_name": mailbox}


@pytest.mark.parametrize("workers", [1, 3])
def test_a_copy_of_a_stored_email_is_not_extracted(run, db, workers):
    run(
        [_mail("AAMk-archive-1", "<one@example.com>"), _mail("new", "<new@example.com>")],
        workers,
        db_path=db,
    )

    assert run.calls == ["new"]
    # Left pending: the load notes its move and alias, and prunes its batch.
    assert "AAMk-archive-1" not in run.state()["processed_ids"]
    assert "1 staged copies of emails already stored" in run.log()


def test_an_email_stored_under_its_own_id_is_not_extracted_again(run, db):
    """A lost or quarantined state file must not send the stored mail back to the model."""
    run([_mail("AAMk-inbox-1", "<one@example.com>", mailbox="Inbox")], db_path=db)

    assert run.calls == []


def test_two_copies_of_one_new_message_go_to_the_model_once(run, db):
    run(
        [
            _mail("AAMk-inbox-2", "<two@example.com>", "Inbox"),
            _mail("AAMk-archive-2", "<two@example.com>"),
        ],
        db_path=db,
    )

    assert run.calls == ["AAMk-inbox-2"]


def test_a_copy_waits_while_another_copy_is_extracted_and_not_yet_loaded(run, db):
    run([_mail("AAMk-inbox-3", "<three@example.com>", "Inbox")], db_path=db)
    run.calls.clear()

    run(
        [
            _mail("AAMk-inbox-3", "<three@example.com>", "Inbox"),
            _mail("AAMk-archive-3", "<three@example.com>"),
        ],
        db_path=db,
    )

    assert run.calls == []


def test_a_copy_is_not_held_behind_one_whose_extraction_was_lost(run, db, tmp_path):
    """Recorded as processed, with no file on disk, the first copy never loads
    (scripts/recover_missing_extractions.py); the second must not wait for it."""
    run([_mail("AAMk-inbox-4", "<four@example.com>", "Inbox")], db_path=db)
    for path in (tmp_path / "extracted").glob("AAMk-inbox-4*"):
        path.unlink()
    run.calls.clear()

    run(
        [
            _mail("AAMk-inbox-4", "<four@example.com>", "Inbox"),
            _mail("AAMk-archive-4", "<four@example.com>"),
        ],
        db_path=db,
    )

    assert run.calls == ["AAMk-archive-4"]


def test_without_a_database_every_pending_email_is_extracted(run, tmp_path):
    run([_mail("a", "<a@example.com>"), _mail("b", None)], db_path=tmp_path / "absent.db")

    assert sorted(run.calls) == ["a", "b"]


def test_sync_hands_its_database_to_extraction(tmp_path, monkeypatch):
    from unittest.mock import patch

    from src import cli

    db_path = tmp_path / "brain.db"
    conn = create_database(str(db_path))
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("src.cli.DATA_ROOT", tmp_path)
    args = type(
        "Args",
        (),
        {"db": db_path, "limit": None, "engine": "claude", "workers": 1, "skip_export": True},
    )()
    ok = {"extracted": 0, "failed": 0, "quota_paused": False, "model_successes": 0}
    with (
        patch("src.extract.local.run_extraction", return_value=ok) as extraction,
        patch("src.store.loader.load_extractions", return_value=0),
        patch("src.extract.attachment_pipeline.run_phase1", return_value={"processed": 0}),
        patch("src.export.conversation_export.export_conversations", return_value={"exported": 0}),
        patch("src.extract.image_pipeline.run_backfill", return_value={}),
    ):
        cli.cmd_sync(args)

    assert extraction.call_args.kwargs["db_path"] == str(db_path)
