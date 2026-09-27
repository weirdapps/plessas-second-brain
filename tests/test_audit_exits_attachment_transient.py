"""A transient phase-2 failure stays pending (attachments-2).

Only an auth failure was offered again. A connection drop, a 5xx, a timeout or
a 429 that outlasted call_with_policy's retries was written llm_status='failed',
which is terminal because run_phase2 re-selects only 'pending', so an outage
during the nightly pass dropped the summaries of the whole queue for good. The
calendar path in cli.py already splits it this way.
"""

import os
import sqlite3

import google.auth.exceptions as gauth
import pytest

from src.extract import claude_extract, vertex_auth


def _db_with_one_attachment(tmp_path) -> str:
    from src.store.schema import create_database

    db_path = str(tmp_path / "test.db")
    conn = create_database(db_path)
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (1000, '2026-01-01', 'T')"
    )
    att_dir = tmp_path / "attachments" / "1000"
    att_dir.mkdir(parents=True)
    path = att_dir / "notes.txt"
    path.write_text("Body text long enough to clear the extraction threshold comfortably.")
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at) VALUES (1, 1000, 'notes.txt', 'text/plain', 100, ?, "
        "'2026-01-01')",
        (os.fspath(path),),
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def phase2(tmp_path, monkeypatch):
    from src.extract.attachment_pipeline import run_phase1

    monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", tmp_path / "needs_gcloud_reauth")
    db_path = _db_with_one_attachment(tmp_path)
    run_phase1(db_path)
    return db_path


def _row(db_path):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT llm_status, llm_error FROM attachment_content").fetchone()
    conn.close()
    return row


def _raise(exc):
    def _complete(**kw):
        raise exc

    return _complete


# The network dropping, raised as the builtin and as google-auth's own type,
# which is how a drop while refreshing the Vertex token arrives.
_TRANSIENT = [
    ConnectionResetError("connection reset by peer"),
    gauth.TransportError("token endpoint unreachable"),
]


@pytest.mark.parametrize("exc", _TRANSIENT)
def test_a_transient_failure_stays_pending_and_counts_as_failed(exc, phase2, monkeypatch):
    from src.extract.attachment_pipeline import run_phase2

    monkeypatch.setattr(claude_extract, "complete", _raise(exc))

    stats = run_phase2(phase2)

    status, error = _row(phase2)
    assert status == "pending"
    assert type(exc).__name__ in error
    assert stats["failed"] == 1
    assert stats["extracted"] == 0


def test_the_pending_row_is_offered_again_next_run(phase2, monkeypatch):
    from src.extract.attachment_pipeline import run_phase2

    monkeypatch.setattr(claude_extract, "complete", _raise(ConnectionResetError("reset")))
    run_phase2(phase2)

    assert run_phase2(phase2)["processed"] == 1


def test_a_transient_failure_does_not_touch_the_reauth_sentinel(phase2, monkeypatch, tmp_path):
    from src.extract.attachment_pipeline import run_phase2

    monkeypatch.setattr(claude_extract, "complete", _raise(ConnectionResetError("reset")))
    run_phase2(phase2)

    assert not (tmp_path / "needs_gcloud_reauth").exists()


def test_an_item_fault_is_still_terminal(phase2, monkeypatch):
    from src.extract.attachment_pipeline import run_phase2

    monkeypatch.setattr(claude_extract, "complete", _raise(ValueError("bad json on line 5")))

    stats = run_phase2(phase2)

    assert _row(phase2)[0] == "failed"
    assert stats["failed"] == 1


def test_an_item_that_runs_out_of_time_is_still_terminal(phase2, monkeypatch):
    """The same request is likely to time out again (policy_bridge.is_item_timeout).
    attachment_content keeps no attempt count to cap it with, so left pending it
    would be sent again every night, and with nothing else extracted it would turn
    the nightly stage red every night for good."""
    from src.extract.attachment_pipeline import run_phase2

    monkeypatch.setattr(claude_extract, "complete", _raise(TimeoutError("the model took too long")))

    stats = run_phase2(phase2)

    assert _row(phase2)[0] == "failed"
    assert stats["failed"] == 1
