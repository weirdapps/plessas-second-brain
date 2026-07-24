"""
Subprocess wrapper for outlook-cli.

Used by the second-brain ingestion pipeline. Always passes --no-auto-reauth
to prevent silent Playwright pop-ups in cron context.
"""

import json
import subprocess
from typing import Any


class OutlookCliError(Exception):
    def __init__(self, exit_code: int, stderr: str, retryable: bool):
        self.exit_code = exit_code
        self.stderr = stderr
        self.retryable = retryable
        super().__init__(f"outlook-cli exit {exit_code}: {stderr}")


class OutlookCliAuthRequired(OutlookCliError):
    """Subclass for exit code 4 — caller should bail without retrying."""

    def __init__(self, stderr: str):
        super().__init__(exit_code=4, stderr=stderr, retryable=False)


_RETRYABLE_EXIT_CODES = {5}  # upstream API errors


def run_outlook_cli(args: list[str], timeout_sec: int = 60) -> Any:
    """
    Invoke outlook-cli with the given args. Always appends --no-auto-reauth
    and --json. Returns parsed JSON on exit 0; raises typed errors otherwise.
    """
    final_args = list(args)
    if "--no-auto-reauth" not in final_args:
        final_args.append("--no-auto-reauth")
    if "--json" not in final_args:
        final_args.append("--json")

    cmd = ["outlook-cli", *final_args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )

    if result.returncode == 0:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            # outlook-cli occasionally appends a non-JSON line after the JSON
            # payload (e.g. a token-refresh notice), making json.loads raise
            # "Extra data". Decode just the leading JSON value and ignore the
            # trailing output rather than failing the whole sync.
            return json.JSONDecoder().raw_decode(result.stdout.lstrip())[0]

    if result.returncode == 4:
        raise OutlookCliAuthRequired(result.stderr.strip())

    raise OutlookCliError(
        exit_code=result.returncode,
        stderr=result.stderr.strip(),
        retryable=result.returncode in _RETRYABLE_EXIT_CODES,
    )
