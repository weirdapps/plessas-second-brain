"""
Subprocess wrapper for sharepoint-cli.

Sibling of outlook_cli.py. SharePoint moved out of outlook-cli into its own
repo (~/SourceCode/sharepoint-access) on 2026-08-08; mail, attachments and
calendar still go through outlook_cli.py.

Exit codes 0-6 are identical across outlook-cli, teams-cli and sharepoint-cli,
so 4 always means "credentials gone" and 5 always means "upstream misbehaved".
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Overridable so a non-standard checkout or a VPS deploy path still works.
SHAREPOINT_CLI = Path(
    os.environ.get(
        "SHAREPOINT_CLI_PATH",
        str(Path.home() / "SourceCode" / "sharepoint-access" / "dist" / "cli.js"),
    )
)


class SharepointCliError(Exception):
    def __init__(self, exit_code: int, stderr: str, retryable: bool):
        self.exit_code = exit_code
        self.stderr = stderr
        self.retryable = retryable
        super().__init__(f"sharepoint-cli exit {exit_code}: {stderr}")


class SharepointCliAuthRequired(SharepointCliError):
    """Exit code 4 — the session is gone; caller should bail without retrying."""

    def __init__(self, stderr: str):
        super().__init__(exit_code=4, stderr=stderr, retryable=False)


class SharepointCliMissing(SharepointCliError):
    """The CLI is not built. Distinct so health checks can say so precisely."""

    def __init__(self, path: Path):
        super().__init__(
            exit_code=127,
            stderr=(
                f"sharepoint-cli not built at {path}. Run:\n"
                f"  cd ~/SourceCode/sharepoint-access && npm ci && npm run build"
            ),
            retryable=False,
        )


_RETRYABLE_EXIT_CODES = {5}  # upstream API errors


def host_for_url(url: str) -> str:
    """The CLI requires --host. For an absolute URL, use the URL's own host."""
    return (urlparse(url).netloc or "").lower()


def parse_error_payload(stderr: str) -> dict[str, Any]:
    """sharepoint-cli writes a JSON error object to stderr. Never raises."""
    for line in reversed((stderr or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def run_sharepoint_cli(args: list[str], host: str, timeout_sec: int = 180) -> Any:
    """
    Invoke sharepoint-cli with --host. Returns parsed JSON on exit 0;
    raises typed errors otherwise.
    """
    if not SHAREPOINT_CLI.exists():
        raise SharepointCliMissing(SHAREPOINT_CLI)

    cmd = ["node", str(SHAREPOINT_CLI), "--host", host, *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, check=False)

    if result.returncode == 0:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return json.JSONDecoder().raw_decode(result.stdout.lstrip())[0]

    if result.returncode == 4:
        raise SharepointCliAuthRequired(result.stderr.strip())

    raise SharepointCliError(
        exit_code=result.returncode,
        stderr=result.stderr.strip(),
        retryable=result.returncode in _RETRYABLE_EXIT_CODES,
    )
