"""Tests for Vertex AI / gcloud ADC auth-expiry detection.

Two layers:
  1. Helper unit tests (is_vertex_auth_error, touch_sentinel) — direct.
  2. Persistence-site contract tests (attachment + teams) — inline the deferral
     branch to verify the SQL UPDATE shape without spinning up the full pipeline
     harness. Verifies the *contract* between the helper and the DB write.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime


class TestIsVertexAuthError:
    def test_matches_reauthentication_string(self):
        from src.extract.vertex_auth import is_vertex_auth_error

        err = Exception("RefreshError: ('Reauthentication is needed', None)")
        assert is_vertex_auth_error(err)

    def test_matches_application_default_login(self):
        from src.extract.vertex_auth import is_vertex_auth_error

        err = "Please run gcloud auth application-default login"
        assert is_vertex_auth_error(err)

    def test_matches_invalid_grant(self):
        from src.extract.vertex_auth import is_vertex_auth_error

        err = Exception("invalid_grant: Token has been expired or revoked.")
        assert is_vertex_auth_error(err)

    def test_matches_refresherror_class_name(self):
        from src.extract.vertex_auth import is_vertex_auth_error

        err = Exception("google.auth.exceptions.RefreshError: token error")
        assert is_vertex_auth_error(err)

    def test_case_insensitive(self):
        from src.extract.vertex_auth import is_vertex_auth_error

        assert is_vertex_auth_error("REAUTHENTICATION IS NEEDED")

    def test_returns_false_for_unrelated_errors(self):
        from src.extract.vertex_auth import is_vertex_auth_error

        for msg in [
            "AttributeError: 'NoneType' object has no attribute 'foo'",
            "Connection timeout",
            "404 Not Found",
            "json decode error at line 12",
            "max_tokens reached",
        ]:
            assert not is_vertex_auth_error(Exception(msg)), msg


class TestTouchSentinel:
    def test_creates_sentinel_file(self, tmp_path, monkeypatch):
        from src.extract import vertex_auth

        fake = tmp_path / "needs_gcloud_reauth"
        monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", fake)
        assert not fake.exists()
        vertex_auth.touch_sentinel()
        assert fake.exists()

    def test_idempotent(self, tmp_path, monkeypatch):
        from src.extract import vertex_auth

        fake = tmp_path / "needs_gcloud_reauth"
        monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", fake)
        vertex_auth.touch_sentinel()
        vertex_auth.touch_sentinel()  # must not raise
        assert fake.exists()

    def test_creates_parent_dir_if_missing(self, tmp_path, monkeypatch):
        from src.extract import vertex_auth

        fake = tmp_path / "subdir_does_not_exist" / "needs_gcloud_reauth"
        monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", fake)
        vertex_auth.touch_sentinel()
        assert fake.exists()


class TestAttachmentPipelineDefersOnAuthError:
    """Auth-error must mark llm_status='pending' (not 'failed') AND touch sentinel."""

    def test_auth_error_defers_and_touches_sentinel(self, tmp_path, monkeypatch):
        from src.extract import vertex_auth
        from src.extract.vertex_auth import is_vertex_auth_error, touch_sentinel

        fake = tmp_path / "needs_gcloud_reauth"
        monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", fake)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE attachment_content (
                id INTEGER PRIMARY KEY,
                summary TEXT, language TEXT,
                llm_status TEXT, llm_error TEXT, llm_extracted_at TEXT
            );
            INSERT INTO attachment_content (id, llm_status) VALUES (1, 'pending');
            """
        )

        # Mirror the production branch from attachment_pipeline._store_result.
        ac_id = 1
        error = "RefreshError: ('Reauthentication is needed', None)"
        now = datetime.now().isoformat()
        if error and is_vertex_auth_error(error):
            touch_sentinel()
            conn.execute(
                """UPDATE attachment_content
                   SET llm_status = 'pending', llm_error = ?, llm_extracted_at = ?
                   WHERE id = ?""",
                (
                    f"deferred (gcloud reauth needed): {str(error)[:200]}",
                    now,
                    ac_id,
                ),
            )
        conn.commit()

        row = conn.execute(
            "SELECT llm_status, llm_error FROM attachment_content WHERE id=1"
        ).fetchone()
        assert row["llm_status"] == "pending"
        assert "deferred (gcloud reauth needed)" in row["llm_error"]
        assert fake.exists()


class TestTeamsPipelineDefersOnAuthError:
    """Auth-error must mark extraction_status='pending' (not 'failed') AND touch sentinel."""

    def test_auth_error_defers_and_touches_sentinel(self, tmp_path, monkeypatch):
        from src.extract import vertex_auth
        from src.extract.vertex_auth import is_vertex_auth_error, touch_sentinel

        fake = tmp_path / "needs_gcloud_reauth"
        monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", fake)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE teams_threads (
                id INTEGER PRIMARY KEY,
                extraction_status TEXT, extraction_error TEXT
            );
            INSERT INTO teams_threads (id, extraction_status) VALUES (10, 'pending');
            """
        )

        thread_id = 10
        e = Exception("RefreshError: ('Reauthentication is needed', None)")
        if is_vertex_auth_error(e):
            touch_sentinel()
            conn.execute(
                "UPDATE teams_threads SET extraction_status='pending', "
                "extraction_error=? WHERE id = ?",
                (f"deferred (gcloud reauth needed): {str(e)[:400]}", thread_id),
            )
        conn.commit()

        row = conn.execute(
            "SELECT extraction_status, extraction_error FROM teams_threads WHERE id=10"
        ).fetchone()
        assert row["extraction_status"] == "pending"
        assert "deferred" in row["extraction_error"]
        assert fake.exists()


class TestNonAuthErrorsStillFail:
    """Sanity check: only auth errors should defer; everything else still hard-fails."""

    def test_random_error_does_not_match(self):
        from src.extract.vertex_auth import is_vertex_auth_error

        # If this slips through and gets deferred we'd lose visibility into real bugs.
        assert not is_vertex_auth_error("ValueError: bad json on line 5")
        assert not is_vertex_auth_error(TimeoutError("read timeout after 60s"))
        assert not is_vertex_auth_error("HTTP 500 from Vertex backend")
