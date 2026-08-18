import os
import sqlite3
import tempfile
import time
from unittest.mock import patch

import anthropic
import google.auth.exceptions as gauth
import pytest

from src.extract import claude_extract, vertex_auth
from src.llm_policy import ReauthResult


def _create_test_db(db_path):
    """Create a minimal test database with attachments."""
    from src.store.schema import create_database

    conn = create_database(db_path)

    # Insert a test email
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, summary) "
        "VALUES (1000, '2026-01-01', 'Test Email', 'Test summary')"
    )

    # Create a real text file for the attachment
    att_dir = os.path.join(os.path.dirname(db_path), "attachments", "1000")
    os.makedirs(att_dir, exist_ok=True)
    att_path = os.path.join(att_dir, "test.txt")
    with open(att_path, "w") as f:
        f.write(
            "This is a test document with enough text content to pass the extraction threshold easily."
        )

    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size, file_path, exported_at) "
        f"VALUES (1, 1000, 'test.txt', 'text/plain', 100, '{att_path}', '2026-01-01')"
    )
    conn.commit()
    return conn


def test_phase1_extracts_text():
    from src.extract.attachment_pipeline import run_phase1

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        conn = _create_test_db(db_path)
        conn.close()

        stats = run_phase1(db_path, limit=10)
        assert stats["processed"] == 1
        assert stats["extracted"] >= 1

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT extracted_text, extraction_status, extraction_method "
            "FROM attachment_content WHERE attachment_id = 1"
        ).fetchone()
        assert row is not None
        assert row[1] == "extracted"  # extraction_status
        assert "test document" in row[0]  # extracted_text
        conn.close()


def test_phase1_is_resumable():
    """Running phase1 twice should not reprocess already-extracted files."""
    from src.extract.attachment_pipeline import run_phase1

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        conn = _create_test_db(db_path)
        conn.close()

        stats1 = run_phase1(db_path, limit=10)
        assert stats1["processed"] == 1

        stats2 = run_phase1(db_path, limit=10)
        assert stats2["processed"] == 0  # nothing new to process


def test_extract_one_attachment_auth_error_triggers_reauth(monkeypatch):
    """One auth error triggers reauth; the second call succeeds.

    Before the fix: RefreshError is caught by the broad except in
    _extract_one_attachment and serialised to an error string — the policy
    loop never ran, so the auth watcher's sentinel was written for a genuine
    first-try failure even though a retry would have succeeded.
    After the fix: call_with_policy retries after reauth; only a genuine
    terminal failure (after all retries) reaches the broad except.

    running_on_linux is pinned False (macOS path) so decide() chooses
    REAUTH_RETRY instead of giving up immediately with UNRECOVERABLE_AUTH.

    Mutation checks:
    - Remove call_with_policy (bare create): RefreshError caught by broad
      except, error is not None, assert error is None fails.
    - Remove the retry (loop exits after 1): len(calls) == 1, not 2.
    """
    from src.extract.attachment_pipeline import _extract_one_attachment

    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                raise gauth.RefreshError("invalid_grant: Bad Request")
            return type(
                "R",
                (),
                {
                    "stop_reason": "end_turn",
                    "content": [type("C", (), {"text": '{"summary": "ok"}'})()],
                },
            )()

    fake = type("Client", (), {"messages": FakeMessages()})()
    # _extract_one_attachment uses a lazy import of _get_client_and_model from
    # claude_extract, so patching it there is the correct interception point.
    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (fake, "m"))
    monkeypatch.setattr(claude_extract, "running_on_linux", lambda: False)
    monkeypatch.setattr(
        "src.extract.attachment_prompt.build_attachment_prompt",
        lambda **kw: "test prompt",
    )
    monkeypatch.setattr(
        "src.extract.parser.parse_extraction",
        lambda text, **kw: {
            "summary": "ok",
            "language": "en",
            "topics": [],
            "decisions": [],
            "action_items": [],
            "key_facts": [],
        },
    )

    row = (1, 2, "some extracted text", "test.txt", "text/plain", 3, "Test email", "2026-01-01")
    with patch.object(claude_extract, "reauth", return_value=ReauthResult.SUCCEEDED):
        ac_id, email_id, extraction, error, auth_error = _extract_one_attachment(row)

    assert error is None
    assert extraction is not None
    assert auth_error is False
    assert len(calls) == 2


# Every exception type classify_exception routes to AUTH_REAUTH_REQUIRED, plus one that
# it does not. The two anthropic types are built with __new__ because their __init__
# demands a live httpx response; the classifier reads the type, never the body.
_TERMINAL_FAILURES = [
    (gauth.RefreshError("invalid_grant: Bad Request"), "pending"),
    (anthropic.AuthenticationError.__new__(anthropic.AuthenticationError), "pending"),
    (anthropic.PermissionDeniedError.__new__(anthropic.PermissionDeniedError), "pending"),
    (ValueError("bad json on line 5"), "failed"),
]


@pytest.mark.parametrize(("exc", "expected_status"), _TERMINAL_FAILURES)
def test_phase2_defers_every_auth_type_the_policy_calls_reauthable(
    exc, expected_status, monkeypatch, tmp_path
):
    """What survives the retry loop must be persisted the way the classifier judged it.

    ``pending`` and ``failed`` are not two labels for the same thing. run_phase2 selects
    ``WHERE llm_status = 'pending'``, so ``failed`` means never looked at again by any
    later cron — the row needs a human. Writing it for a 401 that a token push would cure
    silently drops the attachment for good.

    Would this pass with the behaviour removed? No, and this is the measured regression.
    Restore the previous ``if is_vertex_auth_error(error)`` — a pattern match over
    ``f"{type(e).__name__}: {e}"`` — and the RefreshError case stays green purely because
    "refresherror" is one of the four patterns and the type name is in the string, while
    AuthenticationError and PermissionDeniedError carry no matching token and both write
    ``failed``: two of the four cases fail. Hardcoding ``auth_error = True`` instead flips
    the ValueError control to ``pending`` and fails the fourth. The sentinel assertions
    catch the same two mutations independently of the status column.
    """
    from src.extract.attachment_pipeline import run_phase1, run_phase2

    sentinel = tmp_path / "needs_gcloud_reauth"
    monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", sentinel)
    # The latch is process-global and this test drives reauth() to FAILED, which sets it.
    # Left behind it would make the next case skip the reauth path it means to exercise.
    claude_extract.reset_reauth_latch()

    class FakeMessages:
        def create(self, **kw):
            raise exc

    fake = type("Client", (), {"messages": FakeMessages()})()
    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (fake, "m"))
    # macOS path: decide() returns REAUTH_RETRY, the stub fails it, and the second auth
    # error hits UNRECOVERABLE_AUTH. Two iterations, no sleeping, exception raised.
    monkeypatch.setattr(claude_extract, "running_on_linux", lambda: False)
    # An exhausted budget, so the ValueError control terminates on the forward-looking
    # budget test instead of sleeping through its API_ERROR backoffs (30s then 60s of real
    # time). The auth cases are untouched: decide()'s macOS auth branch never consults the
    # deadline. Using the real mechanism rather than patching time.sleep keeps the retry
    # loop honest.
    monkeypatch.setenv("PTS_LLM_DEADLINE", repr(time.time()))

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        _create_test_db(db_path).close()
        run_phase1(db_path, limit=10)

        with patch.object(claude_extract, "reauth", return_value=ReauthResult.FAILED):
            run_phase2(db_path, limit=10)

        conn = sqlite3.connect(db_path)
        status = conn.execute(
            "SELECT llm_status FROM attachment_content WHERE attachment_id = 1"
        ).fetchone()[0]
        conn.close()

    claude_extract.reset_reauth_latch()

    assert status == expected_status
    assert sentinel.exists() is (expected_status == "pending")


# Phase 1 cost per attachment spans three orders of magnitude: a plain-text part
# is ~0.1 s, while OCR (the single most common extraction_method on prod, 14,212
# rows) or a 30 MB .xlsb workbook runs for minutes. A count limit therefore says
# nothing about wall-clock, and sb-outlook-sync — TimeoutStartSec=10min — was
# SIGTERMed repeatedly on 2026-08-18 inside a 200-attachment Phase 1 that a
# 60-attachment sample had suggested would take six seconds. Same fix already
# used by run_backfill: a wall-clock budget, checked between items so whatever
# is in flight still finishes and gets committed.


def _db_with_n_attachments(db_path, n):
    from src.store.schema import create_database

    conn = create_database(db_path)
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (1000, '2026-01-01', 'T')"
    )
    att_dir = os.path.join(os.path.dirname(db_path), "attachments", "1000")
    os.makedirs(att_dir, exist_ok=True)
    for i in range(n):
        p = os.path.join(att_dir, f"f{i}.txt")
        with open(p, "w") as f:
            f.write("Body text long enough to clear the extraction threshold comfortably.")
        conn.execute(
            "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
            " file_path, exported_at) VALUES (1, 1000, ?, 'text/plain', 100, ?, '2026-01-01')",
            (f"f{i}.txt", p),
        )
    conn.commit()
    conn.close()


def test_phase1_stops_starting_work_once_the_deadline_passes():
    from src.extract.attachment_pipeline import run_phase1

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        _db_with_n_attachments(db_path, 5)

        stats = run_phase1(db_path, deadline_s=0)

        assert stats["processed"] == 0
        assert stats["deferred"] == 5


def test_phase1_deferred_work_is_picked_up_by_the_next_run():
    """Deferring must not consume the item — the backlog has to converge."""
    from src.extract.attachment_pipeline import run_phase1

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        _db_with_n_attachments(db_path, 3)

        run_phase1(db_path, deadline_s=0)
        stats = run_phase1(db_path)

        assert stats["processed"] == 3
        assert stats["deferred"] == 0


def test_phase1_without_a_deadline_processes_everything():
    """The nightly bulk path must keep its current unbounded behaviour."""
    from src.extract.attachment_pipeline import run_phase1

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        _db_with_n_attachments(db_path, 4)

        stats = run_phase1(db_path)

        assert stats["processed"] == 4
        assert stats["deferred"] == 0


def test_phase2_stops_starting_work_once_the_deadline_passes(monkeypatch):
    """Phase 2 is network-bound: 25 calls looked modest and cost 6-8 minutes of
    wall clock at ~15-25 s each, which is what kept SIGTERMing sb-outlook-sync
    after Phase 1 was already bounded. Count is not a budget; time is."""
    from src.extract import attachment_pipeline

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        _db_with_n_attachments(db_path, 3)
        from src.extract.attachment_pipeline import run_phase1, run_phase2

        run_phase1(db_path)

        called = []
        monkeypatch.setattr(
            attachment_pipeline,
            "_extract_one_attachment",
            lambda row: called.append(row) or (row[0], None, {}, None, False),
        )

        stats = run_phase2(db_path, deadline_s=0)

        assert called == [], "no LLM call may start after the budget is spent"
        assert stats["processed"] == 0
        assert stats["deferred"] == 3


def test_phase2_deferred_work_stays_pending_for_the_next_run():
    """Deferring must leave llm_status='pending' so the nightly drain finds it."""
    from src.extract.attachment_pipeline import run_phase1, run_phase2

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        _db_with_n_attachments(db_path, 2)
        run_phase1(db_path)

        run_phase2(db_path, deadline_s=0)

        conn = sqlite3.connect(db_path)
        pending = conn.execute(
            "SELECT COUNT(*) FROM attachment_content WHERE llm_status = 'pending'"
        ).fetchone()[0]
        conn.close()
        assert pending == 2


def test_phase2_deadline_also_applies_with_concurrent_workers(monkeypatch):
    """The nightly runs --workers 4, i.e. the CONCURRENT branch. Filtering rows
    into a pending list before submitting evaluates the deadline once, at t=0,
    so every row is dispatched and the budget is a no-op — which is why
    sb-attachments ran 35 minutes into a 30-minute cap on 2026-08-19. The
    check has to happen inside the submitted task, as run_backfill already does.
    """
    from src.extract import attachment_pipeline

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        _db_with_n_attachments(db_path, 8)
        from src.extract.attachment_pipeline import run_phase1, run_phase2

        run_phase1(db_path)

        called = []

        # The budget must expire DURING the run, not before it: deadline_s=0 is
        # caught by any pre-flight filter and proves nothing.
        def _slow(row):
            time.sleep(0.15)
            called.append(row)
            return (row[0], None, {}, None, False)

        monkeypatch.setattr(attachment_pipeline, "_extract_one_attachment", _slow)

        stats = run_phase2(db_path, workers=2, deadline_s=0.2)

        assert stats["deferred"] > 0, "budget expired mid-run but nothing was deferred"
        assert len(called) < 8
