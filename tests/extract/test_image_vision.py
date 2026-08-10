import sqlite3
from pathlib import Path
from unittest.mock import patch

import google.auth.exceptions as gauth

from src.extract import claude_extract
from src.extract.image_classifier import Classification
from src.extract.image_vision import classify_with_vision, parse_vision_response
from src.llm_policy import ReauthResult
from src.store.schema import create_database, run_migrations


def _setup_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    conn = create_database(str(db_path))
    run_migrations(conn)
    return conn


def test_parse_vision_response_content():
    label, desc = parse_vision_response("CONTENT: bar chart of revenue by region")
    assert label == Classification.CONTENT
    assert "bar chart" in desc


def test_parse_vision_response_decoration():
    label, desc = parse_vision_response("DECORATION: ACME logo")
    assert label == Classification.SIGNATURE
    assert "ACME logo" in desc


def test_parse_vision_response_invalid_falls_back_to_signature():
    label, desc = parse_vision_response("garbage output")
    assert label == Classification.SIGNATURE  # safe default — don't enrich extractor with junk


@patch("src.extract.claude_extract._get_client_and_model")
def test_classify_with_vision_uses_cache(mock_get_client, tmp_path):
    from PIL import Image

    img = tmp_path / "x.png"
    Image.new("RGB", (800, 600), "blue").save(img)
    db = _setup_db(tmp_path)
    from src.extract.image_classifier import sha256_of_file

    sha = sha256_of_file(img)
    db.execute(
        """INSERT INTO inline_images
           (sha256, width, height, bytes, classification, classification_method, classified_at, vision_description)
           VALUES (?, 800, 600, 1000, 'content', 'vision_llm', '2026-04-22T00:00:00Z', 'cached desc')""",
        (sha,),
    )
    db.commit()

    label, desc = classify_with_vision(img, conn=db)
    assert label == Classification.CONTENT
    assert desc == "cached desc"
    assert not mock_get_client.called


def test_classify_with_vision_auth_error_triggers_reauth(tmp_path, monkeypatch):
    """One auth error triggers reauth; the second call succeeds.

    Before the fix: RefreshError escapes classify_with_vision (no retry);
    failures are invisible to the auth watcher because the broad handler in
    attachment_pipeline is never reached from the vision path.
    After the fix: call_with_policy retries after reauth.

    running_on_linux is pinned False (macOS path) so decide() chooses
    REAUTH_RETRY instead of giving up immediately with UNRECOVERABLE_AUTH.

    Mutation checks:
    - Remove call_with_policy (bare create): RefreshError propagates,
      assert label == Classification.CONTENT is never reached.
    - Remove the retry (loop exits after 1): len(calls) == 1, not 2.
    """
    from PIL import Image

    img = tmp_path / "x.png"
    Image.new("RGB", (10, 10), "red").save(img)
    db = _setup_db(tmp_path)

    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                raise gauth.RefreshError("invalid_grant: Bad Request")
            return type("R", (), {
                "stop_reason": "end_turn",
                "content": [type("C", (), {"text": "CONTENT: a red square"})()],
            })()

    fake = type("Client", (), {"messages": FakeMessages()})()
    # classify_with_vision does a lazy import of _get_client_and_model from
    # claude_extract, so patching it there is the correct interception point.
    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (fake, "m"))
    monkeypatch.setattr(claude_extract, "running_on_linux", lambda: False)
    with patch.object(claude_extract, "reauth", return_value=ReauthResult.SUCCEEDED):
        label, desc = classify_with_vision(img, conn=db)

    assert label == Classification.CONTENT
    assert len(calls) == 2
