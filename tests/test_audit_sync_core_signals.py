"""A stop signal stops a sync, and the extraction state survives an interrupted write.

src.extract.local installed SIGTERM and SIGINT handlers when it was imported and
never removed them. The handler only set a flag that the extraction loops read,
so every later step of a sync ignored a stop: systemd waited out TimeoutStopSec
and SIGKILLed the job, five times in September, turning a finished unit into a
failed one. In the concurrent loop a stop broke out without cancelling the queued
calls, and leaving the executor waited for all of them. The state files it wrote
were written in place and read with a bare json.loads.
"""

import json
import os
import signal
import sqlite3
import threading
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from src.extract import local
from src.extract.extraction_files import extraction_path


@pytest.fixture
def no_stop(monkeypatch):
    """The stop flags are module globals: put them back after each test."""
    monkeypatch.setattr(local, "_shutdown", False)
    monkeypatch.setattr(local, "_stop_requested", False)


@pytest.fixture
def harmless_sigterm():
    """A SIGTERM the code under test fails to catch must not kill the test run."""
    seen: list[int] = []
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: seen.append(signum))
    yield seen
    signal.signal(signal.SIGTERM, previous)


@pytest.fixture
def paths(monkeypatch, tmp_path):
    monkeypatch.setattr(local, "STATE_FILE", tmp_path / "state" / "extract_state.json")
    monkeypatch.setattr(local, "EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(local, "CONV_STATE_FILE", tmp_path / "state" / "conv_extract_state.json")
    monkeypatch.setattr(local, "CONV_EXTRACTED_DIR", tmp_path / "extracted" / "conversations")
    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    monkeypatch.setattr(
        "src.extract.claude_extract._get_client_and_model", lambda: (object(), "model")
    )
    return tmp_path


def _mail(n):
    return {"message_id": f"m{n}", "mailbox_name": "Inbox"}


# ------------------------------------------------------------------ handlers


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_importing_the_module_leaves_the_stop_signals_alone(sig):
    assert signal.getsignal(sig) is not local._handle_signal


def test_an_email_run_holds_the_handlers_only_while_it_runs(
    paths, monkeypatch, no_stop, harmless_sigterm
):
    before = signal.getsignal(signal.SIGTERM)
    during: list = []

    def extract(email, api_key, engine="claude"):
        during.append(signal.getsignal(signal.SIGTERM))
        return email["message_id"], {"summary": "s"}, False, None

    monkeypatch.setattr(local, "collect_emails", lambda: [_mail(0)])
    monkeypatch.setattr(local, "extract_inline", extract)

    local.run_extraction(workers=1)

    assert during == [local._handle_signal]
    assert signal.getsignal(signal.SIGTERM) is before


def test_a_conversation_run_holds_the_handlers_only_while_it_runs(
    paths, monkeypatch, no_stop, harmless_sigterm
):
    before = signal.getsignal(signal.SIGINT)
    during: list = []

    def extract(conv):
        during.append(signal.getsignal(signal.SIGINT))
        return conv["session_id"], {"summary": "s"}, False, None

    monkeypatch.setattr(local, "collect_conversations", lambda: [{"session_id": "s1"}])
    monkeypatch.setattr(local, "extract_conversation_inline", extract)

    local.run_conversation_extraction()

    assert during == [local._handle_signal]
    assert signal.getsignal(signal.SIGINT) is before


def test_a_stop_in_the_concurrent_loop_drops_the_queued_calls(
    paths, monkeypatch, no_stop, harmless_sigterm
):
    """A probe of the old code made all 40 calls. The running ones finish and
    their results are kept; the queued ones never start."""
    calls: list[str] = []
    lock = threading.Lock()

    def extract(email, api_key, engine="claude"):
        with lock:
            calls.append(email["message_id"])
            first = len(calls) == 1
        if first:
            os.kill(os.getpid(), signal.SIGTERM)
        threading.Event().wait(0.05)
        return email["message_id"], {"summary": "s"}, False, None

    monkeypatch.setattr(local, "collect_emails", lambda: [_mail(n) for n in range(40)])
    monkeypatch.setattr(local, "extract_inline", extract)

    result = local.run_extraction(workers=4)

    assert harmless_sigterm == [], "the run's own handler should have caught it"
    assert local.stop_requested()
    assert len(calls) < 20
    assert result["extracted"] == len(calls)


# ------------------------------------------------------------------ state files


def _interrupt_every_write(monkeypatch):
    """Every write that starts from here on dies halfway, as a SIGKILL would."""

    def half_dump(obj, f, **kwargs):
        text = json.dumps(obj)
        f.write(text[: len(text) // 2])
        raise OSError("killed mid-write")

    def half_write_text(self, data, *args, **kwargs):
        with open(self, "w") as f:
            f.write(data[: len(data) // 2])
        raise OSError("killed mid-write")

    monkeypatch.setattr(json, "dump", half_dump)
    monkeypatch.setattr(Path, "write_text", half_write_text)


def test_an_interrupted_state_save_leaves_the_previous_state(paths, monkeypatch):
    local.save_state({"processed_ids": ["m0"]})
    _interrupt_every_write(monkeypatch)

    with pytest.raises(OSError):
        local.save_state({"processed_ids": ["m0", "m1"]})

    monkeypatch.undo()
    assert json.loads(Path(paths / "state" / "extract_state.json").read_text())[
        "processed_ids"
    ] == ["m0"]


def test_an_interrupted_extraction_write_leaves_the_previous_file(paths, monkeypatch, no_stop):
    extracted = paths / "extracted"
    extracted.mkdir()
    extraction_path(extracted, "m0").write_text(json.dumps({"summary": "earlier"}))
    monkeypatch.setattr(local, "collect_emails", lambda: [_mail(0)])
    monkeypatch.setattr(
        local,
        "extract_inline",
        lambda e, k, engine="claude": ("m0", {"summary": "new"}, False, None),
    )
    _interrupt_every_write(monkeypatch)

    with pytest.raises(OSError):
        local.run_extraction(workers=1)

    assert json.loads(extraction_path(extracted, "m0").read_text()) == {"summary": "earlier"}


def test_an_interrupted_conversation_state_save_leaves_the_previous_state(
    paths, monkeypatch, no_stop
):
    state = paths / "state" / "conv_extract_state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"processed_ids": ["earlier"]}))
    monkeypatch.setattr(local, "collect_conversations", lambda: [{"session_id": "s1"}])
    monkeypatch.setattr(
        local, "extract_conversation_inline", lambda c: ("s1", None, False, local.FAULT)
    )
    _interrupt_every_write(monkeypatch)

    with pytest.raises(OSError):
        local.run_conversation_extraction()

    monkeypatch.undo()
    assert json.loads(state.read_text())["processed_ids"] == ["earlier"]


def test_a_truncated_email_state_is_quarantined_not_fatal(paths, monkeypatch, no_stop):
    state = paths / "state" / "extract_state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"processed_ids": ["m0", "m')
    monkeypatch.setattr(local, "collect_emails", lambda: [_mail(0)])
    monkeypatch.setattr(
        local, "extract_inline", lambda e, k, engine="claude": ("m0", {"summary": "s"}, False, None)
    )

    result = local.run_extraction(workers=1)

    assert result["extracted"] == 1
    assert (state.parent / "quarantine" / "extract_state.json").exists()
    assert json.loads(state.read_text())["processed_ids"] == ["m0"]


def test_a_truncated_conversation_state_is_quarantined_not_fatal(paths, monkeypatch, no_stop):
    state = paths / "state" / "conv_extract_state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"processed_ids": ["s0", "s')
    monkeypatch.setattr(local, "collect_conversations", lambda: [{"session_id": "s1"}])
    monkeypatch.setattr(
        local, "extract_conversation_inline", lambda c: ("s1", {"summary": "s"}, False, None)
    )

    local.run_conversation_extraction()

    assert (state.parent / "quarantine" / "conv_extract_state.json").exists()
    assert json.loads(state.read_text())["processed_ids"] == ["s1"]


# ------------------------------------------------------------------ cmd_sync


def _sync(tmp_path, monkeypatch, *, stop_in, exported=0, flags=("_shutdown", "_stop_requested")):
    """cmd_sync with every stage mocked; `flags` are set during `stop_in`."""
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

    def stop(result):
        def run(*args, **kwargs):
            for flag in flags:
                monkeypatch.setattr(local, flag, True)
            return result

        return run

    ok = {"extracted": 0, "failed": 0, "quota_paused": False}
    with (
        patch(
            "src.extract.local.run_extraction",
            side_effect=stop(ok) if stop_in == "emails" else None,
            return_value=ok,
        ),
        patch("src.store.loader.load_extractions", return_value=0) as load,
        patch("src.extract.attachment_pipeline.run_phase1", return_value={"processed": 0}),
        patch(
            "src.export.conversation_export.export_conversations",
            return_value={"exported": exported},
        ),
        patch(
            "src.extract.local.run_conversation_extraction",
            side_effect=stop(None) if stop_in == "conversations" else None,
        ),
        patch("src.store.loader.load_conversations", return_value=0) as load_convs,
        patch("src.extract.image_pipeline.run_backfill", return_value={}) as images,
    ):
        rc = cli.cmd_sync(args)
    conn = sqlite3.connect(db_path)
    last = conn.execute("SELECT value FROM sync_metadata WHERE key = 'last_sync_date'").fetchone()
    conn.close()
    return rc, load, load_convs, images, last[0]


def test_a_stop_during_email_extraction_ends_the_sync(tmp_path, monkeypatch, no_stop):
    rc, load, _, images, last = _sync(tmp_path, monkeypatch, stop_in="emails")

    assert rc == 143
    load.assert_not_called()
    images.assert_not_called()
    assert last == "2026-01-01T00:00:00"


def test_a_stop_during_conversation_extraction_ends_the_sync(tmp_path, monkeypatch, no_stop):
    rc, load, load_convs, images, last = _sync(
        tmp_path, monkeypatch, stop_in="conversations", exported=1
    )

    assert rc == 143
    load.assert_called_once()
    load_convs.assert_not_called()
    images.assert_not_called()
    assert last == "2026-01-01T00:00:00"


def test_an_expired_credential_does_not_end_the_sync(tmp_path, monkeypatch, no_stop):
    """The auth branch ends extraction through _shutdown as well. That is not a
    stop: what was extracted still loads."""
    rc, load, _, images, _ = _sync(tmp_path, monkeypatch, stop_in="emails", flags=("_shutdown",))

    assert rc != 143
    load.assert_called_once()
    images.assert_called_once()
