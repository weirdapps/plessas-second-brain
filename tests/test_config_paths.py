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
        monkeypatch.delenv("BRAIN_DATA_DIR", raising=False)
        importlib.reload(cfg)


def test_data_dir_default(monkeypatch):
    monkeypatch.delenv("BRAIN_DATA_DIR", raising=False)
    import src.config as cfg

    importlib.reload(cfg)
    assert cfg.DATA_ROOT == cfg.REPO_ROOT / "data"
    assert cfg.DEFAULT_DB == cfg.REPO_ROOT / "data" / "brain.db"
