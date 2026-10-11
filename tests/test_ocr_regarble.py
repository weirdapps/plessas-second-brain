"""`ocr-regarble` finds the stored OCR texts that are Greek read as Latin look-alike letters, counts
them by extraction method, and with --apply marks them and clears what was summarised from them.

Phase 1 marks such a text as it reads it; the audit's 804 were read before it did
(data-quality-03). A dry run writes nothing. --apply marks each, clears its summary, deletes the
decisions, action items and key facts its summary wrote (by attachment id) and drops its vector,
then says which files are still on disk and which were swept: ids and counts only.
"""

import sys

import pytest

from src.extract.ocr_garble import OCR_GARBLED_NOTE, is_garbled
from src.extract.ocr_regarble import regarble
from src.store.schema import create_database
from tests.greek_text import ENGLISH, GREEK, garble


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, subject)"
        " VALUES (1, 7, '2026-04-06', 'mail')"
    )
    conn.commit()
    removed: list = []
    monkeypatch.setattr(
        "src.store.embeddings.remove_vectors", lambda ids: removed.append(sorted(ids)) or len(ids)
    )
    yield path, conn, tmp_path, removed
    conn.close()


def _row(
    conn,
    root,
    text,
    method="ocr",
    *,
    error=None,
    summary=None,
    llm="pending",
    on_disk=True,
    swept=False,
):
    """An attachment and its content row; its file on disk, swept, or simply missing."""
    n = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] + 1
    f = root / "att" / f"d{n}" / "scan.png"
    if on_disk:
        f.parent.mkdir(parents=True)
        f.write_bytes(b"png")
    att = conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at, file_removed_at) VALUES (1, 7, 'scan.png', 'image/png', 3, ?,"
        " '2026-04-06', ?)",
        (str(f), "2026-09-01T00:00:00+00:00" if swept else None),
    ).lastrowid
    ac = conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extraction_error, extracted_at, summary, language, llm_status)"
        " VALUES (?, ?, ?, 'extracted', ?, '2026-04-06', ?, ?, ?)",
        (att, text, method, error, summary, "en" if summary else None, llm),
    ).lastrowid
    conn.commit()
    return att, ac


def _summarised(conn, att, *, tagged=True):
    """What a summary of the attachment wrote onto its email."""
    tag = att if tagged else None
    conn.execute(
        "INSERT INTO decisions (email_id, decision, attachment_id) VALUES (1, 'a decision', ?)",
        (tag,),
    )
    conn.execute(
        "INSERT INTO action_items (email_id, task, status, attachment_id)"
        " VALUES (1, 'a task', 'open', ?)",
        (tag,),
    )
    conn.execute(
        "INSERT INTO key_facts (email_id, fact, attachment_id) VALUES (1, 'a fact', ?)", (tag,)
    )
    conn.commit()


def _content(conn, att):
    return tuple(
        conn.execute(
            "SELECT extracted_text, extraction_error, summary, language, llm_status"
            " FROM attachment_content WHERE attachment_id = ?",
            (att,),
        ).fetchone()
    )


def _derived(conn):
    return [
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("decisions", "action_items", "key_facts")
    ]


def _corpus(conn, root):
    """Two garbled texts (one summarised, one already marked), and three it must leave."""
    summarised, _ = _row(
        conn,
        root,
        garble(GREEK),
        summary="a wrong summary",
        llm="extracted",
        on_disk=False,
        swept=True,
    )
    _summarised(conn, summarised)
    _summarised(conn, summarised, tagged=False)
    marked, _ = _row(
        conn, root, garble(GREEK), "pymupdf+tesseract", error=OCR_GARBLED_NOTE, llm="skipped"
    )
    clean, _ = _row(conn, root, GREEK, summary="a right summary", llm="extracted")
    english, _ = _row(conn, root, ENGLISH, "zip", summary="an archive", llm="extracted")
    text_layer, _ = _row(conn, root, garble(GREEK), "pymupdf", summary="as written")
    return summarised, marked, clean, english, text_layer


def test_a_dry_run_counts_by_method_and_writes_nothing(store):
    path, conn, root, removed = store
    summarised, marked, clean, english, text_layer = _corpus(conn, root)
    before = [_content(conn, att) for att in (summarised, marked, clean, english, text_layer)]

    stats = regarble(str(path), apply=False, root=root / "att")

    assert stats["by_method"] == {
        "ocr": {"scanned": 2, "garbled": 1, "marked before": 0},
        "pymupdf+tesseract": {"scanned": 1, "garbled": 1, "marked before": 1},
        "zip": {"scanned": 1, "garbled": 0, "marked before": 0},
    }
    assert stats["files"] == {
        "on disk": [marked],
        "swept": [summarised],
        "missing": [],
        "text only": [],
    }
    after = [_content(conn, att) for att in (summarised, marked, clean, english, text_layer)]
    assert after == before
    assert _derived(conn) == [2, 2, 2]
    assert removed == []


def test_apply_marks_them_and_clears_what_their_summaries_wrote(store):
    path, conn, root, removed = store
    summarised, marked, clean, english, text_layer = _corpus(conn, root)
    summarised_ac = conn.execute(
        "SELECT id FROM attachment_content WHERE attachment_id = ?", (summarised,)
    ).fetchone()[0]

    stats = regarble(str(path), apply=True, root=root / "att")

    text, error, summary, language, llm = _content(conn, summarised)
    assert (text, summary, language, llm) == (garble(GREEK), None, None, "skipped")
    assert is_garbled(error)
    # The rows with its attachment id go; the untagged copies stay on the email.
    assert _derived(conn) == [1, 1, 1]
    assert removed == [[-summarised_ac]]
    assert (stats["marked"], stats["summaries cleared"], stats["untagged"]) == (1, 1, 0)
    assert (stats["decisions"], stats["action items"], stats["key facts"]) == (1, 1, 1)
    assert _content(conn, clean)[1:] == (None, "a right summary", "en", "extracted")
    assert _content(conn, english)[1:] == (None, "an archive", "en", "extracted")
    assert _content(conn, text_layer)[1:] == (None, "as written", "en", "pending")
    assert _content(conn, marked)[1] == OCR_GARBLED_NOTE


def test_apply_keeps_what_an_error_already_said(store):
    path, conn, root, _removed = store
    att, _ = _row(conn, root, garble(GREEK), error="time budget spent, 2 pages left unread")

    regarble(str(path), apply=True, root=root / "att")

    error = _content(conn, att)[1]
    assert error.startswith("time budget spent, 2 pages left unread; ")
    assert is_garbled(error)


def test_a_summary_with_no_rows_tagged_by_its_attachment_is_counted(store):
    """Before schema v28 an attachment's rows were written with no attachment id: they cannot be
    told from the email's own and stay; the count says how many such summaries there were."""
    path, conn, root, _removed = store
    att, _ = _row(conn, root, garble(GREEK), summary="a wrong summary", llm="extracted")
    _summarised(conn, att, tagged=False)

    stats = regarble(str(path), apply=True, root=root / "att")

    assert (stats["summaries cleared"], stats["untagged"]) == (1, 1)
    assert _derived(conn) == [1, 1, 1]


def test_apply_twice_changes_nothing_the_second_time(store):
    path, conn, root, removed = store
    _corpus(conn, root)
    regarble(str(path), apply=True, root=root / "att")

    again = regarble(str(path), apply=True, root=root / "att")

    assert (again["found"], again["marked"], again["summaries cleared"]) == (2, 0, 0)
    assert len(removed) == 1


def test_a_text_only_document_and_a_missing_file_are_told_apart(store):
    path, conn, root, _removed = store
    gone, _ = _row(conn, root, garble(GREEK), on_disk=False)
    text_only, _ = _row(conn, root, garble(GREEK), "pymupdf+tesseract", on_disk=False)
    conn.execute(
        "UPDATE attachments SET file_path = 'text:sharepoint:key' WHERE id = ?", (text_only,)
    )
    conn.commit()

    stats = regarble(str(path), apply=False, root=root / "att")

    assert stats["files"] == {
        "on disk": [],
        "swept": [],
        "missing": [gone],
        "text only": [text_only],
    }


# --- the command ---------------------------------------------------------------------------


def _cli(monkeypatch, path, *argv):
    from src import cli

    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setattr(sys, "argv", ["brain", "--db", str(path), "ocr-regarble", *argv])
    try:
        cli.main()
    except SystemExit as exc:
        return exc.code
    return 0


def test_the_command_reports_a_dry_run(store, monkeypatch, capsys):
    path, conn, root, _removed = store
    _corpus(conn, root)

    code = _cli(monkeypatch, path, "--dry-run", "--root", str(root / "att"))

    out = capsys.readouterr().out
    assert code == 0
    assert "DRY RUN" in out and "pymupdf+tesseract" in out
    assert "reextract --garbled" in out


def test_the_command_is_refused_on_a_replica(store, monkeypatch, capsys):
    path, conn, root, _removed = store
    att, _ = _row(conn, root, garble(GREEK), summary="a wrong summary", llm="extracted")
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    code = _cli(monkeypatch, path, "--apply")

    assert code == 2
    assert "BRAIN_ROLE=replica" in capsys.readouterr().err
    assert _content(conn, att)[2] == "a wrong summary"


def test_apply_and_dry_run_cannot_be_given_together(store, monkeypatch):
    path, _conn, _root, _removed = store

    assert _cli(monkeypatch, path, "--apply", "--dry-run") == 2
