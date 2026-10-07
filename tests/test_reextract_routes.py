"""reextract offers what the new readers can read, and records a re-read's verdict when it has no text.

stale    skipped as an unsupported type before the byte sniff and the text sniff landed, though
         today's code reads them: extensionless and dot-lost names, calendar, mail, SVG
formats  the formats this change made readable: .mso, .wmz and .emz, Office files the library
         readers failed on, images that would not open
ocr      scans and images whose OCR found too little, for the grayscale second pass

A re-read that finds no text used to change nothing, so a row re-read to 'encrypted', or to a
skip with its reason, kept its old label. Now a row that holds no text takes the new verdict, as
long as it is not a failure. A row with text keeps it. A row the images pass owns (method
'vision') is never selected, and never written, even when it became one during the re-read.
"""

import gzip
import struct

import pytest

from src.extract import attachment_pipeline
from src.extract import reextract as rx
from src.store.schema import create_database
from tests.ole_fixtures import RMS_NAMES, mso_file, ole_file, package_object

WORDS = "The plan for the regional network, in enough words to pass the filter. "


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (7, '2026-09-01', 'mail')"
    )
    conn.commit()
    monkeypatch.setattr("src.store.embeddings.remove_vectors", lambda ids: len(ids))
    monkeypatch.setattr(
        attachment_pipeline,
        "_extract_one_attachment",
        lambda row, *a, **k: (row[0], row[5], {"summary": "s", "language": "en"}, None, False),
    )
    yield path, conn, tmp_path
    conn.close()


def _row(conn, root, name, body, *, status, error=None, method=None, text=None, mime=""):
    n = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] + 1
    f = root / "att" / f"dir{n}" / name
    f.parent.mkdir(parents=True)
    f.write_bytes(body if isinstance(body, bytes) else body.encode())
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at) VALUES (1, 7, ?, ?, 1, ?, '2026-09-01')",
        (name, mime, str(f)),
    )
    att = conn.execute("SELECT MAX(id) FROM attachments").fetchone()[0]
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extraction_error, extracted_at, llm_status)"
        " VALUES (?, ?, ?, ?, ?, '2026-09-01', 'pending')",
        (att, text, method, status, error),
    )
    conn.commit()
    return att


def _run(path, root, *which, **kw):
    return rx.reextract(str(path), set(which), root=str(root / "att"), **kw)


def _content(conn, att):
    return conn.execute(
        "SELECT extraction_status, extraction_method, extraction_error, extracted_text"
        " FROM attachment_content WHERE attachment_id = ?",
        (att,),
    ).fetchone()


def _pdf() -> bytes:
    import fitz

    doc = fitz.open()
    doc.new_page().insert_text((72, 72), WORDS)
    return doc.tobytes()


def test_stale_reads_an_unsupported_skip_that_todays_code_reads(store):
    path, conn, root = store
    att = _row(
        conn,
        root,
        "Outlook-a1b2",
        _pdf(),
        status="skipped",
        error="Unsupported type: application/octet-stream ()",
        mime="application/octet-stream",
    )

    stats = _run(path, root, "stale")

    status, method, _error, text = _content(conn, att)
    assert (status, method) == ("extracted", "pymupdf")
    assert "regional network" in text
    assert stats["reread"] == 1


def test_formats_reads_an_mso_the_skip_list_held(store):
    import io

    import openpyxl

    path, conn, root = store
    wb = openpyxl.Workbook()
    wb.active.append(["Region", "Revenue", WORDS])
    buf = io.BytesIO()
    wb.save(buf)
    att = _row(
        conn, root, "oledata.mso", mso_file([package_object(buf.getvalue())]), status="skipped"
    )

    _run(path, root, "formats")

    status, method, _error, text = _content(conn, att)
    assert (status, method) == ("extracted", "mso")
    assert "regional network" in text


def test_a_no_text_re_read_records_its_verdict(store):
    """A failed .pptx that is an RMS-protected file: no text, and now the right label."""
    path, conn, root = store
    att = _row(
        conn,
        root,
        "Deck.pptx",
        ole_file(RMS_NAMES),
        status="failed",
        error="PackageNotFoundError: Package not found at '/elsewhere/Deck.pptx'",
    )

    stats = _run(path, root, "formats")

    status, method, error, text = _content(conn, att)
    assert (status, method, text) == ("encrypted", "rms", None)
    assert error.startswith("Rights-protected (RMS) .pptx")
    assert stats["relabelled"] == 1
    extracted_at = conn.execute(
        "SELECT extracted_at FROM attachment_content WHERE attachment_id = ?", (att,)
    ).fetchone()[0]
    assert extracted_at != "2026-09-01"


def test_a_drawing_with_no_text_is_relabelled_with_its_reason(store):
    path, conn, root = store
    wmf = struct.pack("<IHhhhhHIH", 0x9AC6CDD7, 0, 0, 0, 10, 10, 1440, 0, 0)
    wmf += struct.pack("<HHHIHIH", 1, 9, 0x0300, 12, 0, 3, 0) + struct.pack("<IH", 3, 0)
    att = _row(conn, root, "image001.wmz", gzip.compress(wmf), status="skipped")

    stats = _run(path, root, "formats")

    assert _content(conn, att)[:3] == ("skipped", "wmf", "no text in the drawing")
    assert stats["relabelled"] == 1


def test_a_re_read_to_the_same_verdict_counts_as_unchanged(store):
    path, conn, root = store
    att = _row(
        conn,
        root,
        "Deck.pptx",
        ole_file(RMS_NAMES),
        status="failed",
        error="PackageNotFoundError: x",
    )
    _run(path, root, "formats")

    stats = _run(path, root, "formats")  # an 'encrypted' row is no longer selected

    assert stats["selected"] == 0
    assert _content(conn, att)[0] == "encrypted"


def test_ocr_selects_the_low_text_scans_and_images_only(store):
    path, conn, root = store
    image = _row(
        conn,
        root,
        "a.png",
        b"x",
        status="skipped",
        method="ocr",
        error="OCR returned insufficient text",
    )
    scan = _row(
        conn,
        root,
        "b.pdf",
        b"x",
        status="skipped",
        method="pymupdf",
        error="Insufficient text extracted",
    )
    _row(
        conn,
        root,
        "c.docx",
        b"x",
        status="skipped",
        method="python-docx",
        error="Insufficient text extracted",
    )
    _row(conn, root, "d.png", b"x", status="extracted", method="ocr", text=WORDS)

    rows = rx.select_rows(conn, {"ocr"})

    assert sorted(r[1] for r in rows) == [image, scan]


def test_a_vision_row_is_never_selected(store):
    """The images pass writes long transcriptions as 'vision' rows; 'long' would take them."""
    path, conn, root = store
    att = _row(
        conn, root, "chart.png", b"x", status="extracted", method="vision", text="v" * 60_000
    )
    conn.execute(
        "UPDATE attachment_content SET llm_status = 'extracted' WHERE attachment_id = ?", (att,)
    )
    conn.commit()

    assert rx.select_rows(conn, {"long", "partial", "ocr", "formats", "stale"}) == []


def test_a_row_that_became_vision_during_the_re_read_is_not_written(store, monkeypatch):
    path, conn, root = store
    att = _row(
        conn,
        root,
        "scan.png",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
        status="skipped",
        method="ocr",
        error="OCR returned insufficient text",
    )

    def images_pass_wins(*_a, **_k):
        conn.execute(
            "UPDATE attachment_content SET extraction_method = 'vision',"
            " extraction_status = 'extracted', extraction_error = NULL,"
            " extracted_text = 'described' WHERE attachment_id = ?",
            (att,),
        )
        conn.commit()
        return {"text": WORDS, "method": "ocr", "status": "extracted", "error": None}

    monkeypatch.setattr(rx, "extract_text_from_file", images_pass_wins)

    _run(path, root, "ocr")

    assert _content(conn, att)[1:] == ("vision", None, "described")


def test_the_command_takes_the_new_selectors(store, capsys):
    from argparse import Namespace

    from src.cli import cmd_reextract

    path, conn, root = store
    _row(
        conn,
        root,
        "invite.ics",
        "BEGIN:VCALENDAR\nSUMMARY:" + WORDS,
        status="skipped",
        error="Unsupported type: text/calendar (.ics)",
        mime="text/calendar",
    )

    rc = cmd_reextract(
        Namespace(
            db=str(path),
            capped=False,
            long=False,
            zip=False,
            unread=False,
            partial=False,
            stale=True,
            formats=False,
            ocr=False,
            limit=0,
            after_id=0,
            dry_run=False,
            workers=1,
            root=str(root / "att"),
        )
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "reextract (stale):" in out
    assert "  relabelled  : 0" in out


def test_the_command_names_every_selector_when_none_is_chosen(tmp_path, capsys):
    from argparse import Namespace

    from src.cli import cmd_reextract

    rc = cmd_reextract(Namespace(db=tmp_path / "b.db", limit=0, dry_run=False, workers=1, root="."))

    err = capsys.readouterr().err
    assert rc == 2
    assert all(f"--{name}" in err for name in ("stale", "formats", "ocr"))
