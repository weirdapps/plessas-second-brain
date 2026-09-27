"""check_curation must go red while curate-docs places nothing (health-1, scripts-maint-1).

`attempts` only rises when a parked candidate is offered again, and it is only
offered again once its folder has headroom. While every folder sits at its cap
nothing is re-offered, so production held 376 deferred entries all at
attempts=1 and the check read OK for weeks. These tests hold attempts at 1 and
age the queue instead, which is the state production actually reaches.
"""

import importlib.util
import json
from datetime import datetime, timedelta
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


def _ago(days):
    # Naive local ISO, the format curate_documents_daily writes.
    return (datetime.now() - timedelta(days=days)).isoformat()


def _state(tmp_path, deferred, copied):
    p = tmp_path / "curate-state.json"
    p.write_text(json.dumps({"deferred": deferred, "copied": copied}))
    return p


def test_warns_when_the_oldest_deferral_is_over_a_week_old(hc, tmp_path):
    state = _state(
        tmp_path,
        deferred={"7": {"folder": "retail", "attempts": 1, "last_attempt": _ago(10)}},
        copied=[{"id": 1, "classified_at": _ago(1)}],
    )

    r = hc.check_curation(state_path=state)

    assert r["blocked"] == 0
    assert r["status"] == "WARN"
    assert r["deferral_age"] > timedelta(days=7)


def test_warns_when_nothing_was_placed_for_a_week_while_candidates_wait(hc, tmp_path):
    state = _state(
        tmp_path,
        deferred={"7": {"folder": "retail", "attempts": 1, "last_attempt": _ago(1)}},
        copied=[{"id": 1, "classified_at": _ago(10)}],
    )

    assert hc.check_curation(state_path=state)["status"] == "WARN"


def test_fresh_back_pressure_stays_ok(hc, tmp_path):
    state = _state(
        tmp_path,
        deferred={"7": {"folder": "retail", "attempts": 1, "last_attempt": _ago(1)}},
        copied=[{"id": 1, "classified_at": _ago(1)}],
    )

    assert hc.check_curation(state_path=state)["status"] == "OK"


def test_an_old_placement_with_nothing_deferred_stays_ok(hc, tmp_path):
    """No candidate was turned away, so a quiet week is just a quiet week."""
    state = _state(tmp_path, deferred={}, copied=[{"id": 1, "classified_at": _ago(30)}])

    assert hc.check_curation(state_path=state)["status"] == "OK"


def test_report_gives_both_ages(hc, tmp_path):
    state = _state(
        tmp_path,
        deferred={"7": {"folder": "retail", "attempts": 1, "last_attempt": _ago(10)}},
        copied=[{"id": 1, "classified_at": _ago(12)}],
    )

    report, issues = hc.build_report([hc.check_curation(state_path=state)], {}, {}, {}, [])

    line = next(ln for ln in report.splitlines() if "Curation" in ln)
    assert "oldest deferral 10d" in line
    assert "last placement 12d" in line
    assert any("Curation" in str(i) for i in issues)
