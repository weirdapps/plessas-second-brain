import os
import sqlite3
import tempfile
from unittest.mock import patch

import google.auth.exceptions as gauth

from src.extract import claude_extract
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
            return type("R", (), {
                "stop_reason": "end_turn",
                "content": [type("C", (), {"text": '{"summary": "ok"}'})()],
            })()

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
            "summary": "ok", "language": "en",
            "topics": [], "decisions": [], "action_items": [], "key_facts": [],
        },
    )

    row = (1, 2, "some extracted text", "test.txt", "text/plain", 3, "Test email", "2026-01-01")
    with patch.object(claude_extract, "reauth", return_value=ReauthResult.SUCCEEDED):
        ac_id, email_id, extraction, error = _extract_one_attachment(row)

    assert error is None
    assert extraction is not None
    assert len(calls) == 2
