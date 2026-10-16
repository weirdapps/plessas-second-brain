"""The producer's wrappers for topic pages (nightly) and the brief pilot (every 15 minutes).

Run against a fake home whose "python" records its argv, prints what a test asks for and
exits with the code it asks for, and a fake systemctl that records its argv, so the tests
see what each wrapper runs, what it passes through, and when the pilot stops itself.
"""

import subprocess
from pathlib import Path

import pytest

SYSTEMD = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd"
TOPIC_PAGES = SYSTEMD / "sb-topic-pages.sh"
BRIEFS = SYSTEMD / "sb-briefs.sh"


@pytest.fixture
def home(tmp_path):
    (tmp_path / "SourceCode" / "plessas-second-brain").mkdir(parents=True)
    py = tmp_path / ".venvs" / "second-brain" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(
        "#!/bin/bash\n"
        'echo "$@" >> "$HOME/argv"\n'
        'echo "until=${BRAIN_BRIEF_PILOT_UNTIL:-unset}" >> "$HOME/env"\n'
        'printf "%s\\n" "${FAKE_OUT:-}"\n'
        'exit "${FAKE_RC:-0}"\n'
    )
    py.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "systemctl"
    fake.write_text('#!/bin/bash\necho "$@" >> "$HOME/systemctl"\n')
    fake.chmod(0o755)
    return tmp_path


def _run(wrapper, home, **env):
    return subprocess.run(
        ["/bin/bash", str(wrapper)],
        env={"HOME": str(home), "PATH": f"{home / 'bin'}:/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("wrapper", [TOPIC_PAGES, BRIEFS])
def test_it_parses_with_the_interpreter_it_names(wrapper):
    assert wrapper.read_text().startswith("#!/bin/bash\n")
    subprocess.run(["/bin/bash", "-n", str(wrapper)], check=True)


def test_topic_pages_runs_the_nightly_job_and_passes_its_code_through(home):
    assert _run(TOPIC_PAGES, home).returncode == 0
    assert (home / "argv").read_text().strip() == "-m src.cli topic-pages"
    assert _run(TOPIC_PAGES, home, FAKE_RC="1").returncode == 1


@pytest.mark.parametrize("wrapper", [TOPIC_PAGES, BRIEFS])
def test_an_expired_gcloud_credential_skips_the_run(home, wrapper):
    (home / ".second-brain").mkdir()
    (home / ".second-brain" / "needs_gcloud_reauth").touch()

    assert _run(wrapper, home).returncode == 0
    assert not (home / "argv").exists()


def test_an_expired_outlook_session_skips_the_briefs(home):
    (home / ".second-brain").mkdir()
    (home / ".second-brain" / "needs_reauth").touch()

    assert _run(BRIEFS, home).returncode == 0
    assert not (home / "argv").exists()


def test_briefs_reads_the_pilot_settings_file(home):
    settings = home / ".config" / "second-brain" / "briefs.env"
    settings.parent.mkdir(parents=True)
    settings.write_text("BRAIN_BRIEF_PILOT_UNTIL=2026-10-30\n")

    assert _run(BRIEFS, home).returncode == 0
    assert (home / "argv").read_text().strip() == "-m src.cli briefs"
    assert (home / "env").read_text().strip() == "until=2026-10-30"


def test_briefs_passes_a_failure_through(home):
    assert _run(BRIEFS, home, FAKE_RC="1").returncode == 1
    assert not (home / "systemctl").exists()


def test_once_the_pilot_has_ended_the_wrapper_disables_its_own_timer(home):
    out = "briefs: pilot ended on 2026-10-30; nothing sent"

    assert _run(BRIEFS, home, FAKE_OUT=out).returncode == 0
    assert (home / "systemctl").read_text().strip() == "--user disable --now sb-briefs.timer"
    assert "pilot ended" in (home / ".second-brain" / "logs" / "briefs.log").read_text()


def test_the_kill_switch_does_not_disable_the_timer(home):
    out = "briefs: BRAIN_BRIEFS_OFF is set: the kill switch is on; nothing sent"

    assert _run(BRIEFS, home, FAKE_OUT=out).returncode == 0
    assert not (home / "systemctl").exists()
