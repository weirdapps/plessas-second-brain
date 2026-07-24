from unittest.mock import MagicMock, patch

import pytest

from src.export.outlook_cli import (
    OutlookCliAuthRequired,
    OutlookCliError,
    run_outlook_cli,
)


def _mock_subprocess(stdout: str, stderr: str = "", returncode: int = 0):
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


@patch("src.export.outlook_cli.subprocess.run")
def test_returns_parsed_json_on_success(mock_run):
    mock_run.return_value = _mock_subprocess('[{"Id":"x"}]')
    result = run_outlook_cli(["list-mail", "--top", "1"])
    assert result == [{"Id": "x"}]


@patch("src.export.outlook_cli.subprocess.run")
def test_tolerates_trailing_non_json_output(mock_run):
    # outlook-cli sometimes appends a notice (e.g. a token-refresh line) after
    # the JSON payload; the leading JSON value must still parse instead of
    # raising "Extra data" and failing the whole sync.
    mock_run.return_value = _mock_subprocess('{"Id":"x","body":"hi"}\nToken refreshed.\n')
    result = run_outlook_cli(["get-mail", "x", "--body", "text"])
    assert result == {"Id": "x", "body": "hi"}


@patch("src.export.outlook_cli.subprocess.run")
def test_raises_auth_required_on_exit_4(mock_run):
    mock_run.return_value = _mock_subprocess("", '{"code":"AUTH_EXPIRED"}', returncode=4)
    with pytest.raises(OutlookCliAuthRequired):
        run_outlook_cli(["list-mail"])


@patch("src.export.outlook_cli.subprocess.run")
def test_raises_outlook_cli_error_on_exit_5(mock_run):
    mock_run.return_value = _mock_subprocess("", '{"code":"upstream"}', returncode=5)
    with pytest.raises(OutlookCliError) as exc:
        run_outlook_cli(["list-mail"])
    assert exc.value.exit_code == 5
    assert exc.value.retryable is True


@patch("src.export.outlook_cli.subprocess.run")
def test_appends_no_auto_reauth_and_json_flags(mock_run):
    mock_run.return_value = _mock_subprocess("[]")
    run_outlook_cli(["list-mail"])
    args = mock_run.call_args[0][0]
    assert "--no-auto-reauth" in args
    assert "--json" in args
