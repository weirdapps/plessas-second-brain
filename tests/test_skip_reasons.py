"""A skip always says why.

Phase 1 skipped audio, video and archive formats it has no reader for with no reason at all:
564 producer rows carried a NULL error from the skip list, and a declared archive or media type
that the bytes did not contradict was skipped the same way. A skip with no reason cannot be told
from a bug, so each now names the format and that nothing here reads it.
"""

import pytest

from src.extract.attachment_extractors import extract_text_from_file


def _write(tmp_path, name, data=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


@pytest.mark.parametrize(
    ("name", "mime", "reason"),
    [
        ("clip.mp4", "video/mp4", "video: no text to read"),
        ("note.mp3", "audio/mpeg", "audio: no text to read"),
        ("memo.wav", "audio/x-wav", "audio: no text to read"),
        ("pack.7z", "application/x-7z-compressed", "7-Zip archive: no reader for this format"),
        ("pack.rar", "application/x-rar-compressed", "RAR archive: no reader for this format"),
        ("dump.gz", "application/gzip", "gzip archive: no reader for this format"),
    ],
)
def test_a_format_on_the_skip_list_records_its_reason(tmp_path, name, mime, reason):
    out = extract_text_from_file(_write(tmp_path, name), mime)

    assert (out["status"], out["error"]) == ("skipped", reason)


def test_a_declared_media_type_the_bytes_do_not_contradict_records_its_reason(tmp_path):
    out = extract_text_from_file(_write(tmp_path, "Outlook-clip"), "video/mp4")

    assert (out["status"], out["error"]) == ("skipped", "video: no text to read")


def test_a_declared_zip_whose_bytes_are_no_zip_says_so(tmp_path):
    out = extract_text_from_file(
        _write(tmp_path, "Outlook-pack", b"\x01\x02\x03" * 30), "application/zip"
    )

    assert out["status"] == "skipped"
    assert out["error"] == "declared a zip archive, but the bytes are not one"


def test_the_extension_decides_over_a_declared_type(tmp_path):
    out = extract_text_from_file(_write(tmp_path, "clip.mp4"), "application/octet-stream")

    assert out["error"] == "video: no text to read"


def test_every_skipped_extension_and_declared_type_has_a_reason():
    from src.extract.attachment_extractors import SKIP_EXTENSIONS, SKIP_MIME_TYPES, SKIP_REASONS

    assert set(SKIP_EXTENSIONS) | set(SKIP_MIME_TYPES) <= set(SKIP_REASONS)
