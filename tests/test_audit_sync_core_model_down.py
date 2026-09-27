"""A sync in which the model failed every email it was sent is neither fresh nor green.

A retired model id (Vertex answers 404), a 400 on every request or a refusal of
everything made every email fail. cmd_sync read only quota_paused from the run,
exited 0 and stamped last_sync_date, so the MCP freshness said age 0 while no
mail entered the store. News is extracted without the model, so `extracted`
stays above zero on such a day and cannot be the test.
"""

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
        if email["message_id"].startswith("ok"):
            return email["message_id"], {"summary": "s"}, False, None
        return email["message_id"], None, False, local.FAULT

    monkeypatch.setattr(local, "extract_inline", extract)

    def stage(*ids):
        news = {"message_id": "news:1", "mailbox_name": "News", "content": "Body"}
        emails = [news, *({"message_id": i, "mailbox_name": "Inbox"} for i in ids)]
        monkeypatch.setattr(local, "collect_emails", lambda: emails)

    return stage


@pytest.mark.parametrize("workers", [1, 3])
def test_the_run_says_how_many_emails_the_model_took_and_failed(staged, workers):
    staged("bad1", "bad2")

    result = local.run_extraction(workers=workers, deadline_s=600.0)

    assert result["extracted"] == 1, "news extracts without the model"
    assert result["model_successes"] == 0
    assert result["model_failures"] == 2


def test_a_run_where_the_model_worked_for_some_says_so(staged):
    staged("ok1", "bad1")

    result = local.run_extraction(workers=1, deadline_s=600.0)

    assert result["model_successes"] == 1
    assert result["model_failures"] == 1


def test_a_run_with_nothing_pending_reports_zero_of_each(staged, monkeypatch):
    monkeypatch.setattr(local, "collect_emails", lambda: [])

    result = local.run_extraction(workers=1)

    assert result["model_successes"] == 0
    assert result["model_failures"] == 0


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
        patch("src.store.loader.load_extractions", return_value=0) as load,
        patch("src.extract.attachment_pipeline.run_phase1", return_value={"processed": 0}),
        patch("src.export.conversation_export.export_conversations", return_value={"exported": 0}),
        patch("src.extract.image_pipeline.run_backfill", return_value={}) as images,
    ):
        rc = cli.cmd_sync(args)
    conn = sqlite3.connect(db_path)
    last = conn.execute("SELECT value FROM sync_metadata WHERE key = 'last_sync_date'").fetchone()
    conn.close()
    return rc, last[0], load, images


def test_a_sync_where_the_model_failed_everything_is_not_fresh_or_green(tmp_path, monkeypatch):
    dead = {
        "extracted": 5,  # news
        "failed": 37,
        "quota_paused": False,
        "model_successes": 0,
        "model_failures": 37,
    }

    rc, last, load, images = _sync(tmp_path, monkeypatch, dead)

    assert rc == 75
    assert last == "2026-01-01T00:00:00"
    load.assert_called_once()
    images.assert_called_once()


def test_a_sync_where_the_model_worked_for_one_email_is_fresh(tmp_path, monkeypatch):
    some = {
        "extracted": 1,
        "failed": 3,
        "quota_paused": False,
        "model_successes": 1,
        "model_failures": 3,
    }

    rc, last, _, _ = _sync(tmp_path, monkeypatch, some)

    assert rc == 0
    assert last != "2026-01-01T00:00:00"


def test_a_sync_that_sent_nothing_to_the_model_is_fresh(tmp_path, monkeypatch):
    quiet = {
        "extracted": 2,
        "failed": 0,
        "quota_paused": False,
        "model_successes": 0,
        "model_failures": 0,
    }

    rc, last, _, _ = _sync(tmp_path, monkeypatch, quiet)

    assert rc == 0
    assert last != "2026-01-01T00:00:00"
