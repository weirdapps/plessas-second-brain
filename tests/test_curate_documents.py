"""
Tests for scripts/curate_documents_daily.py.

Regression cover for the extended-thinking content-block bug. The script was
written before commit 0cc1c50 ("extract: read past thinking blocks") and lived
outside version control on the VPS, so it never received that fix. Both of its
LLM call sites indexed ``response.content[0].text``, which raises
AttributeError when the first block is a ThinkingBlock. Observed in production
on 2026-08-11: every classification failed with

    classify error for id=31376: 'ThinkingBlock' object has no attribute 'text'

The script loads its organisational taxonomy from a private file outside the
repo, so these tests stand up a stub taxonomy under a temporary HOME and load
the script the way tests/test_health_check.py loads its script.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

CURATE_PATH = Path(__file__).parent.parent / "scripts" / "curate_documents_daily.py"

STUB_TAXONOMY = """\
MANAGED_FOLDERS = ["Area/one", "Area/two"]
SOFT_CAPS = {"Area/one": 10}
TAXONOMY = "stub taxonomy"
CLASSIFY_PROMPT = (
    "{taxonomy} {filename} {subject} {sender} {date} {size_mb} {summary}"
)
SUMMARIZE_PROMPT = "{folder} {readme}"
NOISE_SENDERS = set()
AREAS = ["Area"]
"""


class _ThinkingBlock:
    """Stand-in for anthropic's ThinkingBlock: exposes .thinking, never .text."""

    def __init__(self, thinking: str) -> None:
        self.thinking = thinking


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    def __init__(self, *blocks) -> None:
        self.content = list(blocks)


@pytest.fixture
def curate(tmp_path, monkeypatch):
    """Load scripts/curate_documents_daily.py against a stub private taxonomy."""
    taxonomy = tmp_path / "SourceCode" / "claude-config" / "private" / "curate-taxonomy.py"
    taxonomy.parent.mkdir(parents=True)
    taxonomy.write_text(STUB_TAXONOMY)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    spec = importlib.util.spec_from_file_location("curate_documents_daily", CURATE_PATH)
    assert spec and spec.loader, f"could not load {CURATE_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["curate_documents_daily"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("curate_documents_daily", None)


def _capture_response(monkeypatch, curate, response):
    """Make both call sites return ``response`` from the LLM."""
    monkeypatch.setattr(curate, "create_with_refusal_fallback", lambda *a, **k: response)


def _capture_kwargs(monkeypatch, curate, response):
    """Record the kwargs each call site sends to the LLM."""
    seen = {}

    def fake(client, **kwargs):
        seen.update(kwargs)
        return response

    monkeypatch.setattr(curate, "create_with_refusal_fallback", fake)
    return seen


def test_classify_reads_past_a_thinking_block(curate, monkeypatch):
    """A leading ThinkingBlock must not sink the classification."""
    _capture_response(
        monkeypatch,
        curate,
        _Response(
            _ThinkingBlock("weighing the folders"),
            _TextBlock('{"folder": "Area/one", "confidence": "high"}'),
        ),
    )
    candidate = {
        "filename": "deck.pdf",
        "subject": "s",
        "sender": "a@example.com",
        "date": "2026-08-11",
        "file_size": 1024,
        "summary": "body",
    }
    assert curate.classify_one(object(), "model", candidate) == {
        "folder": "Area/one",
        "confidence": "high",
    }


def test_summarize_folder_reads_past_a_thinking_block(curate, monkeypatch):
    """The second call site had the same defect."""
    _capture_response(
        monkeypatch,
        curate,
        _Response(
            _ThinkingBlock("reading the readme"),
            _TextBlock('{"summary": "ok"}'),
        ),
    )
    assert curate.summarize_folder(object(), "model", "Area/one", "readme") == {"summary": "ok"}


def test_classify_still_parses_a_plain_text_response(curate, monkeypatch):
    """No thinking block: unchanged behaviour."""
    _capture_response(
        monkeypatch,
        curate,
        _Response(_TextBlock('```json\n{"folder": "Area/two", "confidence": "low"}\n```')),
    )
    candidate = {
        "filename": "x.pdf",
        "subject": "",
        "sender": "",
        "date": "2026-08-11",
        "file_size": 1,
        "summary": "",
    }
    assert curate.classify_one(object(), "model", candidate) == {
        "folder": "Area/two",
        "confidence": "low",
    }


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda m: m.classify_one(
                object(),
                "model",
                {
                    "filename": "f.pdf",
                    "subject": "",
                    "sender": "",
                    "date": "2026-08-11",
                    "file_size": 1,
                    "summary": "",
                },
            ),
            id="classify_one",
        ),
        pytest.param(
            lambda m: m.summarize_folder(object(), "model", "Area/one", "readme"),
            id="summarize_folder",
        ),
    ],
)
def test_output_budget_leaves_room_after_thinking(curate, monkeypatch, call):
    """max_tokens must fund the thinking budget AND the answer.

    Observed on the VPS 2026-08-11 with the original max_tokens=300::

        stop_reason: max_tokens
        output_tokens: 300  thinking_tokens: 300
        blocks: ['ThinkingBlock']

    Extended thinking spends the same budget the answer draws on, so a ceiling
    below the 1024-token minimum thinking budget can never return text. Both
    call sites use the repo-wide MAX_OUTPUT_TOKENS so they cannot drift apart.
    """
    seen = _capture_kwargs(monkeypatch, curate, _Response(_TextBlock("{}")))
    call(curate)
    assert seen["max_tokens"] == curate.MAX_OUTPUT_TOKENS
    assert seen["max_tokens"] > 1024


def test_missing_taxonomy_file_exits_loud(tmp_path, monkeypatch):
    """No private taxonomy must be a loud exit, never a silent degraded mode."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    spec = importlib.util.spec_from_file_location("curate_documents_daily_missing", CURATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(SystemExit) as excinfo:
        spec.loader.exec_module(module)
    assert "curate-taxonomy.py" in str(excinfo.value)
