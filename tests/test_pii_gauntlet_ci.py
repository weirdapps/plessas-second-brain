"""pii-gauntlet in CI: the private denylist arrives as a repository secret.

The name checks used to run only in a local pre-commit hook, which never runs for
a commit made on GitHub, by Dependabot, or in a clone without the hook, so CI
printed SKIP for all of them on every push (code-tests-ci-02). The CI job now
writes the `PII_DENYLIST` secret to a private temporary file and hands the
gauntlet its path. GitHub withholds secrets from forks and Dependabot, so those
runs still SKIP, as a colleague's clone does.

The planted marker is an invented token, as in test_pii_gauntlet_history.py.
"""

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "pii-gauntlet.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
MARKER = "zqxplantedmarkerzqx"


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    done = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "pii-gauntlet.sh")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _gauntlet_step() -> tuple[str, str]:
    """(the variable the secret is passed in, the step's script) from ci.yml."""
    lines = WORKFLOW.read_text().splitlines()
    job = lines[lines.index("  pii-gauntlet:") :]
    job = job[: next(i for i, line in enumerate(job[1:], 1) if re.match(r"  \S", line))]
    variable = next(
        m[1]
        for line in job
        if (m := re.match(r"\s+(\w+): \$\{\{ secrets\.PII_DENYLIST \}\}$", line))
    )
    start = next(i for i, line in enumerate(job) if line.strip() == "run: |") + 1
    indent = len(job[start - 1]) - len(job[start - 1].lstrip())
    body = []
    for line in job[start:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return variable, textwrap.dedent("\n".join(body))


def _run_step(repo: Path, secret: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """The step as the runner executes it: bash -e, CI set, a private temp dir."""
    variable, script = _gauntlet_step()
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "PII_DENYLIST"}
    env.update({"CI": "true", "GITHUB_ACTIONS": "true", "RUNNER_TEMP": str(runner_temp)})
    env["HOME"] = str(tmp_path / "home")  # never the developer's own denylist
    env[variable] = secret
    return subprocess.run(
        ["bash", "-e", "-c", script], cwd=repo, capture_output=True, text=True, env=env
    )


def test_the_secret_reaches_the_gauntlet_as_a_private_file_removed_afterwards(repo, tmp_path):
    stub = repo / "scripts" / "pii-gauntlet.sh"
    stub.write_text(
        '#!/usr/bin/env bash\nls -l "$PII_DENYLIST" | cut -c1-10\n'
        'wc -l < "$PII_DENYLIST" | tr -d " "\necho "$PII_DENYLIST"\n'
    )

    result = _run_step(repo, f"First\t{MARKER}\t\nSecond\tzqxother\t", tmp_path)

    assert result.returncode == 0, result.stderr
    mode, lines, path = result.stdout.split()
    assert mode == "-rw-------"
    assert lines == "2"
    assert not Path(path).exists()


def test_without_the_secret_the_name_checks_are_skipped_and_the_run_passes(repo, tmp_path):
    result = _run_step(repo, "", tmp_path)

    assert result.returncode == 0, result.stdout
    assert "SKIP [name-based checks]" in result.stdout


def test_with_the_secret_a_hit_fails_and_prints_its_location_only(repo, tmp_path):
    (repo / "notes.txt").write_text(f"clean\ncontact {MARKER} here\n")
    _git(repo, "add", "notes.txt")

    result = _run_step(repo, f"Planted marker\t{MARKER}\t", tmp_path)

    assert result.returncode == 1, result.stdout
    assert "FAIL [Planted marker]" in result.stdout
    assert "notes.txt:2" in result.stdout
    assert MARKER not in result.stdout
    assert "(1 name-based checks loaded" in result.stdout


def test_a_pattern_that_does_not_compile_fails_instead_of_passing(repo, tmp_path):
    """grep exits 2 on a bad pattern, the error went to /dev/null, and the check
    printed OK having searched for nothing. GNU grep on the runner and BSD grep on
    a Mac do not accept the same patterns, so moving the denylist into CI is where
    this would first show up."""
    denylist = tmp_path / "bad.conf"
    denylist.write_text("Broken entry\tzq(x\t\n")
    env = {k: v for k, v in os.environ.items() if k not in ("CI", "GITHUB_ACTIONS")}
    env["PII_DENYLIST"] = str(denylist)

    result = subprocess.run(
        ["bash", "scripts/pii-gauntlet.sh", "--mode=ci"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1, result.stdout
    assert "FAIL [Broken entry]" in result.stdout
