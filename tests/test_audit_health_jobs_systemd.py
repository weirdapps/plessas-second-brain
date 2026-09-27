"""_check_jobs_systemd's verdicts, pinned against canned `systemctl show` output (tests-ci-2).

It is the VPS health mail's only per-unit verdict, and no test reached its
classifier: on CI the real `systemctl --user show` has no user bus, so every
unit fell into NOT_LOADED and only the descriptions were asserted.
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


@pytest.mark.parametrize(
    ("load", "active", "result", "expected"),
    [
        ("loaded", "failed", "exit-code", "FAIL(result=exit-code)"),
        ("loaded", "inactive", "success", "OK"),
        ("loaded", "activating", "success", "RUNNING"),
        # auto-restart after a crash: still activating, but systemd already
        # recorded the failure.
        ("loaded", "activating", "exit-code", "FAIL(result=exit-code)"),
        ("not-found", "inactive", "success", "NOT_LOADED"),
        ("masked", "inactive", "success", "NOT_LOADED"),
    ],
)
def test_systemd_verdicts(hc, monkeypatch, load, active, result, expected):
    stdout = (
        f"LoadState={load}\nActiveState={active}\nSubState=dead\n"
        f"Result={result}\nExecMainStatus=1\n"
    )
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(hc.subprocess, "run", run)

    jobs = hc._check_jobs_systemd()

    assert set(jobs) == set(hc.SYSTEMD_UNITS)
    assert {j["status"] for j in jobs.values()} == {expected}
    assert all("show" in c and "--user" in c for c in calls)
