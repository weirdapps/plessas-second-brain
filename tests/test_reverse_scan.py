"""Tests for src/ingest/reverse_scan.py — pure-function helpers."""

import os
from datetime import datetime
from pathlib import Path

from src.ingest.reverse_scan import (
    _version_key,
    is_curated_filename,
    scan_roots,
    select_files_to_ingest,
    topic_label,
)


def test_is_curated_filename_full_prefix():
    assert is_curated_filename("202504171123_q2_report.pdf") is True


def test_is_curated_filename_no_prefix():
    assert is_curated_filename("PD2023EN.pdf") is False


def test_is_curated_filename_only_digits_no_underscore():
    # 12 digits without trailing underscore are NOT a prefix
    assert is_curated_filename("202504171123.pdf") is False


def test_is_curated_filename_short_digits():
    # Only 8 digits — not the full YYYYMMDDHHMM
    assert is_curated_filename("20250417_q2.pdf") is False


def test_is_curated_filename_too_many_digits():
    # 14 digits — also not the canonical form
    assert is_curated_filename("20250417112345_q2.pdf") is False


def test_is_curated_filename_with_unicode_tail():
    # Greek text in the logical-name part should not break the prefix detection
    assert is_curated_filename("202504171123_νμ_report.pdf") is True


def test_version_key_uses_prefix_when_present(tmp_path):
    # File with prefix — mtime should be irrelevant
    f = tmp_path / "202504171123_q2.pdf"
    f.write_bytes(b"x")
    # Even if mtime is in 2030, the prefix wins
    far_future = datetime(2030, 1, 1, 0, 0, 0).timestamp()
    os.utime(f, (far_future, far_future))
    assert _version_key(f) == "202504171123"


def test_version_key_falls_back_to_mtime_when_no_prefix(tmp_path):
    f = tmp_path / "PD2024EN.pdf"
    f.write_bytes(b"x")
    # Set mtime to 2024-09-15 14:30:00
    target = datetime(2024, 9, 15, 14, 30, 0).timestamp()
    os.utime(f, (target, target))
    assert _version_key(f) == "202409151430"


def _touch_with_mtime(path: Path, ts_str: str) -> Path:
    """Create empty file at path with mtime set from a YYYYMMDDHHMM string."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    dt = datetime.strptime(ts_str, "%Y%m%d%H%M")
    epoch = dt.timestamp()
    os.utime(path, (epoch, epoch))
    return path


def test_select_empty_returns_empty():
    assert select_files_to_ingest([]) == []


def test_select_single_file_returns_it(tmp_path):
    f = _touch_with_mtime(tmp_path / "PD2024EN.pdf", "202409151430")
    assert select_files_to_ingest([f]) == [f]


def test_select_two_prefixed_same_folder_keeps_latest(tmp_path):
    older = _touch_with_mtime(tmp_path / "202504171123_q2.pdf", "202504171123")
    newer = _touch_with_mtime(tmp_path / "202507221530_q2.pdf", "202507221530")
    selected = select_files_to_ingest([older, newer])
    assert selected == [newer]


def test_select_same_logical_name_in_different_folders_keeps_both(tmp_path):
    a = _touch_with_mtime(tmp_path / "cards" / "report.pdf", "202509010900")
    b = _touch_with_mtime(tmp_path / "strategy" / "report.pdf", "202509010900")
    selected = select_files_to_ingest([a, b])
    assert sorted(selected) == sorted([a, b])


def test_select_manual_alone_in_folder_ingested(tmp_path):
    f = _touch_with_mtime(tmp_path / "PD2024EN.pdf", "202409151430")
    assert select_files_to_ingest([f]) == [f]


def test_select_manual_with_older_prefixed_sibling_keeps_manual(tmp_path):
    """Manual mtime in 2026 beats prefixed file dated April 2025."""
    manual = _touch_with_mtime(tmp_path / "report.pdf", "202604151200")
    prefixed = _touch_with_mtime(tmp_path / "202504121500_report.pdf", "202504121500")
    selected = select_files_to_ingest([manual, prefixed])
    assert selected == [manual]


def test_select_manual_with_newer_prefixed_sibling_keeps_prefixed(tmp_path):
    """Prefixed file dated May 2025 beats manual mtime in January 2024."""
    manual = _touch_with_mtime(tmp_path / "report.pdf", "202401150900")
    prefixed = _touch_with_mtime(tmp_path / "202505121500_report.pdf", "202505121500")
    selected = select_files_to_ingest([manual, prefixed])
    assert selected == [prefixed]


def test_select_three_prefixed_keeps_only_latest(tmp_path):
    a = _touch_with_mtime(tmp_path / "202401010000_x.pdf", "202401010000")
    b = _touch_with_mtime(tmp_path / "202406010000_x.pdf", "202406010000")
    c = _touch_with_mtime(tmp_path / "202412010000_x.pdf", "202412010000")
    assert select_files_to_ingest([a, b, c]) == [c]


INGESTABLE_EXTENSIONS = {".pdf", ".pptx", ".xlsx", ".docx", ".md", ".txt"}


def test_scan_roots_picks_up_supported_extensions(tmp_path):
    (tmp_path / "doc.pdf").write_bytes(b"x")
    (tmp_path / "deck.pptx").write_bytes(b"x")
    (tmp_path / "ignored.json").write_bytes(b"x")  # not in INGESTABLE_EXTENSIONS
    result = sorted(p.name for p in scan_roots([tmp_path], INGESTABLE_EXTENSIONS))
    assert result == ["deck.pptx", "doc.pdf"]


def test_scan_roots_recurses_into_subdirs(tmp_path):
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    (tmp_path / "a" / "b" / "c" / "deep.pdf").write_bytes(b"x")
    result = list(scan_roots([tmp_path], INGESTABLE_EXTENSIONS))
    assert len(result) == 1
    assert result[0].name == "deep.pdf"


def test_scan_roots_skips_missing_root(tmp_path, capsys):
    """Missing roots are silently skipped (returns empty for that root)."""
    missing = tmp_path / "does_not_exist"
    real = tmp_path / "real"
    real.mkdir()
    (real / "x.pdf").write_bytes(b"x")
    result = list(scan_roots([missing, real], INGESTABLE_EXTENSIONS))
    assert len(result) == 1
    assert result[0].name == "x.pdf"


def test_scan_roots_extension_filter_is_case_insensitive(tmp_path):
    """User's docs include .JPG (uppercase). Even if .jpg were in the filter,
    we should match case-insensitively. Here we test .PDF matches."""
    (tmp_path / "uppercase.PDF").write_bytes(b"x")
    result = list(scan_roots([tmp_path], INGESTABLE_EXTENSIONS))
    assert len(result) == 1


def test_topic_label_strips_root_prefix(tmp_path):
    root = tmp_path / "Documents" / "National"
    file_path = root / "units" / "cards" / "202504171123_q2.pdf"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"x")
    label = topic_label(
        file_path,
        [tmp_path / "Documents" / "National", tmp_path / "Documents" / "Personal"],
    )
    assert label == "National/units/cards"


def test_topic_label_handles_top_level_file(tmp_path):
    root = tmp_path / "Documents" / "Personal"
    file_path = root / "PD2024EN.pdf"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"x")
    label = topic_label(
        file_path,
        [tmp_path / "Documents" / "National", tmp_path / "Documents" / "Personal"],
    )
    assert label == "Personal"


def test_topic_label_falls_back_to_parent_dir_name_when_outside_roots(tmp_path):
    """If a file is somehow not under any configured root, label = parent dir name."""
    file_path = tmp_path / "stray" / "doc.pdf"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"x")
    label = topic_label(file_path, [tmp_path / "elsewhere"])
    assert label == "stray"
