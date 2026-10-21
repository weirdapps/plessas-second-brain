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


# --- --no-spreadsheets ---------------------------------------------------------


def _method(conn, att_id, method):
    conn.execute(
        "UPDATE attachment_content SET extraction_method = ? WHERE attachment_id = ?",
        (method, att_id),
    )
    conn.commit()


def test_no_spreadsheets_leaves_workbooks_out_of_the_selection(store):
    path, conn, root, _removed, summarised = store
    doc = _row(conn, root, "plan.docx", "y" * 60_000)
    book = _row(conn, root, "plan.xlsx", "z" * 60_000)
    _method(conn, book, "openpyxl")

    stats = _run(path, root, "long", no_spreadsheets=True)

    assert (stats["selected"], summarised) == (1, [doc])
    assert tuple(_content(conn, book, "llm_status, summary")) == ("extracted", "old summary")


def test_no_spreadsheets_leaves_out_a_re_read_that_turns_out_a_workbook(store, monkeypatch):
    """A row with no text yet has no method to judge by: its re-read says what it is."""
    path, conn, root, _removed, summarised = store
    att = _row(
        conn,
        root,
        "book",
        None,
        status="failed",
        error="File not found: /elsewhere/book",
        llm="pending",
        body=b"PK\x03\x04",
    )
    _method(conn, att, None)
    _reads(monkeypatch, "--- Sheet: one ---\na | b", "openpyxl")

    stats = _run(path, root, "unread", no_spreadsheets=True)

    assert stats["spreadsheets"] == 1
    assert tuple(_content(conn, att, "extracted_text, extraction_status")) == (None, "failed")
    assert summarised == []


def test_no_spreadsheets_holds_for_the_estimate(store):
    path, conn, root, _removed, _summarised = store
    _row(conn, root, "plan.docx", "y" * 60_000)
    book = _row(conn, root, "plan.xlsx", "z" * 60_000)
    _method(conn, book, "xlrd (fallback from .xlsb)")

    every = rx.estimate_reextract(str(path), {"long"})
    documents = rx.estimate_reextract(str(path), {"long"}, no_spreadsheets=True)

    assert (every["selected"], every["rows"]) == (2, 2)
    assert (documents["selected"], documents["rows"]) == (1, 1)


# --- duplicates ------------------------------------------------------------------


def _sha(conn, att_id, sha):
    conn.execute("UPDATE attachments SET sha256 = ? WHERE id = ?", (sha, att_id))
    conn.commit()


def test_rows_with_the_same_bytes_are_summarised_once_and_share_the_summary(store):
    path, conn, root, removed, summarised = store
    first = _row(conn, root, "a.txt", "y" * 60_000)
    # Same bytes, so the same document, whatever the stored texts say.
    second = _row(conn, root, "b.txt", "w" * 60_000)
    other = _row(conn, root, "c.txt", "v" * 60_000)
    for att in (first, second):
        _sha(conn, att, "ab" * 32)

    stats = _run(path, root, "long")

    assert sorted(summarised) == [first, other]
    cols = "summary, language, llm_status, llm_error, llm_extracted_at"
    assert _content(conn, second, cols) == _content(conn, first, cols)
    assert _content(conn, second, "summary")[0] == "new summary"
    assert (stats["resummarise"], stats["duplicates"], stats["summarised"]) == (2, 1, 2)
    assert sorted(removed[0]) == sorted(-_ac_id(conn, a) for a in (first, second, other))


def test_rows_with_no_hash_group_by_identical_text(store):
    path, conn, root, _removed, summarised = store
    first = _row(conn, root, "a.txt", "y" * 60_000)
    second = _row(conn, root, "b.txt", "y" * 60_000)
    other = _row(conn, root, "c.txt", "y" * 59_999)

    stats = _run(path, root, "long")

    assert sorted(summarised) == [first, other]
    assert tuple(_content(conn, second, "summary, llm_status")) == ("new summary", "extracted")
    assert stats["duplicates"] == 1


def test_a_duplicate_is_never_set_pending(store, monkeypatch):
    """Only the representative goes to Phase 2: a duplicate the nightly pass found pending
    would be summarised all the same."""
    path, conn, root, _removed, _summarised = store
    first = _row(conn, root, "a.txt", "y" * 60_000)
    second = _row(conn, root, "b.txt", "y" * 60_000)
    seen: list = []
    monkeypatch.setattr(
        rx,
        "run_phase2",
        lambda *a, **k: (
            seen.append((k["attachment_ids"], _content(conn, second, "llm_status")[0]))
            or {"extracted": 0, "failed": 0}
        ),
    )

    _run(path, root, "long")

    assert seen == [([first], "extracted")]


def test_a_failed_representative_leaves_its_duplicates_as_they_were(store, monkeypatch):
    path, conn, root, removed, _summarised = store
    first = _row(conn, root, "a.txt", "y" * 60_000)
    second = _row(conn, root, "b.txt", "y" * 60_000)
    monkeypatch.setattr(
        attachment_pipeline,
        "_extract_one_attachment",
        lambda row, *a, **k: (row[0], row[5], None, "ValueError: bad json", False),
    )

    stats = _run(path, root, "long")

    assert _content(conn, first, "llm_status")[0] == "failed"
    assert tuple(_content(conn, second, "llm_status, summary, llm_error")) == (
        "extracted",
        "old summary",
        None,
    )
    assert (stats["duplicates"], removed) == (0, [])


def test_a_re_read_duplicate_waits_for_its_representative(store):
    """Two copies of one capped file are both read again; one is summarised, and the other
    keeps its old summary until it takes the new one."""
    path, conn, root, _removed, summarised = store
    full = WORDS * 3000
    first = _row(conn, root, "a.txt", full[:100_000], body=full)
    second = _row(conn, root, "b.txt", full[:100_000], body=full)

    stats = _run(path, root, "capped")

    assert (stats["reread"], summarised, stats["duplicates"]) == (2, [first], 1)
    text, summary, llm = _content(conn, second, "extracted_text, summary, llm_status")
    assert (len(text), summary, llm) == (len(full), "new summary", "extracted")


def test_a_re_read_duplicate_keeps_its_old_summary_when_its_representative_fails(
    store, monkeypatch
):
    path, conn, root, _removed, _summarised = store
    full = WORDS * 3000
    _row(conn, root, "a.txt", full[:100_000], body=full)
    second = _row(conn, root, "b.txt", full[:100_000], body=full)
    monkeypatch.setattr(
        attachment_pipeline,
        "_extract_one_attachment",
        lambda row, *a, **k: (row[0], row[5], None, "ValueError: bad json", False),
    )

    _run(path, root, "capped")

    assert tuple(_content(conn, second, "summary, llm_status")) == ("old summary", "extracted")


def test_a_dry_run_and_the_estimate_count_only_representatives(store):
    path, conn, root, removed, summarised = store
    for name in ("a.txt", "b.txt", "c.txt"):
        _row(conn, root, name, "y" * 60_000)
    _row(conn, root, "d.txt", "w" * 60_000)

    stats = _run(path, root, "long", dry_run=True)
    est = rx.estimate_reextract(str(path), {"long"})
    alone = rx.estimate_reextract(str(path), {"long"}, limit=1)

    assert (stats["selected"], stats["resummarise"], stats["duplicates"]) == (4, 2, 2)
    assert (est["selected"], est["rows"], est["duplicates"]) == (4, 2, 2)
    assert est["calls"] == 2 * alone["calls"]
    assert (summarised, removed) == ([], [])
    assert {s for (s,) in conn.execute("SELECT llm_status FROM attachment_content")} == {
        "extracted"
    }


def test_the_command_takes_no_spreadsheets_and_prints_the_duplicates(store, capsys):
    from argparse import Namespace

    from src.cli import cmd_reextract

    path, conn, root, _removed, _summarised = store
    _row(conn, root, "a.txt", "y" * 60_000)
    _row(conn, root, "b.txt", "y" * 60_000)
    book = _row(conn, root, "c.xlsx", "z" * 60_000)
    _method(conn, book, "pyxlsb")
    args = {
        "db": str(path),
        "long": True,
        "limit": 0,
        "after_id": 0,
        "workers": 1,
        "root": str(root / "att"),
        "no_spreadsheets": True,
    }

    assert cmd_reextract(Namespace(**args, dry_run=True)) == 0
    assert cmd_reextract(Namespace(**args, dry_run=False, estimate=True)) == 0

    out = capsys.readouterr().out
    assert "  selected    : 2" in out and "  duplicates  : 1" in out
    assert "  duplicates    : 1" in out  # the estimate's


def test_the_no_spreadsheets_flag_reaches_the_command(monkeypatch):
    import sys

    from src import cli

    got: dict = {}
    monkeypatch.setattr(cli, "cmd_reextract", lambda args: got.update(vars(args)))
    monkeypatch.setattr(sys, "argv", ["brain", "reextract", "--long", "--no-spreadsheets"])

    cli.main()

    assert (got["long"], got["no_spreadsheets"]) == (True, True)


# --- --wide: workbooks summarised from a digest that left out their newest columns -----------
#
# Until 2026-10-16 the digest of a wide sheet kept its first 40 columns and the first 600
# characters of a row, so a report with a column per day was summarised for its oldest days
# (audit 2026-10-16). --wide takes the long workbooks summarised from a digest, since it landed
# on 2026-10-11, whose digest leaves out columns, and summarises them again from the new one.


def _wide_dump(days: int = 120) -> str:
    """A cell dump as Phase 1 stores it: a label column, then a column a day."""
    header = " | ".join(
        ["Channel", *(f"2026-{1 + i // 28:02d}-{1 + i % 28:02d} 00:00:00" for i in range(days))]
    )
    rows = [
        " | ".join([name, *(str(10_000_000 + k * 1000 + i) for i in range(days))])
        for k, name in enumerate(("Cards", "Internet", "Mobile", "Branches"))
    ]
    text = "--- Sheet: Volumes ---\n" + "\n".join([header, *rows])
    return (text + "\n") * (1 + 50_001 // len(text))


def _summarised(conn, att_id, when, method="openpyxl"):
    conn.execute(
        "UPDATE attachment_content SET extraction_method = ?, llm_extracted_at = ?"
        " WHERE attachment_id = ?",
        (method, when, att_id),
    )
    conn.commit()


def test_wide_takes_a_wide_workbook_summarised_from_the_digest(store):
    path, conn, root, _removed, summarised = store
    wide = _row(conn, root, "volumes.xlsx", _wide_dump())
    _summarised(conn, wide, "2026-10-13T02:10:00.000000")
    narrow = _row(conn, root, "ledger.xlsx", "--- Sheet: L ---\n" + "a | b | 1\n" * 6_000)
    _summarised(conn, narrow, "2026-10-13T02:10:00.000000")
    before = _row(conn, root, "old.xlsx", _wide_dump())
    _summarised(conn, before, "2026-10-09T02:10:00.000000")  # from its parts, every column
    document = _row(conn, root, "plan.docx", _wide_dump())
    _summarised(conn, document, "2026-10-13T02:10:00+00:00", method="python-docx")

    stats = _run(path, root, "wide")

    assert (stats["selected"], stats["reread"], summarised) == (1, 0, [wide])
    assert _content(conn, wide, "summary")[0] == "new summary"


def test_wide_does_not_take_a_row_twice(store):
    path, conn, root, _removed, summarised = store
    wide = _row(conn, root, "volumes.xlsx", _wide_dump())
    _summarised(conn, wide, "2026-10-13T02:10:00+00:00")

    _run(path, root, "wide")
    stats = _run(path, root, "wide")

    assert (stats["selected"], summarised) == (0, [wide])
    since = conn.execute(
        "SELECT value FROM sync_metadata WHERE key = 'reextract_wide_since'"
    ).fetchone()[0]
    assert since.endswith("+00:00")


def test_wide_leaves_a_row_flagged_for_full_parts(store):
    path, conn, root, _removed, summarised = store
    wide = _row(conn, root, "volumes.xlsx", _wide_dump())
    _summarised(conn, wide, "2026-10-13T02:10:00+00:00")
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES (?, '2026-10-12')",
        (f"{attachment_pipeline.FULL_PARTS_KEY}{wide}",),
    )
    conn.commit()

    assert _run(path, root, "wide")["selected"] == 0


def test_wide_is_priced_before_it_runs_and_a_dry_run_sets_no_mark(store):
    path, conn, root, _removed, summarised = store
    wide = _row(conn, root, "volumes.xlsx", _wide_dump())
    _summarised(conn, wide, "2026-10-13T02:10:00+00:00")

    est = rx.estimate_reextract(str(path), {"wide"})
    dry = _run(path, root, "wide", dry_run=True)

    assert (est["selected"], est["rows"], est["calls"]) == (1, 1, 1)  # one call over its digest
    assert dry["selected"] == 1 and summarised == []
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sync_metadata WHERE key = 'reextract_wide_since'"
        ).fetchone()[0]
        == 0
    )


def test_the_wide_flag_reaches_the_command(monkeypatch):
    import sys

    from src import cli

    got: dict = {}
    monkeypatch.setattr(cli, "cmd_reextract", lambda args: got.update(vars(args)))
    monkeypatch.setattr(sys, "argv", ["brain", "maint", "reextract", "--wide", "--estimate"])

    cli.main()

    assert (got["wide"], got["estimate"]) == (True, True)


# --- --decoding: texts a reader decoded wrongly ------------------------------------------------
#
# A UTF-16 card statement was read as Latin-1 (its byte-order mark stored as "ÿþ", a NUL after
# every Latin letter), and a Greek agenda failed whole on antiword's output (UnicodeDecodeError).
# Both readers decode by the bytes now (tests/test_text_decoding.py); --decoding reads those rows
# again and summarises them, the right text replacing the longer wrong one.

STATEMENT = "Ημερομηνία\tΠοσό\r\n" + "".join(f"2026-03-{d:02d}\t{d},50\r\n" for d in range(1, 30))


def test_decoding_reads_a_utf16_text_stored_as_latin1_again(store):
    path, conn, root, _removed, summarised = store
    body = STATEMENT.encode("utf-16")
    att = _row(conn, root, "statement.csv", body.decode("latin-1"), body=body, mime="text/csv")
    _row(conn, root, "plain.csv", WORDS * 3, body=WORDS * 3, mime="text/csv")

    stats = _run(path, root, "decoding")

    text, llm = _content(conn, att, "extracted_text, llm_status")
    assert stats["selected"] == 1
    assert text.startswith("Ημερομηνία") and "\x00" not in text
    assert llm == "extracted" and summarised == [att]


def test_decoding_selects_the_row_once_the_nuls_are_stripped(store):
    """`maint strip-nul` may run first: the mark read as Latin-1 still says what the row is."""
    path, conn, root, _removed, _summarised = store
    body = STATEMENT.encode("utf-16")
    att = _row(conn, root, "statement.csv", body.decode("latin-1").replace("\x00", ""), body=body)

    _run(path, root, "decoding")

    assert _content(conn, att, "extracted_text")[0].startswith("Ημερομηνία")


def test_decoding_reads_a_word_file_whose_converter_output_failed(store, monkeypatch):
    path, conn, root, _removed, summarised = store
    att = _row(
        conn,
        root,
        "agenda.doc",
        None,
        status="failed",
        error="UnicodeDecodeError: 'utf-8' codec can't decode byte 0xed in position 9",
        llm="pending",
        body=b"\xd0\xcf\x11\xe0",
        mime="application/msword",
    )
    monkeypatch.setattr(
        rx,
        "extract_text_from_file",
        lambda *a, **k: {
            "text": WORDS * 3,
            "method": "antiword",
            "status": "extracted",
            "error": None,
        },
    )

    stats = _run(path, root, "decoding")

    status, method = _content(conn, att, "extraction_status, extraction_method")
    assert (stats["reread"], status, method) == (1, "extracted", "antiword")
    assert summarised == [att]


def test_decoding_selects_nothing_once_the_rows_are_read_right(store):
    path, conn, root, _removed, _summarised = store
    body = STATEMENT.encode("utf-16")
    _row(conn, root, "statement.csv", body.decode("latin-1"), body=body)
    _run(path, root, "decoding")

    assert _run(path, root, "decoding")["selected"] == 0


# --- --failed: summaries that failed under the call shape before v2-text ----------------------
#
# 14 summaries failed for good before v2-text became the default on 2026-10-16: three images on
# an old parser's IndexError, eight short session notes the model refused, three replies that
# were not JSON. Each failure belongs to the request it was made from, and those requests are
# no longer sent. --failed summarises them again; a failure under today's shape is its verdict
# and is not taken again, so a second run selects nothing.


def _failed(conn, att, when):
    conn.execute(
        "UPDATE attachment_content SET llm_status = 'failed', summary = NULL,"
        " llm_error = 'ValueError: Failed to parse JSON', llm_extracted_at = ?"
        " WHERE attachment_id = ?",
        (when, att),
    )
    conn.commit()


def test_failed_summarises_again_a_summary_that_failed_under_the_older_shape(store, monkeypatch):
    path, conn, root, _removed, summarised = store
    old = _row(conn, root, "note.txt", WORDS * 3)
    _failed(conn, old, "2026-10-15T23:08:46.40774Z")
    recent = _row(conn, root, "other.txt", WORDS * 4)
    _failed(conn, recent, "2026-10-17T09:00:00+00:00")
    monkeypatch.setattr(rx, "extract_text_from_file", lambda *a: pytest.fail("the file was read"))

    stats = _run(path, root, "failed")

    assert (stats["selected"], stats["resummarise"]) == (1, 1)
    assert summarised == [old]
    assert tuple(_content(conn, old, "llm_status, summary")) == ("extracted", "new summary")
    assert _content(conn, recent, "llm_status")[0] == "failed"


def test_failed_takes_a_local_stamp_as_the_instant_it_was(store):
    """Stamps written before `maint utc-stamps` are Athens time with no offset."""
    path, conn, root, _removed, summarised = store
    att = _row(conn, root, "note.txt", WORDS * 3)
    _failed(conn, att, "2026-09-30T19:06:36.45016")

    _run(path, root, "failed")

    assert summarised == [att]


def test_failed_selects_nothing_the_second_time(store, monkeypatch):
    path, conn, root, _removed, _summarised = store
    att = _row(conn, root, "note.txt", WORDS * 3)
    _failed(conn, att, "2026-10-11T21:40:13.65871Z")
    monkeypatch.setattr(
        attachment_pipeline,
        "_extract_one_attachment",
        lambda row, *a, **k: (row[0], row[5], None, "ValueError: refused", False),
    )

    _run(path, root, "failed")

    assert _content(conn, att, "llm_status")[0] == "failed"
    assert _run(path, root, "failed")["selected"] == 0


@pytest.mark.parametrize("flag", ["decoding", "failed"])
def test_the_new_selectors_reach_the_command(flag, monkeypatch):
    import sys

    from src import cli

    got: dict = {}
    monkeypatch.setattr(cli, "cmd_reextract", lambda args: got.update(vars(args)))
    monkeypatch.setattr(sys, "argv", ["brain", "maint", "reextract", f"--{flag}", "--estimate"])

    cli.main()

    assert got[flag] is True
