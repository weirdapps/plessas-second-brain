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

FAKE_GH = r"""#!/bin/sh
# Answers the four `gh api` reads the gate makes, ignoring --jq (the files hold the
# post-jq answer): check runs from a file per commit, a squash commit's PR head from
# pulls-<sha>, and a commit's tree and a compare status from the seed repository.
path="$2"
case "$path" in
  */check-runs*)
    sha=$(printf '%s' "$path" | sed -E 's#.*/commits/([0-9a-f]+)/.*#\1#')
    [ -f "$CHECKS_DIR/$sha" ] && cat "$CHECKS_DIR/$sha" ;;
  */pulls)
    sha=$(printf '%s' "$path" | sed -E 's#.*/commits/([0-9a-f]+)/pulls#\1#')
    [ -f "$CHECKS_DIR/pulls-$sha" ] && cat "$CHECKS_DIR/pulls-$sha" ;;
  */compare/*)
    range=${path##*/compare/}
    if git -C "$SEED" merge-base --is-ancestor "${range%%...*}" "${range##*...}"; then
      echo ahead
    else
      echo diverged
    fi ;;
  */commits/*)
    git -C "$SEED" rev-parse "${path##*/commits/}^{tree}" ;;
esac
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
        "SEED": str(world["seed"]),
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


def _squash_merge(world, files, base=None, passed=REQUIRED, squash_files=None):
    """A PR branch off `base` (default: master) writing `files`, and the squash commit
    auto-merge pushes to master with no check runs of its own. Returns (squash, pr_head)."""
    seed = world["seed"]
    _git(seed, "checkout", "-q", "-b", "pr", base or "master")
    for name, text in files.items():
        (seed / name).write_text(text)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "pr")
    pr_head = _git(seed, "rev-parse", "HEAD")
    if passed is not None:
        (world["checks"] / pr_head).write_text("".join(f"{name}\n" for name in passed))
    _git(seed, "checkout", "-q", "master")
    for name, text in (squash_files or files).items():
        (seed / name).write_text(text)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "squash (#1)")
    _git(seed, "push", "-q", "origin", "master")
    squash = _git(seed, "rev-parse", "HEAD")
    (world["checks"] / f"pulls-{squash}").write_text(pr_head + "\n")
    return squash, pr_head


def test_a_squash_merge_is_verified_by_its_pr_heads_checks(world):
    squash, _ = _squash_merge(world, {"f": "bumped"})
    rc, verdict = _pull(world)
    assert (rc, verdict.split()[0], _head(world)) == (0, "DEPLOYED", squash)


def test_a_squash_merge_whose_pr_head_failed_is_held(world):
    before = _head(world)
    _squash_merge(world, {"f": "bumped"}, passed=REQUIRED[:-1])
    rc, verdict = _pull(world, grace=-1)
    assert (rc, verdict.split()[0], _head(world)) == (1, "HOLD", before)


def test_a_squash_merge_with_other_content_than_the_tested_head_is_held(world):
    before = _head(world)
    _squash_merge(world, {"f": "bumped"}, squash_files={"f": "something else"})
    rc, verdict = _pull(world, grace=-1)
    assert (rc, verdict.split()[0], _head(world)) == (1, "HOLD", before)


def test_a_squash_merge_onto_a_base_the_pr_head_never_contained_is_held(world):
    old_base = _head(world)
    _commit(world, 1, REQUIRED)
    _pull(world)  # deploys c1
    deployed = _head(world)
    # Same final tree, but the PR was tested on c0 and master moved to c1 meanwhile.
    _squash_merge(world, {"f": "bumped"}, base=old_base, squash_files={"f": "bumped"})
    rc, verdict = _pull(world, grace=-1)
    assert (rc, verdict.split()[0], _head(world)) == (1, "HOLD", deployed)
