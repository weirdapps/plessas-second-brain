"""A text marked as Greek read as Latin look-alike letters is not summarised, keeps its file, and
is read again: by the vision transcription for an image, by `reextract --garbled` for a scan.

The audit found about 800 such texts whose summaries named the wrong programme and the wrong
figure, and 785 of their files already swept, because the sweep had no text-quality rule
(data-quality-03). The mark is the ocr_garbled note in attachment_content.extraction_error
(src/extract/ocr_garble.py); no schema change.
"""

import random
import sys

import pytest
from PIL import Image

from src.extract import attachment_pipeline
from src.extract import reextract as rx
from src.extract.image_classifier import sha256_of_file
from src.extract.image_pipeline import project_vision_text, run_transcription
from src.extract.ocr_garble import OCR_GARBLED_NOTE, is_garbled, mark_garbled
from src.store.file_sweep import (
    DELETABLE,
    NOT_HELD,
    SweepPolicy,
    _registered,
    sweep_files,
)
from src.store.schema import create_database
from tests.greek_text import GREEK, garble

DESCRIPTION = "A scanned letter on headed paper"
TRANSCRIPTION = "Αίτηση για το πρόγραμμα, ποσό 3.500 ευρώ"


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (7, '2026-09-01', 'mail')"
    )
    conn.commit()
    summarised: list = []
    monkeypatch.setattr(
        attachment_pipeline,
        "_extract_one_attachment",
        lambda row, *a, **k: (
            summarised.append(row[1])
            or (row[0], row[5], {"summary": "new summary", "language": "el"}, None, False)
        ),
    )
    monkeypatch.setattr("src.store.embeddings.remove_vectors", lambda ids: len(ids))
    yield path, conn, tmp_path, summarised
    conn.close()


def _image_file(tmp_path, name="scan.png"):
    path = tmp_path / "att" / f"d{random.randrange(10**9)}" / name
    path.parent.mkdir(parents=True)
    pixels = random.Random(name).randbytes(120 * 120 * 3)
    Image.frombytes("RGB", (120, 120), pixels).save(path)
    return path


def _attachment(conn, path, mime="image/png"):
    return conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at, sha256) VALUES (1, 7, ?, ?, 1, ?, '2026-09-01', ?)",
        (path.name, mime, str(path), sha256_of_file(path)),
    ).lastrowid


def _content(
    conn,
    att_id,
    text,
    *,
    method="ocr",
    error=OCR_GARBLED_NOTE,
    llm="skipped",
    summary=None,
):
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extraction_error, extracted_at, summary, llm_status)"
        " VALUES (?, ?, ?, 'extracted', ?, '2026-09-01', ?, ?)",
        (att_id, text, method, error, summary, llm),
    )
    conn.commit()


def _row(conn, att_id, cols):
    return tuple(
        conn.execute(
            f"SELECT {cols} FROM attachment_content WHERE attachment_id = ?", (att_id,)
        ).fetchone()
    )


# --- Phase 1 and Phase 2 ---------------------------------------------------------------------


def test_phase_1_stores_a_garbled_reading_marked_and_not_for_summary(store, monkeypatch):
    path, conn, tmp_path, summarised = store
    monkeypatch.setattr("pytesseract.image_to_string", lambda image, lang: garble(GREEK))
    att = _attachment(conn, _image_file(tmp_path))
    conn.commit()

    attachment_pipeline.run_phase1(str(path))
    stats = attachment_pipeline.run_phase2(str(path))

    text, error, llm = _row(conn, att, "extracted_text, extraction_error, llm_status")
    assert (text, llm) == (garble(GREEK), "skipped")
    assert is_garbled(error)
    assert (stats["processed"], summarised) == (0, [])


def test_phase_1_stores_a_clean_reading_for_summary(store, monkeypatch):
    path, conn, tmp_path, summarised = store
    monkeypatch.setattr("pytesseract.image_to_string", lambda image, lang: GREEK)
    att = _attachment(conn, _image_file(tmp_path))
    conn.commit()

    attachment_pipeline.run_phase1(str(path))
    attachment_pipeline.run_phase2(str(path))

    assert _row(conn, att, "extraction_error, llm_status, summary") == (
        None,
        "extracted",
        "new summary",
    )
    assert summarised == [att]


def test_phase_2_never_summarises_a_marked_row_left_pending(store):
    path, conn, tmp_path, summarised = store
    att = _attachment(conn, _image_file(tmp_path))
    _content(conn, att, garble(GREEK), llm="pending")

    stats = attachment_pipeline.run_phase2(str(path))

    assert (stats["processed"], summarised) == (0, [])
    assert _row(conn, att, "llm_status")[0] == "pending"


def test_phase_1_does_not_copy_a_marked_row_to_the_same_bytes(store, monkeypatch):
    path, conn, tmp_path, _summarised = store
    image = _image_file(tmp_path)
    first = _attachment(conn, image)
    _content(conn, first, garble(GREEK), llm="extracted", summary="wrong summary")
    second = _attachment(conn, image)
    conn.commit()
    monkeypatch.setattr("pytesseract.image_to_string", lambda image, lang: GREEK)

    stats = attachment_pipeline.run_phase1(str(path))

    assert stats["reused"] == 0
    assert _row(conn, second, "extracted_text, extraction_error") == (GREEK, None)


# --- the sweep ---------------------------------------------------------------------------------


def _letter(conn, tmp_path):
    """A scanned PDF on disk, on a store where `reextract --partial` has run: otherwise every
    pymupdf+tesseract row counts as read in part (file_sweep.PARTIAL_SQL) and is kept anyway."""
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('reextract_partial_since', '2026-08-01')"
    )
    letter = tmp_path / "att" / "d1" / "letter.pdf"
    letter.parent.mkdir(parents=True)
    letter.write_bytes(b"%PDF-1.4 scan")
    return letter, _attachment(conn, letter, mime="application/pdf")


def test_the_sweep_keeps_the_file_of_a_marked_text(store):
    _path, conn, tmp_path, _summarised = store
    image, att = _letter(conn, tmp_path)
    _content(conn, att, garble(GREEK), method="pymupdf+tesseract")

    stats = sweep_files(conn, tmp_path / "att", SweepPolicy(apply=True))

    assert image.exists()
    assert (stats["not-held"], stats["deleted"]) == (1, 0)


def test_the_same_file_is_deletable_until_its_text_is_marked(store):
    _path, conn, tmp_path, _summarised = store
    _letter_path, att = _letter(conn, tmp_path)
    _content(conn, att, GREEK, method="pymupdf+tesseract", error=None, llm="extracted")

    assert _registered(conn)[("d1", "letter.pdf")][1] == DELETABLE
    conn.execute(
        "UPDATE attachment_content SET extraction_error = ? WHERE attachment_id = ?",
        (mark_garbled(None), att),
    )
    conn.commit()
    assert _registered(conn)[("d1", "letter.pdf")][1] == NOT_HELD


# --- images: the vision transcription --------------------------------------------------------


def _content_image(conn, tmp_path, *, transcribed=False):
    image = _image_file(tmp_path)
    att = _attachment(conn, image)
    _content(conn, att, garble(GREEK))
    conn.execute(
        "INSERT INTO inline_images (sha256, width, height, bytes, classification,"
        " classification_method, classified_at, vision_description, visioned_at,"
        " vision_transcription, transcribed_at)"
        " VALUES (?, 120, 120, 43200, 'content', 'vision_llm', '2026-09-01', ?,"
        " '2026-09-01T10:00:00+00:00', ?, ?)",
        (
            sha256_of_file(image),
            DESCRIPTION,
            TRANSCRIPTION if transcribed else None,
            "2026-09-02T10:00:00+00:00" if transcribed else None,
        ),
    )
    conn.commit()
    return att


class _Reply:
    stop_reason = "end_turn"

    def __init__(self, text):
        self.content = [type("Block", (), {"text": text})()]


def test_a_marked_image_is_not_given_its_description_alone(store):
    _path, conn, tmp_path, _summarised = store
    att = _content_image(conn, tmp_path)

    assert project_vision_text(conn) == 0
    assert _row(conn, att, "extracted_text, extraction_method") == (garble(GREEK), "ocr")


def test_a_marked_image_is_transcribed_and_its_text_replaced(store, monkeypatch):
    _path, conn, tmp_path, _summarised = store
    att = _content_image(conn, tmp_path)
    calls = []
    monkeypatch.setattr(
        "src.extract.claude_extract.complete",
        lambda **kwargs: calls.append(kwargs) or _Reply(TRANSCRIPTION),
    )

    stats = run_transcription(conn)

    assert (stats["candidates"], stats["transcribed"], stats["projected"]) == (1, 1, 1)
    assert len(calls) == 1
    text, method, error, summary, llm = _row(
        conn, att, "extracted_text, extraction_method, extraction_error, summary, llm_status"
    )
    assert text == f"{DESCRIPTION}\n\n{TRANSCRIPTION}"
    assert (method, error, summary, llm) == ("vision", None, DESCRIPTION, "extracted")


# An image the transcription owes is read from a file of its bytes. On the producer 146 content
# images owed one with every such file gone and refetch-garbled settled on each of their rows: the
# mailbox no longer held the message (228 rows), or held nothing or several matches (14). Each night
# counted them missing and nothing could ever transcribe them (audit 2026-10-20). Given up, they
# stop owing; the refetch record of each row says why.


def _gone(conn, att, state="gone", attempts=1):
    import json
    from pathlib import Path

    from src.store.file_sweep import REFETCH_KEY

    Path(
        conn.execute("SELECT file_path FROM attachments WHERE id = ?", (att,)).fetchone()[0]
    ).unlink()
    if state:
        conn.execute(
            "INSERT INTO sync_metadata (key, value) VALUES (?, ?)",
            (f"{REFETCH_KEY}{att}", json.dumps({"state": state, "attempts": attempts})),
        )
    conn.commit()


def _owed(conn):
    from src.store.file_sweep import TRANSCRIPTION_OWED_SQL

    return conn.execute(
        f"SELECT count(*) FROM inline_images ii WHERE {TRANSCRIPTION_OWED_SQL}"
    ).fetchone()[0]


@pytest.mark.parametrize(("state", "attempts"), [("gone", 1), ("unmatched", 1), ("failed", 3)])
def test_a_marked_image_gone_for_good_stops_owing(store, monkeypatch, state, attempts):
    _path, conn, tmp_path, _summarised = store
    _gone(conn, _content_image(conn, tmp_path), state, attempts)
    monkeypatch.setattr("src.extract.claude_extract.complete", lambda **k: pytest.fail("called"))

    stats = run_transcription(conn)

    assert (stats["missing"], stats["gone"]) == (1, 1)
    assert _owed(conn) == 0
    assert run_transcription(conn)["missing"] == 0


@pytest.mark.parametrize(("state", "attempts"), [(None, 0), ("failed", 1), ("not_found", 2)])
def test_a_marked_image_the_refetch_may_still_find_keeps_owing(store, state, attempts):
    _path, conn, tmp_path, _summarised = store
    _gone(conn, _content_image(conn, tmp_path), state, attempts)

    stats = run_transcription(conn)

    assert (stats["missing"], stats["gone"]) == (1, 0)
    assert _owed(conn) == 1


def test_an_image_one_of_whose_rows_was_never_asked_for_keeps_owing(store):
    """Another email's copy of the bytes, with no refetch record, may still bring the file back."""
    _path, conn, tmp_path, _summarised = store
    att = _content_image(conn, tmp_path)
    path, sha = conn.execute(
        "SELECT file_path, sha256 FROM attachments WHERE id = ?", (att,)
    ).fetchone()
    copy = conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at, sha256) VALUES (1, 7, 'copy.png', 'image/png', 1, ?,"
        " '2026-09-01', ?)",
        (path + ".copy", sha),
    ).lastrowid
    _content(conn, copy, garble(GREEK))
    _gone(conn, att)

    assert run_transcription(conn)["gone"] == 0
    assert _owed(conn) == 1


def test_a_dry_run_gives_nothing_up(store):
    _path, conn, tmp_path, _summarised = store
    _gone(conn, _content_image(conn, tmp_path))

    assert run_transcription(conn, dry_run=True)["gone"] == 1
    assert _owed(conn) == 1


def test_a_marked_image_already_transcribed_takes_its_transcription(store):
    _path, conn, tmp_path, _summarised = store
    att = _content_image(conn, tmp_path, transcribed=True)

    assert project_vision_text(conn) == 1
    assert _row(conn, att, "extracted_text")[0] == f"{DESCRIPTION}\n\n{TRANSCRIPTION}"


# --- scans: reextract --garbled ----------------------------------------------------------------


def _scan(conn, tmp_path, text=None, **content):
    scan = tmp_path / "att" / f"d{random.randrange(10**9)}" / "scan.pdf"
    scan.parent.mkdir(parents=True)
    scan.write_bytes(b"%PDF-1.4 scan")
    att = _attachment(conn, scan, mime="application/pdf")
    _content(conn, att, garble(GREEK) if text is None else text, **content)
    return att


def _reads(monkeypatch, text, error=None):
    monkeypatch.setattr(
        rx,
        "extract_text_from_file",
        lambda *a, **k: {
            "text": text,
            "method": "pymupdf+tesseract",
            "status": "extracted",
            "error": error,
        },
    )


def _run(path, tmp_path, **kw):
    return rx.reextract(str(path), {"garbled"}, root=str(tmp_path / "att"), **kw)


def test_a_clean_re_read_replaces_a_garbled_text_of_the_same_length(store, monkeypatch):
    """OCR drift would call a re-read of the same length the same text read again; a garbled
    text's words were wrong, not its length."""
    path, conn, tmp_path, summarised = store
    att = _scan(conn, tmp_path, method="pymupdf+tesseract")
    _reads(monkeypatch, GREEK)
    assert len(GREEK) == len(garble(GREEK))

    stats = _run(path, tmp_path)

    text, error, llm, summary = _row(
        conn, att, "extracted_text, extraction_error, llm_status, summary"
    )
    assert (text, error, llm, summary) == (GREEK, None, "extracted", "new summary")
    assert (stats["reread"], stats["summarised"], summarised) == (1, 1, [att])


def test_a_clean_re_read_shorter_than_the_garbled_text_still_replaces_it(store, monkeypatch):
    path, conn, tmp_path, _summarised = store
    att = _scan(conn, tmp_path, text=garble(GREEK) * 2, method="pymupdf+tesseract")
    _reads(monkeypatch, GREEK)

    _run(path, tmp_path)

    assert _row(conn, att, "extracted_text, extraction_error") == (GREEK, None)


def test_a_re_read_still_garbled_stays_marked_and_unsummarised(store, monkeypatch):
    path, conn, tmp_path, summarised = store
    att = _scan(conn, tmp_path, method="pymupdf+tesseract")
    longer = garble(GREEK) + " " + garble(GREEK)
    _reads(monkeypatch, longer, error=OCR_GARBLED_NOTE)

    _run(path, tmp_path)

    text, error, llm = _row(conn, att, "extracted_text, extraction_error, llm_status")
    assert (text, llm) == (longer, "skipped")
    assert is_garbled(error)
    assert summarised == []


def test_garbled_selects_marked_rows_with_a_file_only(store):
    path, conn, tmp_path, _summarised = store
    marked = _scan(conn, tmp_path)
    _scan(conn, tmp_path, text=GREEK, error=None, llm="extracted")
    _scan(conn, tmp_path, method="vision")
    text_only = _scan(conn, tmp_path)
    conn.execute(
        "UPDATE attachments SET file_path = 'text:sharepoint:key' WHERE id = ?", (text_only,)
    )
    conn.commit()

    rows = rx.select_rows(conn, {"garbled"})

    assert [row[1] for row in rows] == [marked]


def test_the_command_takes_garbled(store, monkeypatch, capsys):
    from src import cli

    path, conn, tmp_path, _summarised = store
    _scan(conn, tmp_path)
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setattr(
        sys, "argv", ["brain", "--db", str(path), "reextract", "--garbled", "--dry-run"]
    )

    cli.main()

    out = capsys.readouterr().out
    assert "reextract (garbled) DRY RUN" in out
    assert "selected    : 1" in out
