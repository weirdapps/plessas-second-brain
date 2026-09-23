"""BRAIN_DATA_DIR must relocate the data root (and everything under it)."""

import importlib
from pathlib import Path
from types import SimpleNamespace


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


# --- Document roots ----------------------------------------------------------
# Two layouts are live at once: the Mac keeps National/ and Personal/ under
# OneDrive's CloudStorage path, the VPS keeps them in a plain ~/Documents tree
# the laptop push writes into. Hardcoding ~/Documents made every Mac-side check
# report the roots MISSING from 2026-09-08, when macOS replaced the OneDrive
# symlink with an ordinary local folder. Resolve, do not assume.


def _plant(base, tree):
    (base / tree).mkdir(parents=True)
    return base


def test_document_root_base_prefers_cloudstorage(monkeypatch, tmp_path):
    import src.config as cfg

    cloud = _plant(tmp_path / "CloudStorage", "National")
    legacy = _plant(tmp_path / "Documents", "National")
    monkeypatch.setattr(cfg, "DOCUMENT_ROOT_CANDIDATES", (cloud, legacy))
    monkeypatch.delenv("BRAIN_DOCUMENT_ROOT", raising=False)

    assert cfg.document_root_base() == cloud


def test_document_root_base_falls_back_to_legacy_documents(monkeypatch, tmp_path):
    """The VPS has no CloudStorage path at all, and must still resolve."""
    import src.config as cfg

    cloud = tmp_path / "CloudStorage"
    legacy = _plant(tmp_path / "Documents", "Personal")
    monkeypatch.setattr(cfg, "DOCUMENT_ROOT_CANDIDATES", (cloud, legacy))
    monkeypatch.delenv("BRAIN_DOCUMENT_ROOT", raising=False)

    assert cfg.document_root_base() == legacy


def test_document_root_base_without_any_tree_returns_last_candidate(monkeypatch, tmp_path):
    """Neither layout present: report a stable MISSING path, do not raise."""
    import src.config as cfg

    cloud = tmp_path / "CloudStorage"
    legacy = tmp_path / "Documents"
    monkeypatch.setattr(cfg, "DOCUMENT_ROOT_CANDIDATES", (cloud, legacy))
    monkeypatch.delenv("BRAIN_DOCUMENT_ROOT", raising=False)

    assert cfg.document_root_base() == legacy


def test_document_root_env_override_wins(monkeypatch, tmp_path):
    import src.config as cfg

    cloud = _plant(tmp_path / "CloudStorage", "National")
    monkeypatch.setattr(cfg, "DOCUMENT_ROOT_CANDIDATES", (cloud,))
    monkeypatch.setenv("BRAIN_DOCUMENT_ROOT", "~/elsewhere")

    assert cfg.document_root_base() == Path.home() / "elsewhere"


def test_document_roots_are_the_two_trees(monkeypatch, tmp_path):
    import src.config as cfg

    base = _plant(tmp_path / "Documents", "National")
    monkeypatch.setattr(cfg, "DOCUMENT_ROOT_CANDIDATES", (base,))
    monkeypatch.delenv("BRAIN_DOCUMENT_ROOT", raising=False)

    assert cfg.document_roots() == [base / "National", base / "Personal"]


def test_reverse_ingest_defaults_to_config_document_roots(monkeypatch, tmp_path, capsys):
    """cmd_reverse_ingest must scan whatever the resolver returns, so the health
    check and the job it polices can never watch different directories."""
    import src.cli as cli
    import src.config as cfg
    import src.ingest.reverse_scan as rs

    base = _plant(tmp_path / "Documents", "National")
    monkeypatch.setattr(cfg, "DOCUMENT_ROOT_CANDIDATES", (base,))
    monkeypatch.delenv("BRAIN_DOCUMENT_ROOT", raising=False)

    seen = {}

    def fake_scan_roots(roots, extensions):
        seen["roots"] = list(roots)
        return []

    monkeypatch.setattr(rs, "scan_roots", fake_scan_roots)

    cli.cmd_reverse_ingest(SimpleNamespace(root=None, dry_run=True))
    capsys.readouterr()

    assert seen["roots"] == cfg.document_roots()
