"""Under `pytest -n`, each worker has a data root and a settings path of its own.

tests/conftest.py points BRAIN_DATA_DIR at a new temporary directory when it is
imported. Under xdist the controller imports it first, and every worker starts with
the controller's environment, so without a step of its own each worker would find
the variable set, keep it, and share the controller's one data root with every other
worker: two tests in two processes on the same brain.db, staging batches and state
files at once.

This runs a small suite under two workers with a copy of the real conftest, and
reads back what each worker's tests saw.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

PROBE = """
import json
import os
from pathlib import Path


def test_record_what_this_worker_sees():
    worker = os.environ.get("PYTEST_XDIST_WORKER", "controller")
    seen = {
        "worker": worker,
        "data": os.environ["BRAIN_DATA_DIR"],
        "config": os.environ["BRAIN_CONFIG_FILE"],
    }
    out = Path(os.environ["PROBE_OUT"]) / (worker + "-" + Path(__file__).stem + ".json")
    out.write_text(json.dumps(seen))
"""


def _run_two_workers(tmp_path: Path, **env: str) -> list[dict]:
    pytest.importorskip("xdist")
    suite, out = tmp_path / "suite", tmp_path / "out"
    suite.mkdir()
    out.mkdir()
    (suite / "conftest.py").write_text((REPO / "tests" / "conftest.py").read_text())
    for n in range(4):
        (suite / f"test_probe_{n}.py").write_text(PROBE)
    # This run's own environment, less what this suite's runner put there for itself: the
    # worker's xdist variables, coverage's, and any options meant for the outer run.
    inherited = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_XDIST_", "COV_CORE_", "COVERAGE_"))
        and key != "PYTEST_ADDOPTS"
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "2"]
        + ["--dist", "loadfile", str(suite)],
        cwd=suite,
        env={**inherited, "PROBE_OUT": str(out), "PYTHONPATH": str(REPO), **env},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return [json.loads(p.read_text()) for p in sorted(out.glob("*.json"))]


def _per_worker(seen: list[dict], key: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for record in seen:
        found.setdefault(record["worker"], set()).add(record[key])
    return found


def test_each_worker_has_a_data_root_of_its_own(tmp_path):
    seen = _run_two_workers(tmp_path)

    roots = _per_worker(seen, "data")
    assert sorted(roots) == ["gw0", "gw1"]
    assert all(len(r) == 1 for r in roots.values()), "a worker changed root between tests"
    assert roots["gw0"].isdisjoint(roots["gw1"])
    assert os.environ["BRAIN_DATA_DIR"] not in roots["gw0"] | roots["gw1"]
    configs = _per_worker(seen, "config")
    assert configs["gw0"].isdisjoint(configs["gw1"])


def test_a_data_root_the_developer_chose_is_kept_by_every_worker(tmp_path):
    """Pointing the suite at a tree on purpose still works, under -n as without it."""
    chosen = tmp_path / "chosen"
    chosen.mkdir()

    seen = _run_two_workers(tmp_path, BRAIN_DATA_DIR=str(chosen), BRAIN_TEST_DATA_DIR="")

    assert {record["data"] for record in seen} == {str(chosen)}
