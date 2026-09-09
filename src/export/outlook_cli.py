"""
Subprocess wrapper for outlook-cli.

Used by the second-brain ingestion pipeline. Always passes --no-auto-reauth
to prevent silent Playwright pop-ups in cron context.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _resolve_outlook_cli() -> str:
    """Absolute path to outlook-cli, not a bare name on the caller's PATH.

    The binary lives in ~/.local/bin, which is on an interactive shell's PATH and
    frequently not on a daemon's. MCP servers in particular run with a sanitized
    PATH, so `outlook_live_search` raised a bare FileNotFoundError in most live
    server processes: the one tool that exists to reach past a stale replica was
    the one that could not run. Same override shape as SHAREPOINT_CLI_PATH.
    """
    override = os.environ.get("OUTLOOK_CLI_PATH")
    if override:
        return override
    found = shutil.which("outlook-cli")
    if found:
        return found
    return str(Path.home() / ".local" / "bin" / "outlook-cli")


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

    cmd = [_resolve_outlook_cli(), *final_args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except FileNotFoundError as e:
        # A typed error saying what to do, not a bare FileNotFoundError that a
        # caller reads as "no results".
        raise OutlookCliError(
            exit_code=127,
            stderr=(
                f"outlook-cli not found at {cmd[0]}. Install it, or set "
                "OUTLOOK_CLI_PATH to its absolute path (this process does not "
                "necessarily share an interactive shell's PATH)."
            ),
            retryable=False,
        ) from e

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
