"""The MCP server carries its own settings instead of borrowing the launching shell's.

The embedding backend and its key lived only in a login shell's profile. Claude
Code starts the server with whatever environment it was started with, so a
server under an agent-team session had neither: the backend fell back to
Vertex, which refused the model, and recall said `semantic: unavailable` at the
end of a 40K-character payload, which nobody saw.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FAKE_PYTHON = """#!/bin/sh
echo "backend=${BRAIN_EMBED_BACKEND:-unset}"
echo "key=${GEMINI_API_KEY:-unset}"
echo "args=$*"
"""


def _launch(tmp_path, *, data_dir: Path | None = None, extra: dict | None = None):
    """run_mcp.sh with a stand-in python that prints what it was handed."""
    fake = tmp_path / "fake-python"
    fake.write_text(FAKE_PYTHON)
    fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "SECOND_BRAIN_VENV_PYTHON": str(fake),
        **(extra or {}),
    }
    if data_dir is not None:
        env["BRAIN_DATA_DIR"] = str(data_dir)
    return subprocess.run(
        ["bash", str(REPO / "run_mcp.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _env_file(directory: Path, mode: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "env"
    path.write_text(
        "# server settings\nBRAIN_EMBED_BACKEND=gemini\nexport GEMINI_API_KEY=placeholder\n"
    )
    path.chmod(mode)
    return path


def test_the_launcher_reads_an_owner_only_env_file_from_the_default_home(tmp_path):
    _env_file(tmp_path / "home" / ".second-brain", 0o600)

    out = _launch(tmp_path)

    assert out.returncode == 0, out.stderr
    assert "backend=gemini" in out.stdout
    assert "key=placeholder" in out.stdout
    assert "args=-m src.mcp_server" in out.stdout


def test_the_launcher_reads_the_env_file_from_brain_data_dir(tmp_path):
    data = tmp_path / "data"
    _env_file(data, 0o600)

    out = _launch(tmp_path, data_dir=data)

    assert "backend=gemini" in out.stdout


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660], ids=oct)
def test_the_launcher_refuses_an_env_file_others_can_read(tmp_path, mode):
    path = _env_file(tmp_path / "home" / ".second-brain", mode)

    out = _launch(tmp_path)

    # The server still starts, without the file, and says why on stderr.
    assert out.returncode == 0
    assert "backend=unset" in out.stdout
    assert "key=unset" in out.stdout
    assert str(path) in out.stderr
    assert "chmod 600" in out.stderr


def test_the_launcher_starts_without_an_env_file(tmp_path):
    out = _launch(tmp_path, extra={"BRAIN_EMBED_BACKEND": "vertex"})

    assert out.returncode == 0
    assert "backend=vertex" in out.stdout
    assert out.stderr == ""


def test_the_env_file_is_read_through_a_symlink_to_an_owner_only_file(tmp_path):
    target = _env_file(tmp_path / "private", 0o600)
    link_dir = tmp_path / "home" / ".second-brain"
    link_dir.mkdir(parents=True)
    (link_dir / "env").symlink_to(target)

    out = _launch(tmp_path)

    assert "backend=gemini" in out.stdout
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


# ------------------------------------------------------------- backend default


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("BRAIN_EMBED_BACKEND", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return monkeypatch


def test_a_gemini_key_with_no_backend_set_picks_gemini(clean_env):
    from src.config import embed_backend

    clean_env.setenv("GEMINI_API_KEY", "placeholder")

    assert embed_backend() == "gemini"


def test_no_key_and_no_backend_keeps_vertex(clean_env):
    from src.config import embed_backend

    assert embed_backend() == "vertex"


def test_an_explicit_backend_wins_over_a_present_key(clean_env):
    from src.config import embed_backend

    clean_env.setenv("GEMINI_API_KEY", "placeholder")
    clean_env.setenv("BRAIN_EMBED_BACKEND", " Vertex ")

    assert embed_backend() == "vertex"


# --------------------------------------------------------------- stats, log


def _store(tmp_path):
    from src.store.schema import create_database, get_connection

    path = tmp_path / "brain.db"
    create_database(str(path)).close()
    return lambda: get_connection(str(path))


def test_stats_names_the_backend_and_has_no_error_before_one_happens(
    tmp_path, clean_env, monkeypatch
):
    import src.store.embeddings as embeddings
    from src import mcp_server

    clean_env.setenv("GEMINI_API_KEY", "placeholder")
    monkeypatch.setattr(embeddings, "_LAST_EMBED_ERROR", None)
    monkeypatch.setattr(mcp_server, "_get_conn", _store(tmp_path))

    out = mcp_server.stats()

    assert out["embed_backend"] == "gemini"
    assert out["last_embed_error"] is None


def test_stats_reports_the_last_embedding_failure_by_type_and_time_only(
    tmp_path, clean_env, monkeypatch
):
    import src.store.embeddings as embeddings
    from src import mcp_server

    class _Refused(Exception):
        code = 403

    class _Client:
        def __init__(self):
            self.models = self

        def embed_content(self, model, contents, config=None):
            raise _Refused("403 PERMISSION_DENIED for the query 'quarterly figures'")

    monkeypatch.setattr(embeddings, "_LAST_EMBED_ERROR", None)
    monkeypatch.setattr(mcp_server, "_get_conn", _store(tmp_path))
    with pytest.raises(_Refused):
        embeddings.embed_query(["quarterly figures"], client=_Client())

    error = mcp_server.stats()["last_embed_error"]

    assert set(error) == {"type", "at"}
    assert error["type"] == "_Refused"
    assert error["at"].endswith("Z")
    assert "quarterly" not in str(error)


def test_the_server_logs_its_embedding_backend_at_startup(clean_env, capsys, monkeypatch):
    from unittest.mock import patch

    from src import mcp_server

    clean_env.setenv("GEMINI_API_KEY", "placeholder")
    with patch.object(mcp_server.mcp, "run"):
        assert mcp_server.main([]) == 0

    err = capsys.readouterr().err
    assert "gemini" in err
    assert "placeholder" not in err
