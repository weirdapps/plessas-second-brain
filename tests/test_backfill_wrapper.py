"""The producer's temporary backfill wrapper (sb-backfill, nightly until done).

It runs the topic registry (issue 180) and the vectors backfill (issue 171) in capped
batches, reads what is left from each command's dry run first, and disables its own
timer once both report nothing left. Run against a fake home whose "python" answers the
dry runs with the counts a test asks for, and whose "systemctl" logs its arguments.
Every value is invented.
"""

import subprocess
from pathlib import Path

import pytest

WRAPPER = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd" / "sb-backfill.sh"

REGISTRY_DRY = "-m src.cli maint topic-registry --limit 1 --llm-calls 0 --show 0"
REGISTRY_APPLY = "-m src.cli maint topic-registry --limit 25000 --llm-calls 20 --apply"
VECTORS_DRY = "-m src.cli maint vectors-backfill --summaries --limit 1"
VECTORS_APPLY = "-m src.cli maint vectors-backfill --apply --limit 2000"
SUMMARIES_APPLY = "-m src.cli maint vectors-backfill --apply --summaries --limit 5000"
DISABLE = "--user disable --now sb-backfill.timer"


@pytest.fixture
def home(tmp_path):
    (tmp_path / "SourceCode" / "plessas-second-brain").mkdir(parents=True)
    py = tmp_path / ".venvs" / "second-brain" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(
        "#!/bin/bash\n"
        'echo "$*" >> "$HOME/argv"\n'
        'case "$*" in\n'
        '  *"topic-registry --limit 1 "*)\n'
        '    [ -n "${BAD_DRY:-}" ] && { echo "Traceback: boom"; exit 1; }\n'
        "    echo \"topic registry: {'joined': 0} (dry run; --apply writes)\"\n"
        "    echo \"after: {'canonical_topics': 3, 'unplaced': ${UNPLACED:-10}}\" ;;\n"
        '  *"vectors-backfill --summaries --limit 1")\n'
        '    echo "vectors-backfill (dry run, nothing written; --apply writes):"\n'
        '    echo "  email chunks: ${EMAILS:-5} emails, 9 chunks waiting"\n'
        '    echo "  copy chunks to drop: ${COPIES:-0}"\n'
        '    echo "  summaries at 1536: ${SUMS:-1} this run" ;;\n'
        '  *--apply*) exit "${APPLY_RC:-0}" ;;\n'
        "esac\n"
        "exit 0\n"
    )
    py.chmod(0o755)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "systemctl").write_text('#!/bin/bash\necho "$*" >> "$HOME/systemctl.log"\n')
    (tools / "systemctl").chmod(0o755)
    return tmp_path


def _run(home, **env):
    return subprocess.run(
        ["/bin/bash", str(WRAPPER)],
        env={"HOME": str(home), "PATH": f"{home / 'tools'}:/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _argv(home):
    path = home / "argv"
    return path.read_text().splitlines() if path.exists() else []


def _disabled(home):
    path = home / "systemctl.log"
    return path.exists() and DISABLE in path.read_text()


def _log(home):
    return (home / ".second-brain" / "logs" / "backfill.log").read_text()


def test_it_parses_with_the_interpreter_it_names():
    assert WRAPPER.read_text().startswith("#!/bin/bash\n")
    subprocess.run(["/bin/bash", "-n", str(WRAPPER)], check=True)


def test_with_work_left_in_both_it_counts_first_then_runs_one_batch_of_each(home):
    result = _run(home)

    assert result.returncode == 0, result.stderr
    assert _argv(home) == [REGISTRY_DRY, REGISTRY_APPLY, VECTORS_DRY, VECTORS_APPLY]
    assert not _disabled(home)


def test_the_summaries_wait_until_no_email_or_copy_chunk_is_left(home):
    assert _run(home, EMAILS="0", COPIES="0", SUMS="1").returncode == 0
    assert _argv(home)[-1] == SUMMARIES_APPLY

    (home / "argv").unlink()
    assert _run(home, EMAILS="0", COPIES="7", SUMS="1").returncode == 0
    assert _argv(home)[-1] == VECTORS_APPLY


def test_a_finished_registry_is_not_run_again(home):
    assert _run(home, UNPLACED="0").returncode == 0
    assert REGISTRY_APPLY not in _argv(home)
    assert VECTORS_APPLY in _argv(home)
    assert not _disabled(home)


def test_when_both_report_nothing_left_it_disables_its_own_timer(home):
    result = _run(home, UNPLACED="0", EMAILS="0", COPIES="0", SUMS="0")

    assert result.returncode == 0
    assert _argv(home) == [REGISTRY_DRY, VECTORS_DRY]
    assert _disabled(home)
    assert "nothing left" in _log(home) and "disabled sb-backfill.timer" in _log(home)


def test_a_failed_batch_fails_the_unit_but_the_other_still_runs(home):
    result = _run(home, APPLY_RC="1")

    assert result.returncode == 1
    assert REGISTRY_APPLY in _argv(home) and VECTORS_APPLY in _argv(home)
    assert not _disabled(home)


def test_a_count_it_cannot_read_fails_and_never_disables(home):
    """An unreadable dry run is not a zero: reading it as one would stop the backfill."""
    result = _run(home, BAD_DRY="1", EMAILS="0", COPIES="0", SUMS="0")

    assert result.returncode == 1
    assert REGISTRY_APPLY not in _argv(home)
    assert not _disabled(home)


def test_an_expired_gcloud_credential_skips_the_run(home):
    """69, not 0: a skip is work not done (tests/test_wrapper_sentinel_skips_are_red.py)."""
    (home / ".second-brain").mkdir()
    (home / ".second-brain" / "needs_gcloud_reauth").touch()

    assert _run(home).returncode == 69
    assert _argv(home) == []
    assert "needs_gcloud_reauth" in _log(home)
