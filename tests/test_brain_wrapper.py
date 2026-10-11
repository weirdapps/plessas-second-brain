"""`./brain` runs from any directory, and a relative path in its arguments stays the caller's.

It found the interpreter but neither changed directory nor set PYTHONPATH, and the project
installs no package, so from anywhere but the checkout `-m src.cli` failed with "No module
named 'src'". Changing directory would have broken the other half: `brain ingest
./report.pdf` names a file where brain was run.
"""

import json
import subprocess
import sys
from pathlib import Path

from tests import seeded_store

REPO = Path(__file__).resolve().parent.parent


def _brain(cwd: Path, *argv: str) -> subprocess.CompletedProcess:
    """`<repo>/brain ARGV` run from `cwd`, with this interpreter as its environment."""
    (cwd / "data").mkdir(exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(cwd),
        "SECOND_BRAIN_VENV_PYTHON": sys.executable,
        "BRAIN_DATA_DIR": str(cwd / "data"),
        "BRAIN_CONFIG_FILE": str(cwd / "absent"),
        "BRAIN_ROLE": "producer",
    }
    return subprocess.run(
        [str(REPO / "brain"), *argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_brain_runs_from_another_directory(tmp_path):
    run = _brain(tmp_path, "--help")

    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("usage: brain")


def test_a_relative_path_names_a_file_where_brain_was_run(tmp_path):
    seeded_store.build(tmp_path / "copy.db", synced="2026-03-03T03:03:03Z")

    run = _brain(tmp_path, "--db", "copy.db", "tool", "stats")

    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["data_as_of"] == "2026-03-03T03:03:03Z"
