"""A refused or unusable reply proves the model answered, so it is no sign it is down.

A sync in which every email the model was sent failed exits 75 and leaves
last_sync_date alone (cli-2). A refusal came back as the same FAULT as a retired
model id, so a quiet run whose one fresh email was refused read as a dead model:
the hourly load froze the freshness stamp and the 07:05 daily sync went red.
About 14 emails a day are refused, so that run is common. The model answering
three or more and none of it usable still reads as down.
"""

import json
import sqlite3
import types
from unittest.mock import patch

import pytest

from src.extract import local


@pytest.fixture
def staged(monkeypatch, tmp_path):
    monkeypatch.setattr(local, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(local, "EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    monkeypatch.setattr(
        "src.extract.claude_extract._get_client_and_model", lambda: (object(), "model")
    )

    def extract(email, api_key, engine="claude"):
        msg_id = email["message_id"]
        if msg_id.startswith("ok"):
            return msg_id, {"summary": "s"}, False, None
        if msg_id.startswith("refused"):
            return msg_id, None, False, local.UNUSABLE
        return msg_id, None, False, local.FAULT

    monkeypatch.setattr(local, "extract_inline", extract)

    def stage(*ids):
        emails = [{"message_id": i, "mailbox_name": "Inbox"} for i in ids]
        monkeypatch.setattr(local, "collect_emails", lambda: emails)

    return stage


def test_an_unusable_reply_comes_back_as_unusable(monkeypatch):
    def refuse(email, api_key, engine="claude"):
        raise ValueError("empty reply, stop_reason='refusal'")

    monkeypatch.setattr(local, "extract_one", refuse)

    assert local.extract_inline({"message_id": "m"}, None, 1, "claude") == (
        "m",
        None,
        False,
        local.UNUSABLE,
    )


@pytest.mark.parametrize("workers", [1, 3])
def test_a_refused_email_is_an_answer_not_an_outage(staged, workers):
    staged("refused1")

    result = local.run_extraction(workers=workers, deadline_s=600.0)

    assert result["failed"] == 1
    assert result["model_unusable"] == 1
    assert result["model_failures"] == 0


@pytest.mark.parametrize("workers", [1, 3])
def test_an_unusable_reply_counts_toward_the_attempt_cap_like_a_fault(staged, tmp_path, workers):
    staged("ok1", "refused1")

    local.run_extraction(workers=workers, deadline_s=600.0)

    state = json.loads((tmp_path / "state.json").read_text())
    assert state["failed_attempts"] == {"refused1": 1}


def _sync(tmp_path, monkeypatch, extraction_result):
    from src import cli
    from src.store.schema import create_database

    db_path = tmp_path / "brain.db"
    conn = create_database(str(db_path))
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("src.cli.DATA_ROOT", tmp_path)
    args = types.SimpleNamespace(
        db=db_path, limit=None, engine="claude", workers=1, skip_export=True
    )
    with (
        patch("src.extract.local.run_extraction", return_value=extraction_result),
        patch("src.store.loader.load_extractions", return_value=0),
        patch("src.extract.attachment_pipeline.run_phase1", return_value={"processed": 0}),
        patch("src.export.conversation_export.export_conversations", return_value={"exported": 0}),
        patch("src.extract.image_pipeline.run_backfill", return_value={}),
    ):
        rc = cli.cmd_sync(args)
    conn = sqlite3.connect(db_path)
    last = conn.execute("SELECT value FROM sync_metadata WHERE key = 'last_sync_date'").fetchone()
    conn.close()
    return rc, last[0]


def _run(unusable, failures=0):
    return {
        "extracted": 0,
        "failed": unusable + failures,
        "quota_paused": False,
        "model_successes": 0,
        "model_failures": failures,
        "model_unusable": unusable,
    }


def test_a_quiet_run_whose_one_email_was_refused_is_fresh_and_green(tmp_path, monkeypatch):
    rc, last = _sync(tmp_path, monkeypatch, _run(unusable=1))

    assert rc == 0
    assert last != "2026-01-01T00:00:00"


def test_a_run_that_could_use_none_of_three_replies_is_not_green(tmp_path, monkeypatch):
    rc, last = _sync(tmp_path, monkeypatch, _run(unusable=3))

    assert rc == 75
    assert last == "2026-01-01T00:00:00"


def test_a_failure_beside_a_refusal_still_says_the_model_is_down(tmp_path, monkeypatch):
    rc, _ = _sync(tmp_path, monkeypatch, _run(unusable=1, failures=1))

    assert rc == 75


@pytest.mark.parametrize("workers", [1, 3])
def test_refusals_carried_from_an_earlier_run_are_no_sign_of_an_outage(staged, tmp_path, workers):
    # Refused while the model answered others, so refused again they say nothing
    # about the model. Counted, a quiet run that met three of them went red at 75
    # every hour, and without a success they were never counted towards retiring.
    (tmp_path / "state.json").write_text(
        json.dumps({"failed_attempts": {"refused1": 1, "refused2": 1, "refused3": 1}})
    )
    staged("refused1", "refused2", "refused3")

    result = local.run_extraction(workers=workers, deadline_s=600.0)

    assert result["model_unusable"] == 0
    assert result["model_failures"] == 0
