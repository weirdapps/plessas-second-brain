"""The Claude model and region come from the environment; the direct API needs an opt-in.

The code fell back to a Claude 4.6 model in europe-west1 when no model or region was set,
and its refusal fallback to another 4.6 model there. A Vertex project whose allowlist
holds neither answers every call with an error, and a fresh host met that with no word of
why. Neither has a default now: the first call that needs them stops and names the
variables, and work met in the meantime stays pending rather than failing for good.

A key for the direct Anthropic API was enough to send work there whenever no Vertex
project was set. It now also takes BRAIN_ALLOW_DIRECT_API=1.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src.extract import claude_extract
from src.extract.claude_extract import ClaudeNotConfigured

MODEL_VARS = ("CLAUDE_EXTRACT_MODEL", "VERTEX_MODEL_EXTRACT")
REGION_VARS = ("VERTEX_SDK_REGION", "CLOUD_ML_REGION")
SETTINGS = (
    *MODEL_VARS,
    *REGION_VARS,
    "VERTEX_SDK_PROJECT",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_API_KEY",
    "BRAIN_ALLOW_DIRECT_API",
)
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _nothing_configured(monkeypatch):
    """None of this machine's settings: each test sets what it means to."""
    for name in SETTINGS:
        if name in os.environ:
            monkeypatch.delenv(name)
    claude_extract.reset_client_cache()
    yield
    claude_extract.reset_client_cache()


@pytest.fixture
def built(monkeypatch):
    """The SDK clients built, as (kind, kwargs), with no SDK or network behind them."""
    made: list[tuple[str, dict]] = []

    def client_class(kind):
        class Client:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                made.append((kind, kwargs))

        return Client

    monkeypatch.setattr("anthropic.AnthropicVertex", client_class("vertex"))
    monkeypatch.setattr("anthropic.Anthropic", client_class("direct"))
    return made


def test_the_first_call_without_a_model_names_the_variables(built, monkeypatch):
    monkeypatch.setenv("VERTEX_SDK_PROJECT", "test-project")
    monkeypatch.setenv("VERTEX_SDK_REGION", "eu")

    with pytest.raises(ClaudeNotConfigured) as err:
        claude_extract._build_client_and_model()

    assert all(name in str(err.value) for name in MODEL_VARS)
    assert "4.7" in str(err.value), "the message keeps the model-region pairing rule"
    assert built == []


def test_the_first_call_without_a_region_names_the_variables(built, monkeypatch):
    monkeypatch.setenv("VERTEX_SDK_PROJECT", "test-project")
    monkeypatch.setenv("CLAUDE_EXTRACT_MODEL", "claude-test-model")

    with pytest.raises(ClaudeNotConfigured) as err:
        claude_extract._build_client_and_model()

    assert all(name in str(err.value) for name in REGION_VARS)
    assert built == []


@pytest.mark.parametrize(
    ("model_var", "region_var"),
    [("CLAUDE_EXTRACT_MODEL", "VERTEX_SDK_REGION"), ("VERTEX_MODEL_EXTRACT", "CLOUD_ML_REGION")],
)
def test_either_name_of_each_setting_will_do(built, monkeypatch, model_var, region_var):
    monkeypatch.setenv("VERTEX_SDK_PROJECT", "test-project")
    monkeypatch.setenv(model_var, "claude-test-model")
    monkeypatch.setenv(region_var, "eu")

    client, model = claude_extract._build_client_and_model()

    assert model == "claude-test-model"
    assert built == [("vertex", client.kwargs)]
    assert client.kwargs["region"] == "eu"


def test_a_blank_setting_counts_as_unset(built, monkeypatch):
    """An EnvironmentFile= line with nothing after the `=` sets the variable to ''."""
    monkeypatch.setenv("VERTEX_SDK_PROJECT", "test-project")
    monkeypatch.setenv("VERTEX_SDK_REGION", "eu")
    monkeypatch.setenv("CLAUDE_EXTRACT_MODEL", " ")

    with pytest.raises(ClaudeNotConfigured):
        claude_extract._build_client_and_model()


def test_a_missing_setting_is_not_retried(monkeypatch):
    """No retry can supply a setting: the policy's backoff only delayed the message."""
    monkeypatch.setenv("VERTEX_SDK_PROJECT", "test-project")
    slept: list[float] = []
    monkeypatch.setattr(claude_extract.time, "sleep", slept.append)

    with pytest.raises(ClaudeNotConfigured):
        claude_extract.complete(max_tokens=10, messages=[])

    assert slept == []


def test_importing_the_extraction_code_needs_no_setting():
    """Tests and the read-only tools import it on hosts that set none of them."""
    env = {k: v for k, v in os.environ.items() if k not in SETTINGS}
    code = (
        "import src.cli, src.extract.claude_extract, src.extract.local, src.extract.vertex_fallback"
    )

    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr


def test_an_attachment_met_before_the_model_is_set_stays_pending(tmp_path, monkeypatch):
    """Not the item's fault: 'failed' is terminal for a summary, so the queue would have
    been lost to a missing setting. Nor is it an expired login."""
    from src.extract import vertex_auth
    from src.extract.attachment_pipeline import run_phase1, run_phase2
    from src.store.schema import create_database

    sentinel = tmp_path / "needs_gcloud_reauth"
    monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", sentinel)
    db_path = str(tmp_path / "brain.db")
    conn = create_database(db_path)
    note = tmp_path / "notes.txt"
    note.write_text("Body text long enough to clear the extraction threshold comfortably.")
    conn.execute("INSERT INTO emails (message_id, date_received) VALUES ('m1', '2026-10-01')")
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at) VALUES (1, 'm1', 'notes.txt', 'text/plain', 70, ?, 'now')",
        (str(note),),
    )
    conn.commit()
    conn.close()
    run_phase1(db_path)

    stats = run_phase2(db_path)

    conn = sqlite3.connect(db_path)
    status, error = conn.execute("SELECT llm_status, llm_error FROM attachment_content").fetchone()
    conn.close()
    assert status == "pending"
    assert "ClaudeNotConfigured" in error
    assert stats["failed"] == 1, "the run still reads as failed"
    assert not sentinel.exists()


@pytest.mark.parametrize("switch", [None, "", "0"])
def test_an_api_key_alone_never_builds_the_direct_client(built, monkeypatch, switch):
    """A key left in a shell profile once sent work mail to the direct API."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
    monkeypatch.setenv("CLAUDE_EXTRACT_MODEL", "claude-test-model")
    if switch is not None:
        monkeypatch.setenv("BRAIN_ALLOW_DIRECT_API", switch)

    with pytest.raises(ClaudeNotConfigured) as err:
        claude_extract._build_client_and_model()

    assert built == []
    assert "BRAIN_ALLOW_DIRECT_API" in str(err.value)


def test_the_switch_lets_the_direct_api_in_and_says_so(built, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
    monkeypatch.setenv("CLAUDE_EXTRACT_MODEL", "claude-test-model")
    monkeypatch.setenv("BRAIN_ALLOW_DIRECT_API", "1")

    client, model = claude_extract._build_client_and_model()

    assert built == [("direct", client.kwargs)]
    assert model == "claude-test-model"
    err = capsys.readouterr().err
    assert "WARNING" in err and "BRAIN_ALLOW_DIRECT_API" in err
    assert "sk-ant" not in err


def test_the_switch_never_overrides_a_vertex_project(built, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
    monkeypatch.setenv("BRAIN_ALLOW_DIRECT_API", "1")
    monkeypatch.setenv("VERTEX_SDK_PROJECT", "test-project")
    monkeypatch.setenv("VERTEX_SDK_REGION", "eu")
    monkeypatch.setenv("CLAUDE_EXTRACT_MODEL", "claude-test-model")

    claude_extract._build_client_and_model()

    assert [kind for kind, _ in built] == ["vertex"]
