"""pii-gauntlet --mode=history: every line and filename ever added, on every ref.

Both existing modes read the current tree only. On 2026-09-23 the root commit of
this public repo still carried the old in-script denylist, reachable from master,
while the gauntlet printed PASS on every push: the fix that moved the denylist out
had removed it from the tree and nowhere else. History mode is the check that can
see that. It is local-only (it needs the private denylist) and prints the commit
and the check label, never the matched text.

The planted marker is an invented token fed through a temporary denylist, so this
file needs no real-looking PII to exercise the scanner.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "pii-gauntlet.sh"
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


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "--short", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "pii-gauntlet.sh")
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "init")
    return root


@pytest.fixture
def denylist(tmp_path):
    path = tmp_path / "denylist.conf"
    path.write_text(f"Planted marker\t{MARKER}\t\n")
    return path


def _run(repo: Path, denylist_path: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("CI", "GITHUB_ACTIONS")}
    env["PII_DENYLIST"] = str(denylist_path)
    return subprocess.run(
        ["bash", "scripts/pii-gauntlet.sh", "--mode=history"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


def test_a_clean_history_passes(repo, denylist):
    """The script's own text is full of the generic patterns; it must not
    convict its own history."""
    result = _run(repo, denylist)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "GAUNTLET PASS" in result.stdout


def test_content_removed_from_the_tree_is_still_found_in_history(repo, denylist):
    (repo / "notes.txt").write_text(f"contact {MARKER} here\n")
    planted = _commit(repo, "plant")
    (repo / "notes.txt").write_text("nothing to see\n")
    _commit(repo, "remove")

    result = _run(repo, denylist)

    assert result.returncode == 1
    assert "FAIL [Planted marker]" in result.stdout
    assert planted in result.stdout
    assert MARKER not in result.stdout  # names the commit, never the text


def test_the_gauntlet_script_itself_is_scanned_for_denylist_terms(repo, denylist):
    """The real leak lived inside an old version of this very script."""
    script = repo / "scripts" / "pii-gauntlet.sh"
    original = script.read_text()
    script.write_text(original + f"\n# check 'Colleague' '{MARKER}'\n")
    planted = _commit(repo, "inline denylist")
    script.write_text(original)
    _commit(repo, "move the denylist out")

    result = _run(repo, denylist)

    assert result.returncode == 1
    assert planted in result.stdout


def test_a_filename_ever_added_is_found(repo, denylist):
    (repo / f"{MARKER}.png").write_bytes(b"\x89PNG")
    planted = _commit(repo, "binary with a telling name")
    (repo / f"{MARKER}.png").unlink()
    _commit(repo, "remove it")

    result = _run(repo, denylist)

    assert result.returncode == 1
    assert planted in result.stdout


def test_history_mode_without_the_denylist_refuses(repo, tmp_path):
    result = _run(repo, tmp_path / "absent.conf")
    assert result.returncode == 1
    assert "no denylist" in result.stdout


def test_content_added_and_removed_on_a_merged_branch_is_found(repo, denylist):
    _git(repo, "switch", "-q", "-c", "feature")
    (repo / "notes.txt").write_text(f"{MARKER}\n")
    planted = _commit(repo, "plant on a branch")
    (repo / "notes.txt").write_text("clean\n")
    _commit(repo, "clean on the branch")
    _git(repo, "switch", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    _git(repo, "branch", "-q", "-D", "feature")

    result = _run(repo, denylist)

    assert result.returncode == 1
    assert planted in result.stdout


def test_the_generic_stream_does_not_simplify_merge_history():
    """A pathspec turns on history simplification, which skips the side of a
    merge that ends up tree-identical, so PII added and removed inside a merged
    PR vanished from every generic check. --full-history keeps it."""
    text = SCRIPT.read_text()
    generic_log = text[text.index("HISTORY_NOSELF=$(mktemp)") :].split('> "$HISTORY_NOSELF"')[0]
    assert "--full-history" in generic_log


def test_a_line_that_is_not_valid_utf8_is_still_scanned(repo, denylist):
    """A UTF-8 locale grep silently skips lines with invalid bytes, so a legacy
    cp1253 or Latin-1 file was invisible to every check."""
    (repo / "legacy.csv").write_bytes(b"\xff\xfe name;" + MARKER.encode() + b"\n")
    planted = _commit(repo, "legacy encoded file")
    (repo / "legacy.csv").unlink()
    _commit(repo, "remove it")

    result = _run(repo, denylist)

    assert result.returncode == 1
    assert planted in result.stdout


def test_pull_request_heads_on_origin_are_scanned_and_cleaned_up(repo, denylist, tmp_path):
    """--all sees local refs only. On GitHub a closed PR's head stays reachable
    through refs/pull/N/head, including after a history rewrite, so history mode
    fetches those heads into temporary refs and deletes them afterwards."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "switch", "-q", "-c", "pr")
    (repo / "notes.txt").write_text(f"{MARKER}\n")
    planted = _commit(repo, "only ever in a pull request")
    _git(repo, "push", "-q", "origin", "HEAD:refs/pull/7/head")
    _git(repo, "switch", "-q", "main")
    _git(repo, "branch", "-q", "-D", "pr")
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "gc", "-q", "--prune=now")

    result = _run(repo, denylist)

    assert result.returncode == 1
    assert planted in result.stdout
    assert _git(repo, "for-each-ref", "refs/gauntlet-pr/") == ""
