"""The producer's wrapper for topic pages (nightly).

Run against a fake home whose "python" records its argv, prints what a test asks for and
exits with the code it asks for, so the tests see what the wrapper runs and what it
passes through. The brief pilot's wrapper was tested here too until the pilot was
removed on 2026-10-25.
"""

import subprocess
from pathlib import Path

import pytest

SYSTEMD = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd"
TOPIC_PAGES = SYSTEMD / "sb-topic-pages.sh"


@pytest.fixture
def home(tmp_path):
    (tmp_path / "SourceCode" / "plessas-second-brain").mkdir(parents=True)
    py = tmp_path / ".venvs" / "second-brain" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(
        "#!/bin/bash\n"
        'echo "$@" >> "$HOME/argv"\n'
        'printf "%s\\n" "${FAKE_OUT:-}"\n'
        'exit "${FAKE_RC:-0}"\n'
    )
    py.chmod(0o755)
    return tmp_path


def _run(wrapper, home, **env):
    return subprocess.run(
        ["/bin/bash", str(wrapper)],
        env={"HOME": str(home), "PATH": f"{home / 'bin'}:/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
    )


def test_it_parses_with_the_interpreter_it_names():
    assert TOPIC_PAGES.read_text().startswith("#!/bin/bash\n")
    subprocess.run(["/bin/bash", "-n", str(TOPIC_PAGES)], check=True)


def test_topic_pages_runs_the_nightly_job_and_passes_its_code_through(home):
    assert _run(TOPIC_PAGES, home).returncode == 0
    assert (home / "argv").read_text().strip() == "-m src.cli topic-pages"
    assert _run(TOPIC_PAGES, home, FAKE_RC="1").returncode == 1


def test_an_expired_gcloud_credential_skips_the_run(home):
    """69, not 0: a skip is work not done (tests/test_wrapper_sentinel_skips_are_red.py)."""
    (home / ".second-brain").mkdir()
    (home / ".second-brain" / "needs_gcloud_reauth").touch()

    assert _run(TOPIC_PAGES, home).returncode == 69
    assert not (home / "argv").exists()
