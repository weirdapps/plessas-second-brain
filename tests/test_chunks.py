"""Long PDF, Word and PowerPoint attachments are cut into passages, each with its vector.

One vector per summary put a 100-page PDF into semantic search as about 400 characters;
the passage that answers a question deep inside it could only be found by its words. The
attachments of those three kinds whose text exceeds CHUNK_MIN_CHARS are cut into chunks of
about CHUNK_CHARS characters at a paragraph, page or slide boundary, with a small overlap,
and attachment_chunks (schema v39) records each chunk's place in the text.
"""

import pytest

from src.config import CURRENT_SCHEMA_VERSION
from src.store import chunks
from src.store.chunks import (
    CHUNK_CHARS,
    CHUNK_MIN_CHARS,
    CHUNK_OVERLAP,
    chunk_kind,
    plan_chunks,
    split_text,
)
from src.store.schema import create_database, get_schema_version, migrate_add_attachment_chunks
from src.store.stamps import to_utc
from tests import factories


def _paragraphs(n: int, size: int = 900) -> str:
    """n paragraphs of about `size` characters, a blank line between each."""
    return "\n\n".join(
        f"Paragraph {i}. " + " ".join(f"word{i}x{j}" for j in range(size // 10)) for i in range(n)
    )


# --- which attachments are cut ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "mime", "kind"),
    [
        ("report.pdf", "application/pdf", "pdf"),
        ("REPORT.PDF", None, "pdf"),
        ("minutes.docx", None, "word"),
        ("minutes.doc", "application/msword", "word"),
        ("deck.pptx", None, "powerpoint"),
        ("deck.ppt", None, "powerpoint"),
        ("scan", "application/pdf", "pdf"),
        ("figures.xlsx", None, None),
        ("page.html", "text/html", None),
        ("notes.md", None, None),
        ("mail.eml", "message/rfc822", None),
    ],
)
def test_only_pdf_word_and_powerpoint_are_chunked(filename, mime, kind):
    """Spreadsheets need another treatment (1.94 G characters on the reference replica), and
    mail bodies repeat their thread's quoted history."""
    assert chunk_kind(filename, mime) == kind


# --- where the text is cut -------------------------------------------------------------------


def test_a_text_under_one_chunk_is_one_span():
    assert split_text("short text") == [(0, 10)]
    assert split_text("") == []


def test_spans_cover_the_text_with_a_small_overlap():
    text = _paragraphs(40)

    spans = split_text(text)

    assert spans[0][0] == 0
    assert spans[-1][1] == len(text)
    for (_s1, e1), (s2, e2) in zip(spans, spans[1:], strict=False):
        assert s2 < e1, "the next chunk starts inside the previous one"
        assert e1 - s2 <= CHUNK_OVERLAP + 20
        assert s2 < e2
    for start, end in spans[:-1]:
        assert CHUNK_CHARS * 3 // 4 <= end - start <= CHUNK_CHARS


def test_a_chunk_ends_at_a_paragraph_or_page_break_when_one_is_near():
    text = _paragraphs(40)

    spans = split_text(text)

    for _start, end in spans[:-1]:
        assert text[end - 2 : end] == "\n\n", "cut inside a paragraph although a break was near"


def test_a_chunk_ends_before_a_slide_marker():
    slides = "\n".join(
        f"--- Slide {i} ---\n" + "\n".join(f"bullet {i}.{j} " + "x" * 60 for j in range(8))
        for i in range(1, 40)
    )

    spans = split_text(slides)

    for _start, end in spans[:-1]:
        assert slides[end:].startswith("--- Slide"), "a slide was cut in two"


def test_a_text_without_breaks_is_cut_at_a_space_and_never_mid_word():
    text = " ".join(f"token{i}" for i in range(3000))

    spans = split_text(text)

    assert len(spans) > 1
    for start, end in spans:
        assert start == 0 or text[start - 1] == " "
        assert end == len(text) or text[end - 1] == " "


def test_one_unbroken_run_is_still_cut_and_always_moves_on():
    text = "x" * (CHUNK_CHARS * 3)

    spans = split_text(text)

    assert spans[0] == (0, CHUNK_CHARS)
    assert spans[-1][1] == len(text)
    assert all(b[0] > a[0] for a, b in zip(spans, spans[1:], strict=False))


def test_the_tail_joins_the_last_chunk_instead_of_standing_alone():
    text = "y " * ((CHUNK_CHARS + 300) // 2)

    assert len(split_text(text)) == 1


def test_a_very_long_text_stops_at_the_chunk_cap(monkeypatch):
    monkeypatch.setattr(chunks, "CHUNK_MAX_PER_ATTACHMENT", 3)

    spans = split_text(_paragraphs(80))

    assert len(spans) == 3


# --- attachment_chunks -----------------------------------------------------------------------


def _store(tmp_path):
    return create_database(str(tmp_path / "brain.db"))


def _long(conn, filename="annual-report.pdf", paragraphs=20, summary="An annual report."):
    return factories.attachment(
        conn, filename=filename, text=_paragraphs(paragraphs), summary=summary
    )


def _rows(conn, attachment_id):
    return conn.execute(
        "SELECT chunk_no, start_char, end_char, chars, text_chars FROM attachment_chunks "
        "WHERE attachment_id = ? ORDER BY chunk_no",
        (attachment_id,),
    ).fetchall()


def test_plan_records_each_chunk_of_a_long_summarised_document(tmp_path):
    conn = _store(tmp_path)
    attachment_id = _long(conn)
    text = conn.execute(
        "SELECT extracted_text FROM attachment_content WHERE attachment_id = ?", (attachment_id,)
    ).fetchone()[0]

    plan = plan_chunks(conn)

    rows = _rows(conn, attachment_id)
    assert [(r[1], r[2]) for r in rows] == split_text(text)
    assert [r[0] for r in rows] == list(range(len(rows)))
    assert all(r[3] == r[2] - r[1] and r[4] == len(text) for r in rows)
    assert plan.changed == {attachment_id}
    assert set(plan.spans) == {attachment_id}


def test_short_unsummarised_and_other_kinds_get_no_chunks(tmp_path):
    conn = _store(tmp_path)
    factories.attachment(
        conn, filename="memo.pdf", text="x " * (CHUNK_MIN_CHARS // 2 - 10), summary="s"
    )
    factories.attachment(conn, filename="draft.pdf", text=_paragraphs(20), summary=None)
    factories.attachment(conn, filename="figures.xlsx", text=_paragraphs(20), summary="s")

    plan = plan_chunks(conn)

    assert conn.execute("SELECT COUNT(*) FROM attachment_chunks").fetchone()[0] == 0
    assert plan.spans == {}


def test_planning_again_changes_nothing(tmp_path):
    conn = _store(tmp_path)
    attachment_id = _long(conn)
    plan_chunks(conn)
    before = _rows(conn, attachment_id)

    plan = plan_chunks(conn)

    assert plan.changed == set()
    assert _rows(conn, attachment_id) == before
    assert set(plan.spans) == {attachment_id}


def test_a_text_read_again_is_chunked_again(tmp_path):
    conn = _store(tmp_path)
    attachment_id = _long(conn)
    plan_chunks(conn)
    conn.execute(
        "UPDATE attachment_content SET extracted_text = ?, extracted_at = '2026-10-16T09:00:00Z'"
        " WHERE attachment_id = ?",
        (_paragraphs(30), attachment_id),
    )
    conn.commit()

    plan = plan_chunks(conn)

    assert plan.changed == {attachment_id}
    assert _rows(conn, attachment_id)[-1][4] == len(_paragraphs(30))


def test_a_document_no_longer_eligible_loses_its_chunks(tmp_path):
    """A summary cleared (ocr-regarble --apply) or an attachment forgotten: the rows go, and the
    index drops the vectors no row stands for."""
    conn = _store(tmp_path)
    attachment_id = _long(conn)
    plan_chunks(conn)
    conn.execute(
        "UPDATE attachment_content SET summary = NULL, llm_status = 'pending'"
        " WHERE attachment_id = ?",
        (attachment_id,),
    )
    conn.commit()

    plan = plan_chunks(conn)

    assert _rows(conn, attachment_id) == []
    assert plan.spans == {}


def test_a_deadline_leaves_the_rest_for_the_next_pass(tmp_path):
    conn = _store(tmp_path)
    first, second = _long(conn, "a.pdf"), _long(conn, "b.docx")

    plan = plan_chunks(conn, deadline=0.0)

    assert plan.changed == set() and plan.deferred == 2
    assert plan_chunks(conn).changed == {first, second}


def test_chunk_texts_carry_the_title_the_email_and_the_summary(tmp_path):
    conn = _store(tmp_path)
    email_id = factories.email(conn, subject="Board pack", date_received="2026-09-30T08:00:00Z")
    attachment_id = factories.attachment(
        conn, email_id=email_id, filename="pack.pdf", text=_paragraphs(20), summary="The pack."
    )
    plan = plan_chunks(conn)

    texts = chunks.chunk_texts(conn, attachment_id, plan.spans[attachment_id])

    text = conn.execute(
        "SELECT extracted_text FROM attachment_content WHERE attachment_id = ?", (attachment_id,)
    ).fetchone()[0]
    _no, start, end = plan.spans[attachment_id][1]
    assert texts[1] == (
        f"title: pack.pdf | text: Board pack, 2026-09-30. The pack.\n\n{text[start:end]}"
    )
    assert len(texts) == len(plan.spans[attachment_id])


def test_converting_its_stamp_to_utc_does_not_cut_a_document_again(tmp_path):
    """`maint utc-stamps` rewrites a local extraction time in UTC and drops its last digit; the
    text is the same, and cutting every document again would pay for every chunk vector again."""
    conn = _store(tmp_path)
    attachment_id = factories.attachment(
        conn,
        filename="report.pdf",
        text=_paragraphs(20),
        summary="s",
        content_columns={"extracted_at": "2026-10-16T12:16:09.841728"},
    )
    plan_chunks(conn)
    conn.execute(
        "UPDATE attachment_content SET extracted_at = ? WHERE attachment_id = ?",
        (to_utc("2026-10-16T12:16:09.841728"), attachment_id),
    )
    conn.commit()

    assert plan_chunks(conn).changed == set()


def test_the_v39_migration_is_idempotent(tmp_path):
    conn = _store(tmp_path)
    attachment_id = _long(conn)
    plan_chunks(conn)
    before = _rows(conn, attachment_id)

    migrate_add_attachment_chunks(conn)
    migrate_add_attachment_chunks(conn)

    assert _rows(conn, attachment_id) == before
    assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
