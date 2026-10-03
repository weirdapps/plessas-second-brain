"""reextract redoes rows earlier code capped, skipped or could not read, while the files exist."""

import io
import zipfile

import pytest

from src.extract import attachment_pipeline
from src.extract import reextract as rx
from src.store.schema import create_database

WORDS = "The plan for the regional network, in enough words to pass the filter. "


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (7, '2026-09-01', 'mail')"
    )
    conn.commit()
    removed: list = []
    monkeypatch.setattr(
        "src.store.embeddings.remove_vectors", lambda ids: removed.append(sorted(ids)) or len(ids)
    )
    summarised: list = []
    monkeypatch.setattr(
        attachment_pipeline,
        "_extract_one_attachment",
        lambda row, *a, **k: (
            summarised.append(row[1])
            or (row[0], row[5], {"summary": "new summary", "language": "en"}, None, False)
        ),
    )
    yield path, conn, tmp_path, removed, summarised
    conn.close()


def _row(
    conn,
    root,
    name,
    text,
    status="extracted",
    error=None,
    llm="extracted",
    body=None,
    mime="text/plain",
):
    n = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] + 1
    f = root / "att" / f"dir{n}" / name
    if body is not None:
        f.parent.mkdir(parents=True)
        if isinstance(body, bytes):
            f.write_bytes(body)
        else:
            f.write_text(body)
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at) VALUES (1, 7, ?, ?, 1, ?, '2026-09-01')",
        (name, mime, str(f)),
    )
    att_id = conn.execute("SELECT MAX(id) FROM attachments").fetchone()[0]
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extraction_error, extracted_at, summary, llm_status)"
        " VALUES (?, ?, 'direct_read', ?, ?, '2026-09-01', 'old summary', ?)",
        (att_id, text, status, error, llm),
    )
    conn.commit()
    return att_id


def _run(path, root, *which, **kw):
    return rx.reextract(str(path), set(which), root=str(root / "att"), **kw)


def _content(conn, att_id, cols):
    return conn.execute(
        f"SELECT {cols} FROM attachment_content WHERE attachment_id = ?", (att_id,)
    ).fetchone()


def test_a_capped_row_is_read_again_in_full_and_summarised(store):
    path, conn, root, _removed, summarised = store
    full = WORDS * 3000
    att = _row(conn, root, "long.txt", full[:100_000], body=full)

    stats = _run(path, root, "capped")

    text, llm, summary = _content(conn, att, "extracted_text, llm_status, summary")
    assert len(text) == len(full)
    assert (llm, summary) == ("extracted", "new summary")
    assert stats["reread"] == 1
    assert summarised == [att]


def test_a_row_whose_file_is_gone_is_left_alone(store):
    path, conn, root, removed, summarised = store
    att = _row(conn, root, "gone.txt", "x" * 100_000)

    stats = _run(path, root, "capped")

    assert stats["missing"] == 1
    assert _content(conn, att, "summary")[0] == "old summary"
    assert (summarised, removed) == ([], [])


def test_a_row_phase1_never_read_is_read_now(store):
    path, conn, root, _removed, _summarised = store
    att = _row(
        conn,
        root,
        "note.txt",
        None,
        status="failed",
        error="File not found: /elsewhere/note.txt",
        llm="pending",
        body=WORDS * 3,
    )

    _run(path, root, "unread")

    text, status = _content(conn, att, "extracted_text, extraction_status")
    assert status == "extracted"
    assert "regional network" in text


def test_a_skipped_zip_is_unpacked_now(store):
    path, conn, root, _removed, _summarised = store
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("inside.txt", WORDS * 3)
    att = _row(
        conn,
        root,
        "pack.zip",
        None,
        status="skipped",
        llm="pending",
        body=buf.getvalue(),
        mime="application/zip",
    )

    _run(path, root, "zip")

    assert "=== inside.txt ===" in _content(conn, att, "extracted_text")[0]


def test_a_long_row_is_summarised_again_without_reading_the_file(store, monkeypatch):
    path, conn, root, _removed, summarised = store
    att = _row(conn, root, "mid.txt", "y" * 60_000)
    monkeypatch.setattr(rx, "extract_text_from_file", lambda *a: pytest.fail("the file was read"))

    stats = _run(path, root, "long")

    assert stats["resummarise"] == 1
    assert summarised == [att]


def test_the_vectors_of_redone_rows_are_dropped(store):
    path, conn, root, removed, _summarised = store
    full = WORDS * 3000
    att = _row(conn, root, "long.txt", full[:100_000], body=full)
    ac_id = conn.execute(
        "SELECT id FROM attachment_content WHERE attachment_id = ?", (att,)
    ).fetchone()[0]

    _run(path, root, "capped")

    assert removed == [[-ac_id]]


def test_a_dry_run_changes_nothing(store):
    path, conn, root, removed, summarised = store
    full = WORDS * 3000
    att = _row(conn, root, "long.txt", full[:100_000], body=full)

    stats = _run(path, root, "capped", dry_run=True)

    assert stats["reread"] == 1
    assert len(_content(conn, att, "extracted_text")[0]) == 100_000
    assert (summarised, removed) == ([], [])


def test_the_command_needs_a_selector(tmp_path, capsys):
    from argparse import Namespace

    from src.cli import cmd_reextract

    rc = cmd_reextract(
        Namespace(
            db=tmp_path / "b.db",
            capped=False,
            long=False,
            zip=False,
            unread=False,
            limit=0,
            dry_run=False,
            workers=1,
            root=str(tmp_path),
        )
    )

    assert rc == 2
    assert "--capped" in capsys.readouterr().err


def test_an_archive_cut_short_is_read_again_in_full(store, monkeypatch):
    """The hourly budget cut it; the one-time recovery has the time, so it reads every member."""
    from src.extract import attachment_extractors

    path, conn, root, _removed, _summarised = store
    monkeypatch.setattr(attachment_extractors, "ZIP_MAX_SECONDS", -1)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in ("a.txt", "b.txt", "c.txt"):
            zf.writestr(name, WORDS * 3 + name)
    att = _row(
        conn,
        root,
        "pack.zip",
        "=== a.txt ===\n" + WORDS,
        error="time budget spent, 2 members left unread",
        body=buf.getvalue(),
        mime="application/zip",
    )

    _run(path, root, "zip")

    text, error = _content(conn, att, "extracted_text, extraction_error")
    assert all(f"=== {n} ===" in text for n in ("a.txt", "b.txt", "c.txt"))
    assert not error


def test_a_failed_re_read_keeps_the_text_it_had(store, monkeypatch):
    """A worse re-read (a timeout, a parser error) must not replace 100,000 good characters."""
    path, conn, root, removed, summarised = store
    full = WORDS * 3000
    att = _row(conn, root, "long.txt", full[:100_000], body=full)
    monkeypatch.setattr(
        rx,
        "extract_text_from_file",
        lambda *a, **k: {"text": None, "method": None, "status": "failed", "error": "boom"},
    )

    stats = _run(path, root, "capped")

    text, status, llm = _content(conn, att, "extracted_text, extraction_status, llm_status")
    assert (len(text), status, llm) == (100_000, "extracted", "extracted")
    assert stats["kept"] == 1
    assert (summarised, removed) == ([], [])


def test_a_row_whose_new_summary_fails_keeps_its_vector(store, monkeypatch):
    path, conn, root, removed, _summarised = store
    _row(conn, root, "mid.txt", "y" * 60_000)
    monkeypatch.setattr(
        attachment_pipeline,
        "_extract_one_attachment",
        lambda row, *a, **k: (row[0], row[5], None, "ValueError: bad json", False),
    )

    _run(path, root, "long")

    assert removed == []


def test_a_capped_row_read_in_full_is_not_selected_again(store):
    """The old cap cut text at exactly 100,000 characters. A row read in full is longer than
    that, and an interrupted or repeated run must not redo it."""
    path, conn, root, _removed, _summarised = store
    full = WORDS * 3000
    _row(conn, root, "long.txt", full[:100_000], body=full)

    _run(path, root, "capped")

    assert rx.select_rows(conn, {"capped"}) == []


def _partial(conn, att_id, method="openpyxl"):
    conn.execute(
        "UPDATE attachment_content SET extraction_method = ? WHERE attachment_id = ?",
        (method, att_id),
    )
    conn.commit()


def _workbook(rows):
    import io as _io

    import openpyxl

    wb = openpyxl.Workbook()
    for r in range(1, rows + 1):
        wb.active.append([f"row {r}", WORDS])
    buf = _io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_a_workbook_the_old_caps_cut_is_read_again_in_full_and_summarised(store):
    path, conn, root, _removed, summarised = store
    att = _row(conn, root, "book.xlsx", "row 1 ... row 50", body=_workbook(60), mime="")
    _partial(conn, att)

    stats = _run(path, root, "partial")

    text, llm = _content(conn, att, "extracted_text, llm_status")
    assert "row 60" in text
    assert llm == "extracted" and summarised == [att]
    assert stats["reread"] == 1
    stamp = conn.execute(
        "SELECT value FROM sync_metadata WHERE key = 'reextract_partial_since'"
    ).fetchone()
    assert stamp is not None


def test_a_re_read_that_changes_nothing_is_not_summarised_again(store, monkeypatch):
    path, conn, root, _removed, summarised = store
    att = _row(conn, root, "book.xlsx", WORDS * 3, body=_workbook(3), mime="")
    _partial(conn, att)
    monkeypatch.setattr(
        rx,
        "extract_text_from_file",
        lambda *a, **k: {
            "text": WORDS * 3,
            "method": "openpyxl",
            "status": "extracted",
            "error": None,
        },
    )

    stats = _run(path, root, "partial")

    (llm,) = _content(conn, att, "llm_status")
    assert stats["unchanged"] == 1 and summarised == []
    assert llm == "extracted"


def test_a_partial_run_that_finished_selects_nothing_again(store):
    path, conn, root, _removed, _summarised = store
    att = _row(conn, root, "book.xlsx", "row 1 ... row 50", body=_workbook(60), mime="")
    _partial(conn, att)
    _run(path, root, "partial")

    stats = _run(path, root, "partial")

    assert stats["selected"] == 0


def test_a_text_at_the_old_ceiling_and_a_scan_are_selected(store):
    path, conn, root, _removed, _summarised = store
    _row(conn, root, "log.txt", "x" * 2_000_000, body="x" * 2_100_000)
    scan = _row(conn, root, "scan.pdf", WORDS, body=b"%PDF-1.4", mime="application/pdf")
    _partial(conn, scan, method="pymupdf+tesseract")
    _row(conn, root, "note.txt", WORDS, body=WORDS)

    stats = _run(path, root, "partial", dry_run=True)

    assert stats["selected"] == 2
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sync_metadata WHERE key = 'reextract_partial_since'"
        ).fetchone()[0]
        == 0
    )


def test_a_re_read_shorter_than_the_stored_text_is_kept(store, monkeypatch):
    """A reader that suddenly reads less (a sheet that understates its size) must not replace
    what was stored, nor mark the row read."""
    path, conn, root, _removed, summarised = store
    att = _row(conn, root, "book.xlsx", WORDS * 10, body=_workbook(3), mime="")
    _partial(conn, att)
    monkeypatch.setattr(
        rx,
        "extract_text_from_file",
        lambda *a, **k: {
            "text": WORDS * 3,
            "method": "openpyxl",
            "status": "extracted",
            "error": None,
        },
    )

    stats = _run(path, root, "partial")

    text, extracted_at = _content(conn, att, "extracted_text, extracted_at")
    assert (stats["kept"], text, extracted_at) == (1, WORDS * 10, "2026-09-01")
    assert summarised == []


def test_a_failed_re_read_keeps_a_row_that_had_no_text(store, monkeypatch):
    path, conn, root, _removed, _summarised = store
    att = _row(conn, root, "big.xlsb", None, status="skipped", body=b"PK", mime="")
    _partial(conn, att, method="pyxlsb")
    monkeypatch.setattr(
        rx,
        "extract_text_from_file",
        lambda *a, **k: {
            "text": None,
            "method": "pyxlsb",
            "status": "failed",
            "error": "MemoryError",
        },
    )

    stats = _run(path, root, "partial")

    status, extracted_at = _content(conn, att, "extraction_status, extracted_at")
    assert (stats["kept"], status, extracted_at) == (1, "skipped", "2026-09-01")


def test_a_row_whose_file_is_kept_for_good_is_not_selected_by_unread(store):
    path, conn, root, _removed, _summarised = store
    _row(
        conn,
        root,
        "log.txt",
        "x" * 2_000_000,
        error="text cut at 2,000,000 characters; the rest is unread, file kept",
        body="x",
    )

    stats = _run(path, root, "unread", dry_run=True)

    assert stats["selected"] == 0


def _ac_id(conn, att_id):
    return conn.execute(
        "SELECT id FROM attachment_content WHERE attachment_id = ?", (att_id,)
    ).fetchone()[0]


def _reads(monkeypatch, text, method):
    monkeypatch.setattr(
        rx,
        "extract_text_from_file",
        lambda *a, **k: {"text": text, "method": method, "status": "extracted", "error": None},
    )


def test_an_ocr_re_read_a_few_characters_short_counts_as_read(store, monkeypatch):
    """OCR on another machine or another tesseract drifts by a character or a word, both ways.
    A drift that small is the same scan read again: the row is read, its text and summary stay."""
    path, conn, root, removed, summarised = store
    stored = WORDS * 40
    att = _row(conn, root, "scan.pdf", stored, body=b"%PDF-1.4", mime="application/pdf")
    _partial(conn, att, method="pymupdf+tesseract")
    _reads(monkeypatch, stored[:-3], "pymupdf+tesseract")

    stats = _run(path, root, "partial")

    text, extracted_at, llm, summary = _content(
        conn, att, "extracted_text, extracted_at, llm_status, summary"
    )
    assert (stats["kept"], stats["ocr_close"]) == (0, 1)
    assert (text, llm, summary) == (stored, "extracted", "old summary")
    assert extracted_at != "2026-09-01"
    assert (summarised, removed) == ([], [])
    assert _run(path, root, "partial", dry_run=True)["selected"] == 0


def test_an_ocr_re_read_a_few_characters_long_keeps_the_stored_text(store, monkeypatch):
    path, conn, root, _removed, summarised = store
    stored = WORDS * 40
    att = _row(conn, root, "scan.tif", stored, body=b"II*\x00", mime="image/tiff")
    _partial(conn, att, method="ocr")
    _reads(monkeypatch, stored + " a.", "ocr")

    stats = _run(path, root, "partial")

    text, extracted_at = _content(conn, att, "extracted_text, extracted_at")
    assert (stats["ocr_close"], text, summarised) == (1, stored, [])
    assert extracted_at != "2026-09-01"


def test_an_ocr_re_read_well_short_of_the_stored_text_is_still_kept(store, monkeypatch):
    path, conn, root, _removed, summarised = store
    stored = WORDS * 40
    att = _row(conn, root, "scan.pdf", stored, body=b"%PDF-1.4", mime="application/pdf")
    _partial(conn, att, method="pymupdf+tesseract")
    _reads(monkeypatch, stored[: len(stored) * 9 // 10], "pymupdf+tesseract")

    stats = _run(path, root, "partial")

    text, extracted_at = _content(conn, att, "extracted_text, extracted_at")
    assert (stats["kept"], stats["ocr_close"]) == (1, 0)
    assert (text, extracted_at, summarised) == (stored, "2026-09-01", [])


def test_an_ocr_re_read_that_reports_pages_unread_is_still_kept(store, monkeypatch):
    """Close in length is not enough when the reader says it stopped early."""
    path, conn, root, _removed, summarised = store
    stored = WORDS * 40
    att = _row(conn, root, "scan.pdf", stored, body=b"%PDF-1.4", mime="application/pdf")
    _partial(conn, att, method="pymupdf+tesseract")
    monkeypatch.setattr(
        rx,
        "extract_text_from_file",
        lambda *a, **k: {
            "text": stored[:-3],
            "method": "pymupdf+tesseract",
            "status": "extracted",
            "error": "time budget spent, 2 pages left unread",
        },
    )

    stats = _run(path, root, "partial")

    (extracted_at,) = _content(conn, att, "extracted_at")
    assert (stats["kept"], stats["ocr_close"], extracted_at) == (1, 0, "2026-09-01")
    assert summarised == []


def test_an_ocr_re_read_far_longer_is_stored_and_summarised(store, monkeypatch):
    """A scan the old reader stopped at 30 pages comes back with the pages it never read."""
    path, conn, root, _removed, summarised = store
    stored = WORDS * 40
    att = _row(conn, root, "scan.pdf", stored, body=b"%PDF-1.4", mime="application/pdf")
    _partial(conn, att, method="pymupdf+tesseract")
    _reads(monkeypatch, stored * 2, "pymupdf+tesseract")

    stats = _run(path, root, "partial")

    (text,) = _content(conn, att, "extracted_text")
    assert (stats["ocr_close"], len(text), summarised) == (0, len(stored) * 2, [att])


def test_a_spreadsheet_re_read_a_few_characters_short_is_still_kept(store, monkeypatch):
    """Spreadsheet readers do not drift: a shorter read is a reader reading less."""
    path, conn, root, _removed, summarised = store
    stored = WORDS * 40
    att = _row(conn, root, "book.xlsx", stored, body=_workbook(3), mime="")
    _partial(conn, att)
    _reads(monkeypatch, stored[:-3], "openpyxl")

    stats = _run(path, root, "partial")

    (extracted_at,) = _content(conn, att, "extracted_at")
    assert (stats["kept"], stats["ocr_close"], extracted_at) == (1, 0, "2026-09-01")


def test_after_id_offers_only_the_rows_above_it(store, monkeypatch):
    """A batch runner passes the highest id it handled, so a row kept unread is not offered to
    every later batch again."""
    path, conn, root, _removed, _summarised = store
    first = _row(conn, root, "a.xlsx", WORDS * 3, body=_workbook(3), mime="")
    second = _row(conn, root, "b.xlsx", WORDS * 3, body=_workbook(3), mime="")
    for att in (first, second):
        _partial(conn, att)
    _reads(monkeypatch, None, "pyxlsb")

    stats = _run(path, root, "partial", after_id=_ac_id(conn, first))

    assert (stats["selected"], stats["kept"]) == (1, 1)
    assert stats["highest_id"] == _ac_id(conn, second)


def test_the_highest_id_is_zero_when_nothing_is_left(store):
    path, conn, root, _removed, _summarised = store
    att = _row(conn, root, "a.xlsx", WORDS * 3, body=_workbook(3), mime="")
    _partial(conn, att)

    stats = _run(path, root, "partial", dry_run=True, after_id=_ac_id(conn, att))

    assert (stats["selected"], stats["highest_id"]) == (0, 0)


def test_the_command_passes_after_id_and_prints_the_highest_id(store, capsys):
    from argparse import Namespace

    from src.cli import cmd_reextract

    path, conn, root, _removed, _summarised = store
    first = _row(conn, root, "a.xlsx", WORDS * 3, body=_workbook(3), mime="")
    second = _row(conn, root, "b.xlsx", WORDS * 3, body=_workbook(3), mime="")
    for att in (first, second):
        _partial(conn, att)

    rc = cmd_reextract(
        Namespace(
            db=str(path),
            capped=False,
            long=False,
            zip=False,
            unread=False,
            partial=True,
            limit=0,
            after_id=_ac_id(conn, first),
            dry_run=True,
            workers=1,
            root=str(root / "att"),
        )
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "  selected    : 1" in out
    assert f"  highest id  : {_ac_id(conn, second)}\n" in out
