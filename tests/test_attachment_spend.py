"""Phase 2 spends what a document is worth, and never more than a run allows.

Until 2026-10-11 a long text was summarised from up to 50 parts plus a merge, a long workbook's
cell dump included, and no run had a ceiling: one day of attachment summaries cost about $2,680
(audit 2026-10-11). Now a long spreadsheet goes in one call over its digest, any other long text
in three parts plus the merge unless the row is flagged for every part, a run can be given a
token budget it stops before passing, and the pending work can be priced without a call.
"""

import json
import sqlite3

import pytest

from src.extract import attachment_pipeline as ap
from src.extract import claude_extract
from src.extract import reextract as rx
from src.extract.attachment_prompt import build_attachment_prompt, split_text
from src.llm_cost import (
    ESTIMATE_MODEL,
    OUTPUT_TOKENS_PER_CALL,
    RATES,
    TokenBudget,
    cost_usd,
)
from src.store.schema import create_database

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class _Usage:
    def __init__(self, i, o):
        self.input_tokens = i
        self.output_tokens = o


class _TextBlock:
    def __init__(self, text):
        self.text = text


class _Response:
    def __init__(self, text, usage):
        self.content = [_TextBlock(text)]
        self.stop_reason = "end_turn"
        self.model = "claude-sonnet-5-5"
        self.usage = usage


def _model(monkeypatch, calls):
    """A model that answers every prompt, reporting one input token per prompt character."""

    class FakeMessages:
        def create(self, **kw):
            prompt = kw["messages"][0]["content"]
            calls.append(prompt)
            merge = "extractions of the parts" in prompt
            body = {
                "summary": "the whole document" if merge else f"call {len(calls)}",
                "topics": [],
                "decisions": [],
                "action_items": [],
                "key_facts": [],
                "language": "english",
            }
            return _Response(json.dumps(body), _Usage(len(prompt), 0))

    fake = type("Client", (), {"messages": FakeMessages()})()
    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (fake, "m"))


def _no_model(monkeypatch):
    def refuse():
        raise AssertionError("an estimate must not reach the model")

    monkeypatch.setattr(claude_extract, "_get_client_and_model", refuse)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (7, '2026-09-01', 'mail')"
    )
    conn.commit()
    yield path, conn
    conn.close()


def _add(conn, text, *, method="direct_read", name="doc.txt", mime="text/plain", llm="pending"):
    att = conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at) VALUES (1, 7, ?, ?, 1, ?, '2026-09-01')",
        (name, mime, f"/x/{name}"),
    ).lastrowid
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extracted_at, llm_status, summary)"
        " VALUES (?, ?, ?, 'extracted', '2026-09-01', ?, 'old summary')",
        (att, text, method, llm),
    )
    conn.commit()
    return att


def _status(conn, att):
    return tuple(
        conn.execute(
            "SELECT llm_status, summary FROM attachment_content WHERE attachment_id = ?", (att,)
        ).fetchone()
    )


def _sheet_dump(rows: int) -> str:
    """A workbook's cell dump as Phase 1 stores it."""
    lines = ["Ref | Region | Amount"] + [f"R{i} | Region {i % 5} | {i}" for i in range(rows)]
    return "--- Sheet: Data ---\n" + "\n".join(lines)


def _long_text(chars: int) -> str:
    paragraph = "Paragraph about the plan. " + "word " * 400
    return "\n\n".join([paragraph] * (chars // len(paragraph) + 1))[:chars]


def _prompt(text, name="doc.txt", mime="text/plain", part=None):
    return build_attachment_prompt(
        extracted_text=text,
        filename=name,
        mime_type=mime,
        email_subject="mail",
        email_date="2026-09-01",
        part=part,
    )


# --- structure-first spreadsheets ----------------------------------------------


def test_a_long_spreadsheet_is_summarised_in_one_call_over_its_digest(db, monkeypatch):
    path, conn = db
    dump = _sheet_dump(3_000)
    att = _add(conn, dump, method="openpyxl", name="book.xlsx", mime=XLSX)
    calls: list[str] = []
    _model(monkeypatch, calls)

    stats = ap.run_phase2(str(path))

    assert len(dump) > ap.LONG_TEXT_CHARS
    assert stats["extracted"] == 1 and len(calls) == 1
    assert "Spreadsheet digest" in calls[0]
    assert "R1500 | Region 0 | 1500" not in calls[0]  # a middle row: the cells are not sent
    assert len(calls[0]) < len(dump) // 5
    assert _status(conn, att) == ("extracted", "call 1")
    stored = conn.execute("SELECT extracted_text FROM attachment_content").fetchone()[0]
    assert stored == dump  # the full dump stays for keyword search


def test_a_short_spreadsheet_is_still_sent_whole(db, monkeypatch):
    path, conn = db
    dump = _sheet_dump(200)
    _add(conn, dump, method="openpyxl", name="book.xlsx", mime=XLSX)
    calls: list[str] = []
    _model(monkeypatch, calls)

    ap.run_phase2(str(path))

    assert len(calls) == 1
    assert "Spreadsheet digest" not in calls[0]
    assert "R150 | Region 0 | 150" in calls[0]


def test_parts_saved_for_a_spreadsheet_by_an_older_run_are_dropped(db, monkeypatch):
    path, conn = db
    _add(conn, _sheet_dump(3_000), method="openpyxl", name="book.xlsx", mime=XLSX)
    ac_id = conn.execute("SELECT id FROM attachment_content").fetchone()[0]
    ap.PARTS_DIR.mkdir(parents=True, exist_ok=True)
    (ap.PARTS_DIR / f"{ac_id}.json").write_text(json.dumps({"parts": {}}))
    _model(monkeypatch, [])

    ap.run_phase2(str(path))

    assert not (ap.PARTS_DIR / f"{ac_id}.json").exists()


# --- the part cap ----------------------------------------------------------------


def test_a_long_text_is_summarised_from_three_parts_and_a_merge(db, monkeypatch):
    path, conn = db
    text = _long_text(250_000)
    att = _add(conn, text)
    n = len(split_text(text))
    calls: list[str] = []
    _model(monkeypatch, calls)

    ap.run_phase2(str(path))

    assert n > 3
    assert len(calls) == 3 + 1
    assert f"part 1 of {n}" in calls[0] and f"part {n} of {n}" in calls[2]
    assert f"3 of the document's {n} parts" in calls[3]
    assert _status(conn, att) == ("extracted", "the whole document")


def test_the_third_part_is_the_one_richest_in_headings():
    plain = "word " * 100
    outline = "\n".join(
        ["Contents", "1. Scope ........ 3", "2. Findings ........ 7", "3. Actions ........ 12"]
    )
    parts = [plain, plain, plain, outline, plain, plain]

    assert ap._choose_parts(parts, full=False) == [0, 3, 5]


def test_numbered_and_capitalised_headings_count_too():
    plain = "word " * 100
    headed = "\n".join(["ΕΙΣΑΓΩΓΗ", plain, "2.1 Scope of the review", plain, "## Findings", plain])
    parts = [plain, plain, headed, plain, plain]

    assert ap._choose_parts(parts, full=False) == [0, 2, 4]


def test_without_headings_the_third_part_is_the_middle_one():
    assert ap._choose_parts(["word " * 100] * 7, full=False) == [0, 3, 6]


def test_three_parts_or_fewer_are_all_kept():
    assert ap._choose_parts(["a", "b", "c"], full=False) == [0, 1, 2]


def test_a_flagged_text_keeps_the_spread_of_fifty():
    chosen = ap._choose_parts(["x"] * 120, full=True)

    assert len(chosen) == ap.MAX_SUMMARY_PARTS == 50
    assert (chosen[0], chosen[-1]) == (0, 119)


def test_a_flagged_row_is_summarised_from_every_part(db, monkeypatch):
    path, conn = db
    text = _long_text(250_000)
    att = _add(conn, text)
    ap.mark_full_parts(conn, [att])
    calls: list[str] = []
    _model(monkeypatch, calls)

    ap.run_phase2(str(path))

    assert ap.full_parts_ids(conn) == {att}
    assert len(calls) == len(split_text(text)) + 1


def test_a_flagged_long_spreadsheet_goes_in_parts_not_in_a_digest(db, monkeypatch):
    path, conn = db
    dump = _sheet_dump(3_000)
    att = _add(conn, dump, method="openpyxl", name="book.xlsx", mime=XLSX)
    ap.mark_full_parts(conn, [att])
    calls: list[str] = []
    _model(monkeypatch, calls)

    ap.run_phase2(str(path))

    assert len(calls) == len(split_text(dump)) + 1
    assert not any("Spreadsheet digest" in c for c in calls)


def test_flagging_is_idempotent(db):
    _path, conn = db

    ap.mark_full_parts(conn, [5, 5, 6])
    ap.mark_full_parts(conn, [6])

    assert ap.full_parts_ids(conn) == {5, 6}


# --- the token budget ----------------------------------------------------------


def _three_notes(conn):
    note = "A note about the plan for the regional network. " * 30
    return [_add(conn, note, name=f"n{i}.txt") for i in range(3)], note


def test_the_run_stops_before_the_call_that_would_pass_the_budget(db, monkeypatch):
    path, conn = db
    atts, note = _three_notes(conn)
    size = len(_prompt(note, name="n0.txt"))
    budget = TokenBudget(2 * size + size // 2, chars_per_token=1.0, output_tokens=0)
    calls: list[str] = []
    _model(monkeypatch, calls)

    stats = ap.run_phase2(str(path), token_budget=budget)

    assert len(calls) == 2
    assert (stats["extracted"], stats["over_budget"], stats["failed"]) == (2, 1, 0)
    assert _status(conn, atts[2]) == ("pending", "old summary")
    assert budget.spent == sum(len(c) for c in calls)  # charged what the responses reported


def test_with_workers_the_budget_still_bounds_the_calls(db, monkeypatch):
    path, conn = db
    note = "A note about the plan for the regional network. " * 30
    for i in range(9):
        _add(conn, note, name=f"n{i}.txt")
    size = len(_prompt(note, name="n0.txt"))
    budget = TokenBudget(4 * size + size // 2, chars_per_token=1.0, output_tokens=0)
    calls: list[str] = []
    _model(monkeypatch, calls)

    stats = ap.run_phase2(str(path), workers=3, token_budget=budget)

    assert len(calls) == 4
    assert (stats["extracted"], stats["over_budget"]) == (4, 5)


def test_a_budget_stop_between_parts_keeps_the_finished_parts(db, monkeypatch):
    path, conn = db
    text = _long_text(250_000)
    att = _add(conn, text)
    parts = split_text(text)
    first, middle, _last = ap._choose_parts(parts, full=False)
    paid = sum(len(_prompt(parts[i], part=(i + 1, len(parts)))) for i in (first, middle))
    calls: list[str] = []
    _model(monkeypatch, calls)

    stats = ap.run_phase2(
        str(path), token_budget=TokenBudget(paid + 10, chars_per_token=1.0, output_tokens=0)
    )

    assert len(calls) == 2 and stats["over_budget"] == 1
    assert _status(conn, att)[0] == "pending"

    ap.run_phase2(str(path))  # the next run, no budget

    assert len(calls) == 2 + 2  # the last part and the merge, nothing paid twice
    assert _status(conn, att) == ("extracted", "the whole document")


def test_no_budget_is_no_limit(db, monkeypatch):
    path, conn = db
    _three_notes(conn)
    calls: list[str] = []
    _model(monkeypatch, calls)

    stats = ap.run_phase2(str(path))

    assert (len(calls), stats["over_budget"]) == (3, 0)


# --- the dry-run estimate ------------------------------------------------------


def test_the_estimate_counts_every_call_and_asks_the_model_nothing(db, monkeypatch):
    path, conn = db
    long_text = _long_text(250_000)
    _add(conn, "A short note about the plan. " * 50)  # one call
    _add(conn, _sheet_dump(3_000), method="openpyxl", name="b.xlsx", mime=XLSX)  # its digest
    _add(conn, long_text, name="long.txt")  # three parts and the merge
    flagged = _add(conn, long_text, name="flag.txt")  # every part and the merge
    ap.mark_full_parts(conn, [flagged])
    _add(conn, "Summarised already. " * 20, llm="extracted")  # not pending
    _no_model(monkeypatch)
    n = len(split_text(long_text))

    est = ap.estimate_phase2(str(path))

    assert est["rows"] == 4
    assert est["calls"] == 1 + 1 + 4 + (n + 1)
    assert est["output_tokens"] == est["calls"] * OUTPUT_TOKENS_PER_CALL
    rates = RATES[ESTIMATE_MODEL]
    tokens = {"input_tokens": est["input_tokens"], "output_tokens": est["output_tokens"]}
    assert est["cost_usd"] == pytest.approx(cost_usd(rates, **tokens))
    assert est["batch_cost_usd"] == pytest.approx(cost_usd(rates, **tokens, batch=True))


def test_a_long_spreadsheet_is_estimated_at_its_digest(db, monkeypatch):
    path, conn = db
    dump = _sheet_dump(3_000)
    _add(conn, dump, method="openpyxl", name="b.xlsx", mime=XLSX)
    _no_model(monkeypatch)

    est = ap.estimate_phase2(str(path), chars_per_token=1.0)

    assert est["calls"] == 1
    assert est["input_tokens"] < len(dump) // 5


def test_the_estimate_honours_the_limit_and_the_type(db, monkeypatch):
    path, conn = db
    _three_notes(conn)
    _add(conn, _sheet_dump(100), method="openpyxl", name="b.xlsx", mime=XLSX)
    _no_model(monkeypatch)

    assert ap.estimate_phase2(str(path), limit=2)["rows"] == 2
    assert ap.estimate_phase2(str(path), file_type="excel")["rows"] == 1


# --- reextract --------------------------------------------------------------------


@pytest.fixture
def vectors(monkeypatch):
    removed: list = []
    monkeypatch.setattr(
        "src.store.embeddings.remove_vectors", lambda ids: removed.extend(ids) or len(ids)
    )
    return removed


def test_full_parts_flags_the_row_and_summarises_it_from_every_part(db, monkeypatch, vectors):
    path, conn = db
    text = _long_text(250_000)
    att = _add(conn, text, llm="extracted")
    calls: list[str] = []
    _model(monkeypatch, calls)

    stats = rx.reextract(str(path), set(), full_parts=[att])

    assert ap.full_parts_ids(conn) == {att}
    assert (stats["flagged"], stats["summarised"]) == (1, 1)
    assert len(calls) == len(split_text(text)) + 1
    assert _status(conn, att) == ("extracted", "the whole document")


def test_a_row_the_budget_left_pending_keeps_its_vector(db, monkeypatch, vectors):
    path, conn = db
    atts, note = _three_notes(conn)
    conn.execute("UPDATE attachment_content SET llm_status = 'extracted'")
    conn.commit()
    size = len(_prompt(note, name="n0.txt"))
    calls: list[str] = []
    _model(monkeypatch, calls)

    stats = rx.reextract(
        str(path),
        set(),
        full_parts=atts[:2],
        workers=1,
        token_budget=TokenBudget(size + size // 2, chars_per_token=1.0, output_tokens=0),
    )

    assert (stats["summarised"], stats["over_budget"]) == (1, 1)
    first = conn.execute(
        "SELECT id FROM attachment_content WHERE attachment_id = ?", (atts[0],)
    ).fetchone()[0]
    assert vectors == [-first]
    assert _status(conn, atts[1]) == ("pending", "old summary")


def test_the_reextract_estimate_changes_nothing_and_asks_nothing(db, monkeypatch):
    path, conn = db
    att = _add(conn, _long_text(60_000), name="mid.txt", llm="extracted")
    _no_model(monkeypatch)

    est = rx.estimate_reextract(str(path), {"long"}, full_parts=[att])

    assert (est["selected"], est["rows"], est["unknown"]) == (1, 1, 0)
    assert est["calls"] == len(split_text(_long_text(60_000))) + 1  # flagged: every part
    assert _status(conn, att) == ("extracted", "old summary")
    assert ap.full_parts_ids(conn) == set()  # an estimate flags nothing


def test_the_reextract_estimate_counts_rows_whose_text_is_not_read_yet(db, monkeypatch, tmp_path):
    path, conn = db
    att = conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at) VALUES (1, 7, 'pack.zip', 'application/zip', 1,"
        " '/x/pack.zip', '2026-09-01')"
    ).lastrowid
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extracted_at, llm_status)"
        " VALUES (?, NULL, 'zip', 'skipped', '2026-09-01', 'pending')",
        (att,),
    )
    conn.commit()
    _no_model(monkeypatch)

    est = rx.estimate_reextract(str(path), {"zip"})

    assert (est["selected"], est["rows"], est["unknown"], est["calls"]) == (1, 0, 1, 0)


def test_the_estimate_reads_no_row_twice(db, monkeypatch):
    """A row two selectors choose is estimated once."""
    path, conn = db
    _add(conn, _long_text(60_000), name="mid.txt", llm="extracted")
    _no_model(monkeypatch)

    once = rx.estimate_reextract(str(path), {"long"})
    att = conn.execute("SELECT id FROM attachments").fetchone()[0]
    twice = rx.estimate_reextract(str(path), {"long"}, full_parts=[att])

    assert once["selected"] == twice["selected"] == 1


def test_pending_rows_left_by_the_budget_are_still_pending_for_the_nightly_pass(db, monkeypatch):
    path, conn = db
    _three_notes(conn)
    _model(monkeypatch, [])

    ap.run_phase2(str(path), token_budget=TokenBudget(1, chars_per_token=1.0, output_tokens=0))

    pending = (
        sqlite3.connect(path)
        .execute("SELECT COUNT(*) FROM attachment_content WHERE llm_status = 'pending'")
        .fetchone()[0]
    )
    assert pending == 3
