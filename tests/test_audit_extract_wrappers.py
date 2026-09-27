"""./brain and ./run_extract.sh find the interpreter the way run_mcp.sh does.

Both accepted only an in-repo .venv, which the producer does not have (its venv
is ~/.venvs/second-brain), so neither ran on the one host where writes are
allowed. brain's hint then told the operator to pip-install an unlocked venv
that bypasses uv.lock, and run_extract.sh documented a --concurrency flag that
src.extract.local rejects. Finding cli-7.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _fake_python(path: Path) -> Path:
    """An 'interpreter' that reports how it was called."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\necho "fake-python $*"\n')
    path.chmod(0o755)
    return path


def _copy(script: str, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / script
    shutil.copy2(REPO / script, target)
    return target


def _run(script: Path, *args: str, env_extra: dict[str, str], home: Path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), **env_extra}
    return subprocess.run(
        ["bash", str(script), *args], capture_output=True, text=True, env=env, timeout=30
    )


@pytest.mark.parametrize(
    ("script", "module"), [("brain", "src.cli"), ("run_extract.sh", "src.extract.local")]
)
def test_uses_the_explicit_override(tmp_path, script, module):
    py = _fake_python(tmp_path / "override" / "python")
    result = _run(
        _copy(script, tmp_path / "repo"),
        "--limit",
        "3",
        env_extra={"SECOND_BRAIN_VENV_PYTHON": str(py)},
        home=tmp_path / "home",
    )
    assert result.returncode == 0, result.stderr
    assert f"fake-python -m {module} --limit 3" in result.stdout


@pytest.mark.parametrize("script", ["brain", "run_extract.sh"])
def test_finds_the_shared_venv_the_producer_uses(tmp_path, script):
    home = tmp_path / "home"
    _fake_python(home / ".venvs" / "second-brain" / "bin" / "python")
    result = _run(_copy(script, tmp_path / "repo"), "stats", env_extra={}, home=home)
    assert result.returncode == 0, result.stderr
    assert "fake-python -m" in result.stdout


@pytest.mark.parametrize("script", ["brain", "run_extract.sh"])
def test_prefers_the_in_repo_venv(tmp_path, script):
    repo = tmp_path / "repo"
    _fake_python(repo / ".venv" / "bin" / "python")
    result = _run(_copy(script, repo), env_extra={}, home=tmp_path / "home")
    assert result.returncode == 0, result.stderr
    assert "fake-python -m" in result.stdout


@pytest.mark.parametrize("script", ["brain", "run_extract.sh"])
def test_no_interpreter_says_uv_sync_frozen(tmp_path, script):
    result = _run(_copy(script, tmp_path / "repo"), env_extra={}, home=tmp_path / "home")
    assert result.returncode != 0
    assert "uv sync --frozen" in result.stderr
    assert "pip install" not in result.stderr


def test_run_extract_usage_names_the_flags_local_accepts():
    text = (REPO / "run_extract.sh").read_text()
    assert "[--workers N] [--limit N]" in text
    assert "--concurrency" not in text


def test_scripts_stay_executable():
    for script in ("brain", "run_extract.sh"):
        assert os.access(REPO / script, os.X_OK)
