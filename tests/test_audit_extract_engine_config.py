"""BRAIN_EXTRACT_ENGINE is normalised, and a value that is neither engine stops.

systemd's EnvironmentFile= keeps an inline comment as part of the value, so the
.env.example line `BRAIN_EXTRACT_ENGINE=claude   # "claude" (default) or
"gemini"` arrived as that whole string. Every `engine == "claude"` test then
failed and extraction silently took the Gemini branch. Finding docs-3.
"""

import importlib

import pytest


def _reload(monkeypatch, value):
    import src.config as cfg

    if value is None:
        monkeypatch.delenv("BRAIN_EXTRACT_ENGINE", raising=False)
    else:
        monkeypatch.setenv("BRAIN_EXTRACT_ENGINE", value)
    return importlib.reload(cfg)


@pytest.fixture
def restore_config(monkeypatch):
    yield
    import src.config as cfg

    monkeypatch.undo()
    importlib.reload(cfg)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, "claude"), ("", "claude"), (" Claude ", "claude"), ("GEMINI\n", "gemini")],
)
def test_engine_is_stripped_and_lowercased(monkeypatch, restore_config, raw, expected):
    assert _reload(monkeypatch, raw).EXTRACT_ENGINE == expected


@pytest.mark.parametrize(
    "raw", ['claude            # "claude" (default) or "gemini"', "anthropic", "openai"]
)
def test_an_unknown_engine_fails_fast_and_names_the_value(monkeypatch, restore_config, raw):
    with pytest.raises(ValueError, match="BRAIN_EXTRACT_ENGINE") as err:
        _reload(monkeypatch, raw)
    assert repr(raw.strip().lower()) in str(err.value)


def test_env_example_has_no_inline_comments():
    """Every assignment, commented out or not, is copied into a systemd
    EnvironmentFile= sooner or later, so none may carry a trailing comment."""
    import re
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / ".env.example"
    assignment = re.compile(r"^#?\s*[A-Z][A-Z0-9_]*=")
    offenders = [
        line
        for line in example.read_text().splitlines()
        if assignment.match(line) and re.search(r"\s#", line.lstrip("#"))
    ]
    assert offenders == []
