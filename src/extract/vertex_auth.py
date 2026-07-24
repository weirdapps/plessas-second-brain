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

from pathlib import Path

GCLOUD_SENTINEL = Path.home() / ".second-brain" / "needs_gcloud_reauth"

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


def touch_sentinel() -> None:
    """Touch the gcloud-reauth sentinel that auth-watch and wrapper scripts check."""
    GCLOUD_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
    GCLOUD_SENTINEL.touch(exist_ok=True)
