"""Integration test for `brain reverse-ingest` CLI subcommand."""

import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src.cli import cmd_reverse_ingest


def _touch(path: Path, ts: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 stub")
    epoch = datetime.strptime(ts, "%Y%m%d%H%M").timestamp()
    os.utime(path, (epoch, epoch))
    return path


def test_reverse_ingest_dry_run_lists_survivors_without_ingesting(tmp_path, capsys):
    """--dry-run prints what would be ingested but does not call ingest_document."""
    root = tmp_path / "Documents" / "National"
    _touch(root / "cards" / "PD2024EN.pdf", "202401151430")
    _touch(root / "cards" / "202504171123_q2.pdf", "202504171123")
    _touch(
        root / "cards" / "202507221530_q2.pdf", "202507221530"
    )  # latest of two prefixed siblings

    args = type(
        "Args",
        (),
        {
            "root": [root],
            "workers": 1,
            "dry_run": True,
            "verbose": False,
            "db": tmp_path / "brain.db",
        },
    )()

    with patch("src.extract.attachment_pipeline.ingest_document") as mock_ingest:
        cmd_reverse_ingest(args)

    # No DB calls in dry-run mode
    mock_ingest.assert_not_called()

    out = capsys.readouterr().out
    # Dedup: only the latest of the two q2 prefixed files survives, plus the manual file
    assert "PD2024EN.pdf" in out
    assert "202507221530_q2.pdf" in out
    assert "202504171123_q2.pdf" not in out  # superseded by the July version


def test_reverse_ingest_calls_ingest_document_for_each_survivor(tmp_path):
    """Real run (not dry-run) calls ingest_document once per file with the right source label."""
    root = tmp_path / "Documents" / "National"
    _touch(root / "cards" / "manual.pdf", "202401151430")

    args = type(
        "Args",
        (),
        {
            "root": [root],
            "workers": 1,
            "dry_run": False,
            "verbose": False,
            "db": tmp_path / "brain.db",
        },
    )()

    with (
        patch("src.extract.attachment_pipeline.ingest_document") as mock_ingest,
        patch("src.extract.attachment_pipeline.run_phase1") as mock_phase1,
        patch("src.extract.attachment_pipeline.run_phase2") as mock_phase2,
    ):
        mock_ingest.return_value = {"email_id": 1, "attachment_id": 1, "message_id": -1}
        mock_phase1.return_value = {"extracted": 1, "failed": 0, "skipped": 0}
        mock_phase2.return_value = {"extracted": 1, "failed": 0, "processed": 1}
        cmd_reverse_ingest(args)

    assert mock_ingest.call_count == 1
    args_called = mock_ingest.call_args
    # First positional arg = file path
    assert args_called[0][0].endswith("manual.pdf") or args_called[1].get("file_path", "").endswith(
        "manual.pdf"
    )
    # source kwarg should be the topic label
    src_label = args_called[1].get("source") or (
        args_called[0][2] if len(args_called[0]) > 2 else None
    )
    assert src_label == "National/cards"
    # Both phases must run for the new attachment so the LLM has text to summarize
    mock_phase1.assert_called_once()
    mock_phase2.assert_called_once()


def test_reverse_ingest_skips_missing_root_silently(tmp_path):
    """A configured root that doesn't exist should not crash the run."""
    missing = tmp_path / "ghost"
    args = type(
        "Args",
        (),
        {
            "root": [missing],
            "workers": 1,
            "dry_run": True,
            "verbose": False,
            "db": tmp_path / "brain.db",
        },
    )()
    cmd_reverse_ingest(args)  # should not raise
