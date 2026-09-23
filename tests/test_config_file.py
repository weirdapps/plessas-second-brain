"""The optional per-host settings file.

Identity and tenant settings have to reach every entry point on a host: the CLI
the timers run, the MCP server Claude Code spawns with an empty environment, and
the health check. Nothing read a file before this, so on 2026-09-23 the producer
ran with SHAREPOINT_HOST at its "contoso" placeholder and every Mac MCP server
ran without BRAIN_USER_EMAIL_PATTERN, which switched stale_threads off.
"""

import importlib

from src.config import load_config_file


def _write(tmp_path, text):
    path = tmp_path / "env"
    path.write_text(text)
    return path


def test_a_missing_file_changes_nothing(tmp_path):
    env: dict[str, str] = {}
    load_config_file(tmp_path / "absent", env)
    assert env == {}


def test_brain_and_sharepoint_keys_are_read(tmp_path):
    path = _write(
        tmp_path,
        "BRAIN_USER_EMAIL_PATTERN=owner@example.com\nSHAREPOINT_HOST=contoso.sharepoint.com\n",
    )
    env: dict[str, str] = {}
    load_config_file(path, env)
    assert env == {
        "BRAIN_USER_EMAIL_PATTERN": "owner@example.com",
        "SHAREPOINT_HOST": "contoso.sharepoint.com",
    }


def test_export_prefix_quotes_comments_and_blank_lines(tmp_path):
    path = _write(
        tmp_path,
        "# identity\n\nexport BRAIN_USER_NAME=\"Jane Doe\"\nBRAIN_USER_ROLE='CFO'\n",
    )
    env: dict[str, str] = {}
    load_config_file(path, env)
    assert env == {"BRAIN_USER_NAME": "Jane Doe", "BRAIN_USER_ROLE": "CFO"}


def test_the_environment_wins_over_the_file(tmp_path):
    path = _write(tmp_path, "SHAREPOINT_HOST=partner.sharepoint.com\n")
    env = {"SHAREPOINT_HOST": "contoso.sharepoint.com"}
    load_config_file(path, env)
    assert env["SHAREPOINT_HOST"] == "contoso.sharepoint.com"


def test_keys_outside_the_allowlist_are_ignored(tmp_path):
    """The file carries identity and tenant settings only. A credential, a
    backend switch or a relocated data home pasted into it must not be picked up
    silently, so only four named keys are honoured."""
    path = _write(
        tmp_path,
        "ANTHROPIC_API_KEY=sk-test\nPATH=/tmp\nBRAIN_CONFIG_FILE=/elsewhere\n"
        "BRAIN_EXTRACT_ENGINE=gemini\nBRAIN_DATA_DIR=/elsewhere\nnot a setting\n",
    )
    env: dict[str, str] = {}
    load_config_file(path, env)
    assert env == {}


def test_an_inline_comment_is_not_part_of_the_value(tmp_path):
    path = _write(
        tmp_path,
        "SHAREPOINT_HOST=contoso.sharepoint.com  # our tenant\n"
        'BRAIN_USER_NAME="Jane # Doe" # the owner\n',
    )
    env: dict[str, str] = {}
    load_config_file(path, env)
    assert env == {"SHAREPOINT_HOST": "contoso.sharepoint.com", "BRAIN_USER_NAME": "Jane # Doe"}


def test_a_byte_order_mark_does_not_hide_the_first_line(tmp_path):
    path = tmp_path / "env"
    path.write_bytes("﻿SHAREPOINT_HOST=contoso.sharepoint.com\n".encode())
    env: dict[str, str] = {}
    load_config_file(path, env)
    assert env == {"SHAREPOINT_HOST": "contoso.sharepoint.com"}


def test_a_file_that_is_not_utf8_is_ignored_not_fatal(tmp_path, capsys):
    """The loader runs at import of every entry point. A Greek name saved as
    cp1253 must not stop the MCP server, the CLI and the health check."""
    path = tmp_path / "env"
    path.write_bytes("BRAIN_USER_NAME=Δημήτρης\n".encode("cp1253"))
    env: dict[str, str] = {}
    load_config_file(path, env)
    assert env == {}
    assert "not UTF-8" in capsys.readouterr().err


def test_importing_config_applies_the_file(monkeypatch, tmp_path):
    # import_module, not `import src.config`: the latter can resolve through the
    # package attribute, which tests/test_health_check.py's _reload_hc leaves
    # pointing at a module object that is no longer in sys.modules.
    cfg = importlib.import_module("src.config")

    path = _write(tmp_path, "BRAIN_USER_EMAIL_PATTERN=owner@example.com\n")
    monkeypatch.setenv("BRAIN_CONFIG_FILE", str(path))
    monkeypatch.delenv("BRAIN_USER_EMAIL_PATTERN", raising=False)
    try:
        importlib.reload(cfg)
        assert cfg.USER_EMAIL_PATTERN == "owner@example.com"
    finally:
        monkeypatch.setenv("BRAIN_CONFIG_FILE", str(tmp_path / "absent"))
        monkeypatch.delenv("BRAIN_USER_EMAIL_PATTERN", raising=False)
        importlib.reload(cfg)
