"""The hourly attachment download may rewrite files it already saved.

list-mail's --since filter is inclusive, so the cursor's own message comes back
on every run, and outlook-cli refused its first already-saved file with
IO_WRITE_EEXIST. That was 99.7% of the logged download failures, and it also
meant a download cut off mid-message could never finish on a re-list. outlook-
cli writes each file to a temp name and renames it into place, so overwriting
is safe. Findings export-3 and vps-runtime-3.
"""

from unittest.mock import patch

from src.export.outlook_export import download_attachments_for_messages


@patch("src.export.outlook_export.run_outlook_cli")
def test_download_passes_overwrite(mock_cli, tmp_path):
    mock_cli.return_value = {}
    result = download_attachments_for_messages(
        [{"Id": "AAMk-cursor", "HasAttachments": True}], base_dir=tmp_path
    )

    args = mock_cli.call_args[0][0]
    assert args[:2] == ["download-attachments", "AAMk-cursor"]
    assert "--overwrite" in args
    assert result["failed_messages"] == 0


@patch("src.export.outlook_export.run_outlook_cli")
def test_overwrite_is_passed_without_inline_too(mock_cli, tmp_path):
    mock_cli.return_value = {}
    download_attachments_for_messages(
        [{"Id": "AAMk-2", "HasAttachments": True}], base_dir=tmp_path, include_inline=False
    )
    args = mock_cli.call_args[0][0]
    assert "--overwrite" in args
    assert "--include-inline" not in args
