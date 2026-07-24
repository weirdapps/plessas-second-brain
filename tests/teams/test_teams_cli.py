"""Tests for the teams-cli subprocess wrapper."""

from unittest.mock import MagicMock, patch

import pytest

from src.export.teams_cli import (
    TeamsCliAuthRequired,
    TeamsCliError,
    run_teams_cli,
)


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


@patch("subprocess.run")
def test_returns_parsed_json_on_success(mock_run):
    mock_run.return_value = _completed(0, stdout='{"ok": true}')
    result = run_teams_cli(["list-teams"])
    assert result == {"ok": True}
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "teams-cli"
    assert "--no-auto-reauth" in cmd
    # teams-cli emits JSON natively; no --json flag required


@patch("subprocess.run")
def test_exit_4_raises_auth_required(mock_run):
    mock_run.return_value = _completed(4, stderr="auth required")
    with pytest.raises(TeamsCliAuthRequired) as exc:
        run_teams_cli(["list-chats"])
    assert exc.value.exit_code == 4


@patch("subprocess.run")
def test_exit_5_raises_retryable(mock_run):
    mock_run.return_value = _completed(5, stderr="upstream 429")
    with pytest.raises(TeamsCliError) as exc:
        run_teams_cli(["list-messages", "--chat", "x"])
    assert exc.value.exit_code == 5
    assert exc.value.retryable is True


@patch("subprocess.run")
def test_other_exit_raises_non_retryable(mock_run):
    mock_run.return_value = _completed(2, stderr="invalid args")
    with pytest.raises(TeamsCliError) as exc:
        run_teams_cli(["bogus"])
    assert exc.value.exit_code == 2
    assert exc.value.retryable is False


@patch("subprocess.run")
def test_appends_no_auto_reauth_idempotently(mock_run):
    mock_run.return_value = _completed(0, stdout="{}")
    run_teams_cli(["list-teams", "--no-auto-reauth"])
    cmd = mock_run.call_args[0][0]
    # Should appear exactly once
    assert cmd.count("--no-auto-reauth") == 1
