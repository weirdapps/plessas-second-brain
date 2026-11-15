"""scripts/deploy-gate.sh: the producer fast-forwards only to a commit whose required
checks passed. A fake `gh` on PATH answers check runs from a file per commit."""

import os
import stat
import subprocess
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "scripts" / "deploy-gate.sh"
REQUIRED = ["lint", "test (3.12)", "test (3.14)", "types (3.12)", "types (3.14)"]
REQUIRED += ["pii-gauntlet", "wrappers"]

FAKE_GH = """#!/bin/sh
# gh api repos/<slug>/commits/<sha>/check-runs?... --jq ...: the passed check names on file.
sha=$(printf '%s' "$2" | sed -E 's#.*/commits/([0-9a-f]+)/.*#\\1#')
[ -f "$CHECKS_DIR/$sha" ] && cat "$CHECKS_DIR/$sha"
exit 0
"""


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def world(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    for cwd in (seed,):
        _git(cwd, "config", "user.email", "t@example.com")
        _git(cwd, "config", "user.name", "T")
    (seed / "f").write_text("0")
    _git(seed, "add", "f")
    _git(seed, "commit", "-q", "-m", "c0")
    _git(seed, "push", "-q", "origin", "master")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    checks = tmp_path / "checks"
    checks.mkdir()
    return {"seed": seed, "checkout": checkout, "bin": bin_dir, "checks": checks}


def _commit(world, n, passed=None):
    (world["seed"] / "f").write_text(str(n))
    _git(world["seed"], "commit", "-q", "-am", f"c{n}")
    _git(world["seed"], "push", "-q", "origin", "master")
    sha = _git(world["seed"], "rev-parse", "HEAD")
    if passed is not None:
        (world["checks"] / sha).write_text("".join(f"{name}\n" for name in passed))
    return sha


def _pull(world, grace=7200):
    env = {
        **os.environ,
        "PATH": f"{world['bin']}{os.pathsep}{os.environ['PATH']}",
        "CHECKS_DIR": str(world["checks"]),
        "DEPLOY_GATE_GRACE_SECS": str(grace),
    }
    run = subprocess.run(
        ["bash", str(GATE), "pull", str(world["checkout"])],
        env=env,
        capture_output=True,
        text=True,
    )
    return run.returncode, run.stdout.strip().splitlines()[-1]


def _head(world):
    return _git(world["checkout"], "rev-parse", "HEAD")


def test_a_current_checkout_is_ok(world):
    assert _pull(world)[1].startswith("OK")


def test_a_verified_commit_is_deployed(world):
    sha = _commit(world, 1, REQUIRED)
    rc, verdict = _pull(world)
    assert (rc, verdict.split()[0], _head(world)) == (0, "DEPLOYED", sha)


def test_an_unverified_commit_is_held_and_the_newest_verified_one_taken(world):
    good = _commit(world, 1, REQUIRED)
    _commit(world, 2, REQUIRED[:-1])  # one required check missing
    rc, verdict = _pull(world)
    assert (rc, _head(world)) == (0, good)
    assert "holding" in verdict


def test_nothing_verified_holds_and_pages_after_the_grace(world):
    before = _head(world)
    _commit(world, 1)  # no check runs at all
    assert _pull(world)[0] == 0
    rc, verdict = _pull(world, grace=-1)
    assert (rc, verdict.split()[0], _head(world)) == (1, "HOLD", before)


def test_a_dirty_tree_is_left_alone(world):
    _commit(world, 1, REQUIRED)
    (world["checkout"] / "f").write_text("local edit")
    before = _head(world)
    rc, verdict = _pull(world)
    assert (rc, verdict.split()[0], _head(world)) == (0, "SKIP", before)
