"""scripts/wrappers/systemd/sb-whatsapp-sync.sh, the producer's :55 timer job.

Run against a fake home whose "python" records its argv and exits with the code
a test asks for, so the tests see what the wrapper runs and what it reports.
"""

import json
import subprocess
from pathlib import Path

import pytest

WRAPPER = Path(__file__).parents[2] / "scripts" / "wrappers" / "systemd" / "sb-whatsapp-sync.sh"


@pytest.fixture
def home(tmp_path):
    (tmp_path / "SourceCode" / "plessas-second-brain").mkdir(parents=True)
    py = tmp_path / ".venvs" / "second-brain" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(
        '#!/bin/bash\necho "$@" >> "$HOME/argv"\necho "Step 1/4: 2 new"\nexit "${FAKE_RC:-0}"\n'
    )
    py.chmod(0o755)
    return tmp_path


def _run(home, **env):
    return subprocess.run(
        ["/bin/bash", str(WRAPPER)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
    )


def test_it_runs_whatsapp_sync_and_passes_its_code_through(home):
    assert _run(home).returncode == 0
    assert "-m src.cli whatsapp-sync" in (home / "argv").read_text()
    assert _run(home, FAKE_RC="66").returncode == 66


def test_failures_are_counted_in_its_state_file(home):
    _run(home, FAKE_RC="66")
    _run(home, FAKE_RC="66")
    state = json.loads((home / ".second-brain" / "whatsapp_sync_wrapper.json").read_text())
    assert state["consecutive_failures"] == 2
    _run(home)
    state = json.loads((home / ".second-brain" / "whatsapp_sync_wrapper.json").read_text())
    assert state["consecutive_failures"] == 0


def test_an_expired_gcloud_credential_skips_the_run(home):
    (home / ".second-brain").mkdir()
    (home / ".second-brain" / "needs_gcloud_reauth").touch()
    assert _run(home).returncode == 0
    assert not (home / "argv").exists()


def test_it_parses_with_the_interpreter_it_names():
    assert WRAPPER.read_text().startswith("#!/bin/bash\n")
    subprocess.run(["/bin/bash", "-n", str(WRAPPER)], check=True)
