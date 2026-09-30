"""A zip attachment is unpacked into a temporary directory and every member is extracted.

Nothing outlives the call. Guards: at most ZIP_MAX_MEMBERS members and ZIP_MAX_TOTAL_BYTES
uncompressed, no member compressed more than ZIP_MAX_RATIO to 1, archives nested at most
ZIP_MAX_DEPTH deep, and an encrypted member is skipped and named.
"""

import tempfile
import zipfile
from pathlib import Path

from src.extract import attachment_extractors as ex
from src.extract.attachment_extractors import extract_text_from_file

WORDS = "Quarterly figures for the regional network, with enough words to pass the filter."


def _zip(path: Path, members: dict, compression=zipfile.ZIP_DEFLATED) -> Path:
    with zipfile.ZipFile(path, "w", compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _flag_encrypted(path: Path, name: str) -> None:
    """Set the encrypted bit on one member in the central directory, as a real one carries it."""
    data = bytearray(path.read_bytes())
    at = data.find(b"PK\x01\x02")
    while at != -1:
        length = int.from_bytes(data[at + 28 : at + 30], "little")
        if bytes(data[at + 46 : at + 46 + length]) == name.encode():
            flags = int.from_bytes(data[at + 8 : at + 10], "little") | 0x1
            data[at + 8 : at + 10] = flags.to_bytes(2, "little")
        at = data.find(b"PK\x01\x02", at + 4)
    path.write_bytes(bytes(data))


def test_members_are_extracted_under_their_names(tmp_path):
    z = _zip(tmp_path / "pack.zip", {"a.txt": WORDS, "notes/b.md": WORDS + " Second."})

    out = extract_text_from_file(str(z), "application/zip")

    assert (out["status"], out["method"]) == ("extracted", "zip")
    assert "=== a.txt ===" in out["text"]
    assert "=== notes/b.md ===" in out["text"]
    assert "Second." in out["text"]


def test_a_zip_is_found_by_its_extension_whatever_its_label(tmp_path):
    z = _zip(tmp_path / "pack.zip", {"a.txt": WORDS})
    assert extract_text_from_file(str(z), "application/octet-stream")["status"] == "extracted"


def test_an_encrypted_member_is_named_and_skipped(tmp_path):
    z = _zip(tmp_path / "locked.zip", {"open.txt": WORDS, "secret.txt": WORDS + " Hidden."})
    _flag_encrypted(z, "secret.txt")

    out = extract_text_from_file(str(z), "application/zip")

    assert "=== open.txt ===" in out["text"]
    assert "Hidden." not in out["text"]
    assert "secret.txt: encrypted" in out["error"]


def test_too_many_members_skip_the_archive_and_say_why(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "ZIP_MAX_MEMBERS", 3)
    z = _zip(tmp_path / "many.zip", {f"{i}.txt": WORDS for i in range(4)})

    out = extract_text_from_file(str(z), "application/zip")

    assert out["status"] == "skipped"
    assert "4 members" in out["error"]


def test_too_many_bytes_skip_the_archive_and_say_why(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "ZIP_MAX_TOTAL_BYTES", 100)
    z = _zip(tmp_path / "big.zip", {"a.txt": WORDS * 5})

    out = extract_text_from_file(str(z), "application/zip")

    assert out["status"] == "skipped"
    assert "bytes uncompressed" in out["error"]


def test_a_member_compressed_past_the_ratio_is_skipped(tmp_path):
    z = _zip(tmp_path / "bomb.zip", {"a.txt": WORDS, "zeros.txt": b"0" * 2_000_000})

    out = extract_text_from_file(str(z), "application/zip")

    assert "=== a.txt ===" in out["text"]
    assert "zeros.txt: compression ratio" in out["error"]


def test_a_nested_archive_is_read_one_level_deep(tmp_path):
    inner = _zip(tmp_path / "inner.zip", {"deep.txt": WORDS + " Inner."})
    outer = _zip(tmp_path / "outer.zip", {"inner.zip": inner.read_bytes()}, zipfile.ZIP_STORED)

    assert "Inner." in extract_text_from_file(str(outer), "application/zip")["text"]


def test_an_archive_two_levels_down_is_named_and_skipped(tmp_path):
    c = _zip(tmp_path / "c.zip", {"deepest.txt": WORDS + " Deepest."})
    b = _zip(tmp_path / "b.zip", {"c.zip": c.read_bytes()}, zipfile.ZIP_STORED)
    a = _zip(tmp_path / "a.zip", {"b.zip": b.read_bytes(), "top.txt": WORDS}, zipfile.ZIP_STORED)

    out = extract_text_from_file(str(a), "application/zip")

    assert "Deepest." not in out["text"]
    assert "nested archive" in out["error"]


def test_a_member_path_cannot_escape_the_temporary_directory(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    z = _zip(work / "evil.zip", {"../../escaped.txt": WORDS})

    out = extract_text_from_file(str(z), "application/zip")

    assert out["status"] == "extracted"
    assert not list(tmp_path.rglob("escaped.txt"))


def test_nothing_is_left_on_disk(tmp_path, monkeypatch):
    made = []
    real = tempfile.TemporaryDirectory

    class Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self.name)

    monkeypatch.setattr(tempfile, "TemporaryDirectory", Recording)
    extract_text_from_file(str(_zip(tmp_path / "p.zip", {"a.txt": WORDS})), "application/zip")

    assert made
    assert not any(Path(d).exists() for d in made)


def test_an_archive_stops_reading_members_once_its_time_is_spent(tmp_path, monkeypatch):
    """A member can cost minutes (OCR), and an archive with no content row is offered again
    every run: without a budget one archive could outlast the hourly unit, every hour."""
    monkeypatch.setattr(ex, "ZIP_MAX_SECONDS", -1)
    z = _zip(tmp_path / "slow.zip", {"a.txt": WORDS, "b.txt": WORDS + " Second.", "c.txt": WORDS})

    out = extract_text_from_file(str(z), "application/zip")

    assert out["status"] == "extracted"
    assert "=== a.txt ===" in out["text"]
    assert "Second." not in out["text"]
    assert "time budget" in out["error"]
    assert "2 members" in out["error"]
