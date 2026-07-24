"""Subprocess wrapper for teams-cli.

Used by the second-brain Teams ingestion pipeline. Always passes
--no-auto-reauth so a cron context cannot trigger an interactive
Playwright login. Mirrors src/export/outlook_cli.py.
"""

import json
import subprocess
from typing import Any


class TeamsCliError(Exception):
    """Generic teams-cli failure."""

    def __init__(self, exit_code: int, stderr: str, retryable: bool):
        self.exit_code = exit_code
        self.stderr = stderr
        self.retryable = retryable
        super().__init__(f"teams-cli exit {exit_code}: {stderr}")


class TeamsCliAuthRequired(TeamsCliError):
    """Exit code 4 — caller should bail without retrying."""

    def __init__(self, stderr: str):
        super().__init__(exit_code=4, stderr=stderr, retryable=False)


_RETRYABLE_EXIT_CODES = {5}  # upstream API errors (429, 5xx, network)


def run_teams_cli(args: list[str], timeout_sec: int = 60) -> Any:
    """Invoke teams-cli with the given args.

    Always appends --no-auto-reauth (idempotent). teams-cli emits JSON to
    stdout natively, so no --json flag is required.

    Args:
        args: Subcommand and flags, e.g. ["list-teams"] or
              ["list-messages", "--chat", "<id>"].
        timeout_sec: Subprocess timeout in seconds.

    Returns:
        Parsed JSON from stdout on exit 0.

    Raises:
        TeamsCliAuthRequired: exit 4.
        TeamsCliError: any other non-zero exit.
    """
    final_args = list(args)
    if "--no-auto-reauth" not in final_args:
        final_args.append("--no-auto-reauth")

    cmd = ["teams-cli", *final_args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )

    if result.returncode == 0:
        return json.loads(result.stdout) if result.stdout.strip() else None

    if result.returncode == 4:
        raise TeamsCliAuthRequired(result.stderr.strip())

    raise TeamsCliError(
        exit_code=result.returncode,
        stderr=result.stderr.strip(),
        retryable=result.returncode in _RETRYABLE_EXIT_CODES,
    )
