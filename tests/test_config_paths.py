"""BRAIN_DATA_DIR must relocate the data root (and everything under it)."""

import importlib


def test_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_DATA_DIR", str(tmp_path))
    import src.config as cfg

    importlib.reload(cfg)
    try:
        assert cfg.DATA_ROOT == tmp_path
        assert cfg.DEFAULT_DB == tmp_path / "brain.db"
        assert cfg.ATTACHMENTS_DIR == tmp_path / "attachments"
        assert cfg.RAW_BATCH_DIR == tmp_path / "raw"
        assert cfg.SHAREPOINT_DATA_DIR == tmp_path / "sharepoint"
        assert cfg.CONVERSATION_STAGING_DIR == tmp_path / "staging" / "conversations"
    finally:
        _restore_config(monkeypatch, cfg)


def _restore_config(monkeypatch, cfg):
    """Reload with the session's environment back, not merely this test's var
    removed: reloading with BRAIN_DATA_DIR unset pointed DATA_ROOT at the repo's
    own data/ for every test that ran after this one."""
    monkeypatch.undo()
    importlib.reload(cfg)


def test_data_dir_default(monkeypatch):
    monkeypatch.delenv("BRAIN_DATA_DIR", raising=False)
    import src.config as cfg

    importlib.reload(cfg)
    try:
        assert cfg.DATA_ROOT == cfg.REPO_ROOT / "data"
        assert cfg.DEFAULT_DB == cfg.REPO_ROOT / "data" / "brain.db"
    finally:
        _restore_config(monkeypatch, cfg)


def test_the_config_tests_leave_the_session_data_root_alone():
    """Runs after the two above: DATA_ROOT must be the session's temp dir again."""
    import os

    import src.config as cfg

    assert str(cfg.DATA_ROOT) == os.environ["BRAIN_DATA_DIR"]


def test_an_empty_data_dir_means_the_default(monkeypatch):
    """An empty BRAIN_DATA_DIR= line made the data root the working directory,
    where the wrappers (${BRAIN_DATA_DIR:-...}) saw the default."""
    monkeypatch.setenv("BRAIN_DATA_DIR", "")
    import src.config as cfg

    importlib.reload(cfg)
    try:
        assert cfg.DATA_ROOT == cfg.REPO_ROOT / "data"
    finally:
        _restore_config(monkeypatch, cfg)
