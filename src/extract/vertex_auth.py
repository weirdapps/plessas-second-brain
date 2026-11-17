"""Vertex AI / gcloud ADC auth-expiry detection.

When ADC expires the Anthropic Vertex SDK raises a
``google.auth.exceptions.RefreshError`` whose string contains
``"Reauthentication is needed"``. Persisting these as hard ``failed`` rows
wastes LLM cycles and prevents auto-recovery after re-auth — the row stays
``failed`` forever unless someone resets it.

This module centralises detection. Persistence sites (attachment_pipeline,
teams_pipeline) use ``is_vertex_auth_error`` to route auth errors into a
``pending`` row instead of ``failed`` and ``touch_sentinel`` to write the
``needs_gcloud_reauth`` file that ``sb-auth-watch.sh`` already manages
(probes hourly, clears on success). Wrapper scripts read the same sentinel
to short-circuit on the next cron tick.

See ``docs/superpowers/specs/2026-05-05-vertex-auth-detection-design.md``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

GCLOUD_SENTINEL = Path.home() / ".second-brain" / "needs_gcloud_reauth"

# Whether this process has already said that it set, or found, the sentinel.
_LOGGED = False

# A refusal is confirmed before the sentinel goes up. gcloud refuses "reauth is
# required" for seconds at a time on a credential that is fine: on the VPS between
# 2026-11-12 and 11-17 it did so on seven days, and gcloud-refresh.sh logged
# "ADC OK (RAPT OK)" at every slot either side of each one, with no new credential
# pushed in between. One such blip raised the sentinel, which only auth-watch
# clears, so every Vertex job skipped for up to an hour and the estate counted a
# credential outage that never happened. So: re-try the exchange after each of
# these delays, and raise the sentinel only if every try is refused.
CONFIRM_DELAYS_S = (5, 20)
# After a refusal turned out transient, later errors in this process within this
# many seconds skip the re-check, so a batch failing row after row waits once.
TRANSIENT_HOLD_S = 300
_transient_until = 0.0

_AUTH_PATTERNS = (
    "reauthentication is needed",
    "application-default login",
    "refresherror",
    "invalid_grant",
)


def is_vertex_auth_error(err: object) -> bool:
    """True if the error string matches a known Vertex/ADC auth-expiry pattern."""
    msg = str(err).lower()
    return any(p in msg for p in _AUTH_PATTERNS)


def _process() -> str:
    """This process, briefly: the script and its subcommand, as in "cli.py sync"."""
    argv = sys.argv or ["python"]
    return " ".join([Path(argv[0]).name or "python", *argv[1:2]])


def _adc_mints() -> bool | None:
    """Does ADC mint a token right now? None when it cannot be asked.

    The token itself is never logged or returned: only whether one came back.
    """
    gcloud = shutil.which("gcloud")
    if not gcloud:
        return None
    try:
        r = subprocess.run(
            [gcloud, "auth", "application-default", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.returncode == 0 and bool(r.stdout.strip())


def _refusal_confirmed() -> bool:
    """True unless ADC mints a token again within CONFIRM_DELAYS_S.

    A probe that cannot be asked (no gcloud here, a timeout) confirms nothing
    either way, so it keeps the old behaviour: the sentinel goes up.
    """
    global _transient_until
    if time.monotonic() < _transient_until:
        return False
    for delay in CONFIRM_DELAYS_S:
        time.sleep(delay)
        if _adc_mints() is True:
            _transient_until = time.monotonic() + TRANSIENT_HOLD_S
            return False
    return True


def touch_sentinel(reason: str | None = None) -> None:
    """Set the gcloud-reauth sentinel that auth-watch and wrapper scripts check.

    Says who set it: the process, and the call site unless `reason` says more. On
    2026-10-10 the sentinel went up between two auth-watch probes that both found
    ADC healthy, the next probe's clearing restarted five jobs as if after an
    outage, and no log line named the job that had set it. Created only when
    absent, so its mtime stays the moment it went up, which the health report
    prints and auth-watch's restore reads. Said once per process, and again
    whenever this process is the one that creates it.
    """
    global _LOGGED
    if not GCLOUD_SENTINEL.exists() and not _refusal_confirmed():
        logger.warning(
            "%s NOT set by %s (pid %d): ADC minted a token again within %ss, so the "
            "refusal was transient; the failed item is retried on the next run",
            GCLOUD_SENTINEL.name,
            _process(),
            os.getpid(),
            "+".join(str(d) for d in CONFIRM_DELAYS_S),
        )
        return
    GCLOUD_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
    try:
        GCLOUD_SENTINEL.touch(exist_ok=False)
        created = True
    except FileExistsError:
        created = False
    if created or not _LOGGED:
        if reason is None:
            caller = sys._getframe(1)
            reason = f"{caller.f_globals.get('__name__', '?')}:{caller.f_lineno}"
        logger.warning(
            "%s %s at %s by %s (pid %d): %s",
            "set" if created else "already set",
            GCLOUD_SENTINEL.name,
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            _process(),
            os.getpid(),
            reason,
        )
        _LOGGED = True
