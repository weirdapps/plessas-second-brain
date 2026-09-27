"""auto_fix reports what the kick actually did (health-2).

Every sb-* unit is Type=oneshot, so a blocking `systemctl start` waited for the
whole job and was killed at 30 s, and the report said "Failed to kick" for jobs
systemd had queued and went on to run. The return code was never read either,
so a restart systemd refused was reported as "Re-kicked".
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

HEALTH_CHECK_PATH = Path(__file__).parent.parent / "scripts" / "health_check.py"


@pytest.fixture
def hc():
    spec = importlib.util.spec_from_file_location("health_check", HEALTH_CHECK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_run(calls, returncode, stderr=b""):
    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout=b"", stderr=stderr)

    return run


def test_systemd_kick_only_enqueues_the_job(hc, monkeypatch):
    calls = []
    monkeypatch.setattr(hc, "IS_MACOS", False)
    monkeypatch.setattr(hc.subprocess, "run", _fake_run(calls, 0))

    hc.kick_job("sb-x.service")

    assert "--no-block" in calls[0]
    assert calls[0][-1] == "sb-x.service"


@pytest.mark.parametrize("macos", [False, True])
def test_a_queued_job_is_reported_as_queued(hc, monkeypatch, macos):
    calls = []
    monkeypatch.setattr(hc, "IS_MACOS", macos)
    monkeypatch.setattr(hc.subprocess, "run", _fake_run(calls, 0))

    actions = hc.auto_fix([{"type": "job_failed", "label": "sb-x.service"}])

    assert actions == ["Queued sb-x.service"]


@pytest.mark.parametrize("macos", [False, True])
def test_a_refused_kick_is_reported_as_failed_with_its_reason(hc, monkeypatch, macos):
    calls = []
    monkeypatch.setattr(hc, "IS_MACOS", macos)
    monkeypatch.setattr(
        hc.subprocess, "run", _fake_run(calls, 5, stderr=b"Unit sb-x.service not found.\n")
    )

    actions = hc.auto_fix(
        [
            {"type": "job_failed", "label": "sb-x.service"},
            {"type": "stale_data", "label": "sb-x.service", "name": "Emails"},
            {"type": "sentinel", "name": "needs_reauth"},
        ]
    )

    assert actions[0] == "Failed to kick sb-x.service (rc=5): Unit sb-x.service not found."
    assert actions[1] == "Failed to kick sb-x.service (rc=5): Unit sb-x.service not found."
    assert actions[2].startswith(f"Failed to kick {hc.AUTH_WATCH_JOB} (rc=5)")
    assert not any("Kicked" in a or "Queued" in a for a in actions)
