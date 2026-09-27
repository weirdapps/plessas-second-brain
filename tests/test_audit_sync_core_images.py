"""Step 8's line says how many images failed.

run_backfill returns a failed count, and sync printed classified, deferred and
missing only: a run in which every image failed read "No unclassified images".
"""

import types
from unittest.mock import patch

import pytest


def _sync(tmp_path, monkeypatch, image_stats):
    from src import cli
    from src.store.schema import create_database

    db_path = tmp_path / "brain.db"
    conn = create_database(str(db_path))
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("src.cli.DATA_ROOT", tmp_path)
    args = types.SimpleNamespace(
        db=db_path, limit=None, engine="claude", workers=1, skip_export=True
    )
    ok = {"extracted": 0, "failed": 0, "quota_paused": False}
    with (
        patch("src.extract.local.run_extraction", return_value=ok),
        patch("src.store.loader.load_extractions", return_value=0),
        patch("src.extract.attachment_pipeline.run_phase1", return_value={"processed": 0}),
        patch("src.export.conversation_export.export_conversations", return_value={"exported": 0}),
        patch("src.extract.image_pipeline.run_backfill", return_value=image_stats),
    ):
        cli.cmd_sync(args)


def test_a_run_in_which_every_image_failed_says_so(tmp_path, monkeypatch, capsys):
    stats = {"scanned": 200, "classified": 0, "missing": 0, "failed": 200, "deferred": 0}

    _sync(tmp_path, monkeypatch, stats)

    out = capsys.readouterr().out
    assert "No unclassified images" not in out
    assert "failed: 200" in out


@pytest.mark.parametrize("failed", [0, 3])
def test_the_line_carries_the_failed_count(tmp_path, monkeypatch, capsys, failed):
    stats = {"scanned": 9, "classified": 6, "missing": 0, "failed": failed, "deferred": 0}

    _sync(tmp_path, monkeypatch, stats)

    assert f"failed: {failed}" in capsys.readouterr().out


def test_nothing_to_do_still_reads_as_nothing_to_do(tmp_path, monkeypatch, capsys):
    stats = {"scanned": 0, "classified": 0, "missing": 0, "failed": 0, "deferred": 0}

    _sync(tmp_path, monkeypatch, stats)

    assert "No unclassified images" in capsys.readouterr().out
