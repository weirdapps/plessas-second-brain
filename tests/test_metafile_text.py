""".wmz and .emz drawings give up the text they draw.

They are gzip-wrapped Windows metafiles (WMF) and enhanced metafiles (EMF): the diagrams and
charts Office puts in an HTML mail. On the producer 235 .wmz (all placeable WMF) and 18 .emz
(EMF) were skipped unread, though the text records of 189 hold 50 characters or more. WMF text is
8-bit in the code page of the selected font (mostly Greek there), EMF text is UTF-16. The parser
stops at a record of size 0, one that runs past the end, and at a record count bound.
"""

import gzip
import struct

from src.extract import attachment_extractors as ax
from src.extract.attachment_extractors import extract_text_from_file

GREEK = "Πωλήσεις ανά περιοχή"
ENGLISH = "Digital sales by region and channel"
# Longer than the 50-character noise floor on its own.
LEGEND = "Digital sales by region and channel, every branch included"


# --- WMF ---


def _wmf_record(function: int, params: bytes) -> bytes:
    if len(params) % 2:
        params += b"\x00"
    return struct.pack("<IH", (6 + len(params)) // 2, function) + params


def _font(charset: int) -> bytes:
    params = struct.pack("<hhhhhBBBBBBBB", -12, 0, 0, 0, 400, 0, 0, 0, charset, 0, 0, 0, 0)
    return _wmf_record(0x02FB, params + b"Arial".ljust(32, b"\x00"))


def _select(index: int) -> bytes:
    return _wmf_record(0x012D, struct.pack("<H", index))


def _delete(index: int) -> bytes:
    return _wmf_record(0x01F0, struct.pack("<H", index))


def _ext_text_out(y: int, x: int, raw: bytes, clipped: bool = False) -> bytes:
    rect = struct.pack("<hhhh", 0, 0, 100, 100) if clipped else b""
    return _wmf_record(
        0x0A32, struct.pack("<hhHH", y, x, len(raw), 4 if clipped else 0) + rect + raw
    )


def _text_out(y: int, x: int, raw: bytes) -> bytes:
    padded = raw + (b"\x00" if len(raw) % 2 else b"")
    return _wmf_record(0x0521, struct.pack("<H", len(raw)) + padded + struct.pack("<hh", y, x))


WMF_EOF = struct.pack("<IH", 3, 0x0000)


def _wmf(*records: bytes) -> bytes:
    body = b"".join(records) + WMF_EOF
    placeable = struct.pack("<IHhhhhHIH", 0x9AC6CDD7, 0, 0, 0, 1000, 1000, 1440, 0, 0)
    header = struct.pack("<HHHIHIH", 1, 9, 0x0300, (18 + len(body)) // 2, 4, 64, 0)
    return placeable + header + body


# --- EMF ---


def _emf_record(kind: int, payload: bytes) -> bytes:
    payload += b"\x00" * (-len(payload) % 4)
    return struct.pack("<II", kind, 8 + len(payload)) + payload


def _emf_text(x: int, y: int, text: str) -> bytes:
    encoded = text.encode("utf-16-le")
    head = struct.pack("<iiii", 0, 0, 0, 0) + struct.pack("<Iff", 1, 1.0, 1.0)
    emr_text = struct.pack("<iiIIIiiiiI", x, y, len(text), 76, 0, 0, 0, 0, 0, 0)
    return _emf_record(84, head + emr_text + encoded)


def _emf(*records: bytes) -> bytes:
    header_payload = (
        struct.pack("<iiii", 0, 0, 100, 100)
        + struct.pack("<iiii", 0, 0, 2000, 2000)
        + b" EMF"
        + struct.pack("<IIIHHIIIiiii", 0x10000, 0, 0, 1, 0, 0, 0, 0, 1920, 1080, 508, 286)
    )
    eof = _emf_record(14, struct.pack("<III", 0, 16, 20))
    return _emf_record(1, header_payload) + b"".join(records) + eof


def _write(tmp_path, name, data, wrap=True):
    path = tmp_path / name
    path.write_bytes(gzip.compress(data) if wrap else data)
    return str(path)


def test_a_wmz_gives_its_text_decoded_in_each_fonts_code_page(tmp_path):
    data = _wmf(
        _font(161),
        _select(0),
        _ext_text_out(100, 10, GREEK.encode("cp1253")),
        _delete(0),
        _font(0),
        _select(0),
        _ext_text_out(200, 10, ENGLISH.encode("cp1252"), clipped=True),
        _text_out(300, 10, b"Source: quarterly report"),
    )

    out = extract_text_from_file(_write(tmp_path, "image001.wmz", data), "application/gzip")

    assert (out["status"], out["method"]) == ("extracted", "wmf")
    assert GREEK in out["text"] and ENGLISH in out["text"]
    assert "Source: quarterly report" in out["text"]


def test_runs_on_one_baseline_join_into_one_line(tmp_path):
    data = _wmf(
        _font(0),
        _select(0),
        _ext_text_out(100, 10, b"Quarterly"),
        _ext_text_out(100, 90, b"branch revenue,"),
        _ext_text_out(100, 200, b"all regions together"),
        _ext_text_out(140, 10, b"Second line of the chart title"),
    )

    out = extract_text_from_file(_write(tmp_path, "chart.wmz", data), "image/x-wmz")

    assert "Quarterly branch revenue, all regions together\nSecond line" in out["text"]


def test_an_emz_gives_its_utf16_text(tmp_path):
    data = _emf(_emf_text(10, 10, GREEK), _emf_text(10, 40, ENGLISH))

    out = extract_text_from_file(_write(tmp_path, "image002.emz", data), "application/gzip")

    assert (out["status"], out["method"]) == ("extracted", "emf")
    assert GREEK in out["text"] and ENGLISH in out["text"]


def test_a_drawing_without_text_is_skipped_with_the_reason(tmp_path):
    out = extract_text_from_file(_write(tmp_path, "shape.wmz", _wmf(_font(0), _select(0))), "")

    assert (out["status"], out["method"]) == ("skipped", "wmf")
    assert out["error"] == "no text in the drawing"


def test_bytes_that_are_not_gzip_are_skipped_with_the_reason(tmp_path):
    out = extract_text_from_file(_write(tmp_path, "odd.wmz", b"not gzip at all", wrap=False), "")

    assert out["status"] == "skipped"
    assert "not a gzip-compressed drawing" in out["error"]


def test_a_gzip_bomb_stops_at_the_inflation_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(ax, "INFLATE_MAX_BYTES", 4096)
    path = _write(tmp_path, "bomb.emz", b"\x00" * (1 << 20))

    out = extract_text_from_file(path, "")

    assert out["status"] == "skipped"
    assert "inflates past 4,096 bytes" in out["error"]


def test_a_record_of_size_zero_ends_the_parse_and_keeps_the_text_before_it(tmp_path):
    good = _ext_text_out(100, 10, LEGEND.encode("cp1252"))
    data = _wmf(_font(0), _select(0), good)[:-6] + struct.pack("<IH", 0, 0x0A32) + b"\x00" * 64

    out = extract_text_from_file(_write(tmp_path, "cut.wmz", data), "")

    assert out["status"] == "extracted"
    assert LEGEND in out["text"]
    assert "malformed" in out["error"]


def test_a_record_that_runs_past_the_end_ends_the_parse(tmp_path):
    good = _emf_text(10, 10, LEGEND)
    data = _emf(good)[:-20] + struct.pack("<II", 84, 1 << 30)

    out = extract_text_from_file(_write(tmp_path, "cut.emz", data), "")

    assert LEGEND in out["text"]
    assert "malformed" in out["error"]


def test_the_parse_stops_at_the_record_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(ax, "METAFILE_MAX_RECORDS", 4)
    lines = [
        _ext_text_out(10 * i, 10, f"Line {i} of a long legend, a chart".encode()) for i in range(10)
    ]
    data = _wmf(_font(0), _select(0), *lines)

    out = extract_text_from_file(_write(tmp_path, "long.wmz", data), "")

    assert "Line 1 of a long legend" in out["text"]
    assert "Line 5 of a long legend" not in out["text"]
    assert "record bound" in out["error"]


def test_a_text_record_whose_string_runs_past_the_record_is_ignored(tmp_path):
    bad = struct.pack("<IH", 7, 0x0A32) + struct.pack("<hhHH", 0, 0, 500, 0)  # claims 500 chars
    data = _wmf(_font(0), _select(0), bad, _ext_text_out(100, 10, LEGEND.encode()))

    out = extract_text_from_file(_write(tmp_path, "odd.wmz", data), "")

    assert out["status"] == "extracted"
    assert LEGEND in out["text"]
