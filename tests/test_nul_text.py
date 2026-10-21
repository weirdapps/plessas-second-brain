"""No stored text holds a NUL character, and `brain maint strip-nul` takes out the ones stored.

SQLite's length() ends a text at its first NUL and Python's len() does not. The chunk pass
measured a text both ways (src/store/chunks.py), so on the producer three documents were cut
again on every embedding pass and eleven long ones were never cut at all: 54 stored texts held
NULs (audit 2026-10-20). Every text an extractor returns, and every text-only document, is
stored without them now; the maint command repairs what was stored before.
"""

import pytest

from src.extract.attachment_extractors import extract_text_from_file
from src.extract.attachment_pipeline import ingest_text_document
from src.store import nul_text
from src.store.chunks import CHUNK_MIN_CHARS, plan_chunks
from src.store.schema import create_database, get_connection
from tests import factories
from tests.brain_cli import brain

WORDS = "The quarterly figures for every region, written out in plenty of plain words. "


def _paragraphs(n: int) -> str:
    return "\n\n".join(f"Paragraph {i}. " + WORDS * 12 for i in range(n))


@pytest.fixture
def conn(tmp_path):
    c = create_database(str(tmp_path / "brain.db"))
    yield c
    c.close()


def _text(conn, attachment_id):
    return conn.execute(
        "SELECT extracted_text FROM attachment_content WHERE attachment_id = ?", (attachment_id,)
    ).fetchone()[0]


def _fts_hits(conn, word):
    return conn.execute(
        "SELECT count(*) FROM attachment_content_fts WHERE attachment_content_fts MATCH ?",
        (word,),
    ).fetchone()[0]


# --- at the source ----------------------------------------------------------------------------


def test_an_extracted_text_holds_no_nul(tmp_path):
    """A PDF whose fonts map glyphs to U+0000 and a CSV read byte by byte gave texts like
    "\\x004\\x00.\\x005"; the digits are the text."""
    f = tmp_path / "rates.csv"
    f.write_text(WORDS + "rate\x00 4\x00.\x005 per cent " + WORDS, encoding="utf-8")

    result = extract_text_from_file(str(f), "text/csv")

    assert result["status"] == "extracted"
    assert "\x00" not in result["text"]
    assert "rate 4.5 per cent" in result["text"]


def test_a_member_of_an_archive_holds_no_nul(tmp_path):
    import zipfile

    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("notes.txt", WORDS + "\x00\x00" + WORDS)

    result = extract_text_from_file(str(archive), "application/zip")

    assert result["status"] == "extracted"
    assert "\x00" not in result["text"]


def test_a_text_only_document_is_stored_without_nul(conn):
    outcome = ingest_text_document(
        conn,
        source="sharepoint",
        key="k",
        filename="plan.txt",
        mime_type="text/plain",
        text=WORDS + "\x00" + WORDS,
        sha256="a" * 64,
        method="direct_read",
        status="extracted",
        error=None,
        subject="[SharePoint] plan",
        sender_name="SharePoint",
        date="2026-10-01T09:00:00Z",
    )

    assert "\x00" not in _text(conn, outcome["attachment_id"])


# --- the repair -------------------------------------------------------------------------------


def _nul_document(conn, filename="scan.pdf"):
    """A long summarised PDF stored with a NUL early in it, as older extraction left it."""
    text = _paragraphs(4) + "\x00 tailword " + _paragraphs(16)
    return factories.attachment(conn, filename=filename, text=text, summary="A scan.")


def test_strip_nul_counts_and_changes_nothing_in_a_dry_run(conn):
    attachment_id = _nul_document(conn)
    factories.attachment(conn, filename="clean.pdf", text=_paragraphs(3), summary="s")
    before = _text(conn, attachment_id)

    stats = nul_text.strip_nul(conn, apply=False)

    assert (stats["texts"], stats["nuls"], stats["stripped"]) == (1, 1, 0)
    assert _text(conn, attachment_id) == before


def test_strip_nul_takes_them_out_and_keeps_the_index_in_step(conn):
    attachment_id = _nul_document(conn)

    stats = nul_text.strip_nul(conn, apply=True)

    assert (stats["texts"], stats["stripped"]) == (1, 1)
    assert "\x00" not in _text(conn, attachment_id)
    assert _fts_hits(conn, "tailword") == 1
    conn.execute(
        "INSERT INTO attachment_content_fts(attachment_content_fts) VALUES ('integrity-check')"
    )


def test_a_document_once_too_short_by_length_is_cut_after_the_repair(conn):
    """Eleven long documents on the producer had a NUL before their 8,000th character, so
    length() said they were short and they were never cut into chunks."""
    attachment_id = _nul_document(conn)
    assert (
        conn.execute(
            "SELECT length(extracted_text) FROM attachment_content WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchone()[0]
        < CHUNK_MIN_CHARS
    )
    assert plan_chunks(conn).spans == {}

    nul_text.strip_nul(conn, apply=True)

    assert plan_chunks(conn).changed == {attachment_id}
    assert plan_chunks(conn).changed == set()


def test_a_document_already_cut_is_cut_again_once(conn):
    """Its chunk rows were cut from the text with the NUL, whose offsets the stripped text no
    longer matches: they go, so the next pass cuts it again and drops the old chunk vectors."""
    text = _paragraphs(20)
    attachment_id = factories.attachment(
        conn, filename="deck.pdf", text=text[:9_000] + "\x00" + text[9_000:], summary="s"
    )
    plan_chunks(conn)

    stats = nul_text.strip_nul(conn, apply=True)

    assert stats["chunked"] == 1
    assert (
        conn.execute(
            "SELECT count(*) FROM attachment_chunks WHERE attachment_id = ?", (attachment_id,)
        ).fetchone()[0]
        == 0
    )
    assert plan_chunks(conn).changed == {attachment_id}
    assert plan_chunks(conn).changed == set()


def test_a_second_repair_changes_nothing(conn):
    _nul_document(conn)
    nul_text.strip_nul(conn, apply=True)

    stats = nul_text.strip_nul(conn, apply=True)

    assert (stats["texts"], stats["stripped"]) == (0, 0)


def test_the_command_counts_by_default_and_applies_when_asked(tmp_path, monkeypatch, capsys):
    db = tmp_path / "brain.db"
    c = create_database(str(db))
    attachment_id = _nul_document(c)
    c.close()

    code, out, _ = brain(monkeypatch, capsys, "--db", str(db), "maint", "strip-nul")
    assert code == 0 and "1 stored text" in out and "dry run" in out

    code, out, _ = brain(monkeypatch, capsys, "--db", str(db), "maint", "strip-nul", "--apply")
    assert code == 0
    c = get_connection(str(db))
    assert "\x00" not in _text(c, attachment_id)
    c.close()


def test_the_command_refuses_on_a_replica(tmp_path, monkeypatch, capsys):
    db = tmp_path / "brain.db"
    create_database(str(db)).close()
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    code, _out, err = brain(monkeypatch, capsys, "--db", str(db), "maint", "strip-nul")

    assert code == 2 and "replica" in err
