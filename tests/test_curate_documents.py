"""
Tests for scripts/curate_documents_daily.py.

Regression cover for the extended-thinking content-block bug. The script was
written before commit 0cc1c50 ("extract: read past thinking blocks") and lived
outside version control on the VPS, so it never received that fix. Both of its
LLM call sites indexed ``response.content[0].text``, which raises
AttributeError when the first block is a ThinkingBlock. Observed in production
on 2026-08-11: every classification failed with

    classify error for id=31376: 'ThinkingBlock' object has no attribute 'text'

Regression cover, too, for the two defects that made the job eat itself
(measured 2026-08-30): it re-offered its own placed output back to itself
through the reverse-ingest pipeline, and it marked a candidate processed
BEFORE the soft-cap check, so a full folder destroyed the document instead of
postponing it.

The script loads its organisational taxonomy from a private file outside the
repo, so these tests stand up a stub taxonomy under a temporary HOME and load
the script the way tests/test_health_check.py loads its script.
"""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

CURATE_PATH = Path(__file__).parent.parent / "scripts" / "curate_documents_daily.py"

STUB_TAXONOMY = """\
MANAGED_FOLDERS = ["Area/one", "Area/two"]
SOFT_CAPS = {"Area/one": 10}
TAXONOMY = "stub taxonomy"
CLASSIFY_PROMPT = (
    "{taxonomy} {filename} {subject} {sender} {date} {size_mb} {summary}"
)
SUMMARIZE_PROMPT = "{folder} {readme}"
NOISE_SENDERS = set()
AREAS = ["Area"]
"""


class _ThinkingBlock:
    """Stand-in for anthropic's ThinkingBlock: exposes .thinking, never .text."""

    def __init__(self, thinking: str) -> None:
        self.thinking = thinking


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    def __init__(self, *blocks) -> None:
        self.content = list(blocks)


@pytest.fixture
def curate(tmp_path, monkeypatch):
    """Load scripts/curate_documents_daily.py against a stub private taxonomy."""
    taxonomy = tmp_path / "SourceCode" / "claude-config" / "private" / "curate-taxonomy.py"
    taxonomy.parent.mkdir(parents=True)
    taxonomy.write_text(STUB_TAXONOMY)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    spec = importlib.util.spec_from_file_location("curate_documents_daily", CURATE_PATH)
    assert spec and spec.loader, f"could not load {CURATE_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["curate_documents_daily"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("curate_documents_daily", None)


def _capture_response(monkeypatch, curate, response):
    """Make both call sites return ``response`` from the LLM."""
    monkeypatch.setattr(curate, "create_with_refusal_fallback", lambda *a, **k: response)


def _capture_kwargs(monkeypatch, curate, response):
    """Record the kwargs each call site sends to the LLM."""
    seen = {}

    def fake(client, **kwargs):
        seen.update(kwargs)
        return response

    monkeypatch.setattr(curate, "create_with_refusal_fallback", fake)
    return seen


def test_classify_reads_past_a_thinking_block(curate, monkeypatch):
    """A leading ThinkingBlock must not sink the classification."""
    _capture_response(
        monkeypatch,
        curate,
        _Response(
            _ThinkingBlock("weighing the folders"),
            _TextBlock('{"folder": "Area/one", "confidence": "high"}'),
        ),
    )
    candidate = {
        "filename": "deck.pdf",
        "subject": "s",
        "sender": "a@example.com",
        "date": "2026-08-11",
        "file_size": 1024,
        "summary": "body",
    }
    assert curate.classify_one(object(), "model", candidate) == {
        "folder": "Area/one",
        "confidence": "high",
    }


def test_summarize_folder_reads_past_a_thinking_block(curate, monkeypatch):
    """The second call site had the same defect."""
    _capture_response(
        monkeypatch,
        curate,
        _Response(
            _ThinkingBlock("reading the readme"),
            _TextBlock('{"summary": "ok"}'),
        ),
    )
    assert curate.summarize_folder(object(), "model", "Area/one", "readme") == {"summary": "ok"}


def test_classify_still_parses_a_plain_text_response(curate, monkeypatch):
    """No thinking block: unchanged behaviour."""
    _capture_response(
        monkeypatch,
        curate,
        _Response(_TextBlock('```json\n{"folder": "Area/two", "confidence": "low"}\n```')),
    )
    candidate = {
        "filename": "x.pdf",
        "subject": "",
        "sender": "",
        "date": "2026-08-11",
        "file_size": 1,
        "summary": "",
    }
    assert curate.classify_one(object(), "model", candidate) == {
        "folder": "Area/two",
        "confidence": "low",
    }


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda m: m.classify_one(
                object(),
                "model",
                {
                    "filename": "f.pdf",
                    "subject": "",
                    "sender": "",
                    "date": "2026-08-11",
                    "file_size": 1,
                    "summary": "",
                },
            ),
            id="classify_one",
        ),
        pytest.param(
            lambda m: m.summarize_folder(object(), "model", "Area/one", "readme"),
            id="summarize_folder",
        ),
    ],
)
def test_output_budget_leaves_room_after_thinking(curate, monkeypatch, call):
    """max_tokens must fund the thinking budget AND the answer.

    Observed on the VPS 2026-08-11 with the original max_tokens=300::

        stop_reason: max_tokens
        output_tokens: 300  thinking_tokens: 300
        blocks: ['ThinkingBlock']

    Extended thinking spends the same budget the answer draws on, so a ceiling
    below the 1024-token minimum thinking budget can never return text. Both
    call sites use the repo-wide MAX_OUTPUT_TOKENS so they cannot drift apart.
    """
    seen = _capture_kwargs(monkeypatch, curate, _Response(_TextBlock("{}")))
    call(curate)
    assert seen["max_tokens"] == curate.MAX_OUTPUT_TOKENS
    assert seen["max_tokens"] > 1024


def test_missing_taxonomy_file_exits_loud(tmp_path, monkeypatch):
    """No private taxonomy must be a loud exit, never a silent degraded mode."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    spec = importlib.util.spec_from_file_location("curate_documents_daily_missing", CURATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(SystemExit) as excinfo:
        spec.loader.exec_module(module)
    assert "curate-taxonomy.py" in str(excinfo.value)


# --- The feedback loop and the cap burn ----------------------------------


class _FakeClient:
    """AnthropicVertex stand-in: main() only ever calls close() on it."""

    def close(self) -> None:
        pass


def _seed_candidate(conn, src_dir, *, row_id, filename, mailbox_name, message_id):
    """Insert one candidate that satisfies every clause of the real query.

    Only the columns query_new_candidates reads are modelled; the real schema
    lives in src/store/schema.py and is far wider.
    """
    src = src_dir / f"{row_id}_{filename}"
    src.write_bytes(b"pdf bytes")
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, sender_address, subject, mailbox_name) "
        "VALUES (?, ?, '2026-08-11T09:00:00', 'someone@example.com', 'quarterly deck', ?)",
        (row_id, message_id, mailbox_name),
    )
    conn.execute(
        "INSERT INTO attachments "
        "(id, email_id, filename, mime_type, file_size, file_path, is_inline) "
        "VALUES (?, ?, ?, 'application/pdf', 900000, ?, 0)",
        (row_id, row_id, filename, str(src)),
    )
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, summary, llm_status) "
        "VALUES (?, ?, 'extracted')",
        (row_id, "s" * 400),
    )


@pytest.fixture
def brain(curate, tmp_path, monkeypatch):
    """A three-table stand-in store, wired into the loaded script."""
    db = tmp_path / "brain.db"
    src_dir = tmp_path / "exported"
    src_dir.mkdir()
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE emails (
            id INTEGER PRIMARY KEY,
            message_id INTEGER UNIQUE NOT NULL,
            date_received TEXT NOT NULL,
            sender_address TEXT,
            subject TEXT,
            mailbox_name TEXT
        );
        CREATE TABLE attachments (
            id INTEGER PRIMARY KEY,
            email_id INTEGER REFERENCES emails(id),
            filename TEXT NOT NULL,
            mime_type TEXT,
            file_size INTEGER,
            file_path TEXT NOT NULL,
            is_inline INTEGER DEFAULT 0
        );
        CREATE TABLE attachment_content (
            id INTEGER PRIMARY KEY,
            attachment_id INTEGER NOT NULL,
            summary TEXT,
            llm_status TEXT NOT NULL DEFAULT 'pending'
        );
    """)
    conn.commit()
    monkeypatch.setattr(curate, "DB", db)
    (curate.DOCS / "Area").mkdir(parents=True)
    yield type("Brain", (), {"conn": conn, "src_dir": src_dir, "path": db})
    conn.close()


def _fill(curate, folder: str, count: int) -> None:
    """Put `count` unrelated files in a managed folder, as the operator would."""
    target = curate.DOCS / folder
    target.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (target / f"existing_{i}.pdf").write_bytes(b"x")


def _run(curate, monkeypatch, verdicts, max_new=30) -> list[int]:
    """Drive main() end to end with a stubbed LLM.

    Returns the candidate ids that actually reached the classifier, which is
    what the forward-progress assertion turns on.
    """
    seen: list[int] = []

    def fake_classify(client, model, c):
        seen.append(c["id"])
        return verdicts[c["id"]]

    monkeypatch.setattr(curate, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setattr(curate, "get_client", lambda: (_FakeClient(), "model"))
    monkeypatch.setattr(curate, "classify_one", fake_classify)
    monkeypatch.setattr(curate, "summarize_folder", lambda *a, **k: {"purpose": "stub"})
    monkeypatch.setattr(sys, "argv", ["curate_documents_daily.py", "--max-new", str(max_new)])
    assert curate.main() == 0
    return seen


def _state(curate) -> dict:
    return json.loads(curate.STATE.read_text())


def _placed(curate, folder: str) -> list[str]:
    """Files this run copied in: not the operator's, not the generated README."""
    target = curate.DOCS / folder
    if not target.exists():
        return []
    return sorted(
        f.name
        for f in target.iterdir()
        if not f.name.startswith("existing_") and f.name != "README.md"
    )


def test_reverse_ingested_output_is_not_a_candidate(curate, brain):
    """curate's own placed files come back as mailbox_name='External' rows.

    sb-reverse-ingest scans ~/Documents and loads every file it finds through
    ingest_document(), which stamps a synthetic anchor with mailbox_name
    'External' and a negative message_id. Those rows used to satisfy every
    clause of the candidate query, so the job re-classified and re-placed its
    own output under a second "[Document] " prefix.
    """
    _seed_candidate(
        brain.conn,
        brain.src_dir,
        row_id=2,
        filename="202608110900_placed_deck.pdf",
        mailbox_name="External",
        message_id=-4242424242,
    )
    brain.conn.commit()
    assert curate.query_new_candidates(set(), 10) == []


def test_a_normal_email_attachment_is_still_a_candidate(curate, brain):
    """The exclusion must be narrow: real mail carries TEXT message_ids."""
    _seed_candidate(
        brain.conn,
        brain.src_dir,
        row_id=1,
        filename="email_deck.pdf",
        mailbox_name="Inbox",
        message_id="AAMkADk1ZTRiexample",
    )
    _seed_candidate(
        brain.conn,
        brain.src_dir,
        row_id=2,
        filename="202608110900_placed_deck.pdf",
        mailbox_name="External",
        message_id=-4242424242,
    )
    brain.conn.commit()
    assert [c["id"] for c in curate.query_new_candidates(set(), 10)] == [1]


def test_a_parked_candidate_stops_being_retried_at_the_cap(curate, brain):
    """The bound: headroom is no longer enough once the retries are spent.

    attempts only climbs when several candidates chase the same nearly-full
    folder in one run, so this is the backstop against that churn repeating
    twice a day for good.
    """
    _seed_candidate(
        brain.conn,
        brain.src_dir,
        row_id=1,
        filename="email_deck.pdf",
        mailbox_name="Inbox",
        message_id="AAMkADk1ZTRiexample",
    )
    brain.conn.commit()
    assert curate.DOCS.joinpath("Area/one").exists() is False  # all the headroom there is

    spent = {"1": {"folder": "Area/one", "attempts": curate.MAX_DEFER_ATTEMPTS}}
    assert curate.query_new_candidates(set(), 10, spent) == []
    one_left = {"1": {"folder": "Area/one", "attempts": curate.MAX_DEFER_ATTEMPTS - 1}}
    assert [c["id"] for c in curate.query_new_candidates(set(), 10, one_left)] == [1]


def test_cap_rejected_candidate_is_deferred_not_burned(curate, brain, monkeypatch):
    """A full folder is a fact about the DESTINATION, never about the document.

    287 distinct documents were dropped and permanently flagged processed this
    way in August 2026, because processed.add() ran before the cap check.
    """
    _seed_candidate(
        brain.conn,
        brain.src_dir,
        row_id=1,
        filename="email_deck.pdf",
        mailbox_name="Inbox",
        message_id="AAMkADk1ZTRiexample",
    )
    brain.conn.commit()
    _fill(curate, "Area/one", curate.SOFT_CAPS["Area/one"])

    assert _run(curate, monkeypatch, {1: {"folder": "Area/one", "confidence": "high"}}) == [1]

    state = _state(curate)
    assert state["processed_ids"] == []
    assert state["deferred"]["1"]["folder"] == "Area/one"
    assert state["deferred"]["1"]["attempts"] == 1
    assert _placed(curate, "Area/one") == []


def test_deferred_candidate_returns_when_the_folder_has_headroom(curate, brain, monkeypatch):
    """Once the operator prunes the folder, the parked document must land."""
    _seed_candidate(
        brain.conn,
        brain.src_dir,
        row_id=1,
        filename="email_deck.pdf",
        mailbox_name="Inbox",
        message_id="AAMkADk1ZTRiexample",
    )
    brain.conn.commit()
    _fill(curate, "Area/one", curate.SOFT_CAPS["Area/one"])
    verdicts = {1: {"folder": "Area/one", "confidence": "high"}}
    _run(curate, monkeypatch, verdicts)

    (curate.DOCS / "Area/one" / "existing_0.pdf").unlink()
    assert _run(curate, monkeypatch, verdicts) == [1]

    assert _placed(curate, "Area/one") == ["202608110900_email_deck.pdf"]
    state = _state(curate)
    assert state["processed_ids"] == [1]
    assert state["deferred"] == {}


def test_a_full_folder_does_not_stall_forward_progress(curate, brain, monkeypatch):
    """The regression the naive fix would have caused.

    Candidates are ordered id DESC, so a blocked candidate left in the main
    stream is re-offered first on every run forever: no progress into the
    thousands of never-seen candidates, and a wasted classification call per
    run. Parking it must take it OUT of the stream while its folder is full.
    """
    _seed_candidate(
        brain.conn,
        brain.src_dir,
        row_id=1,
        filename="second_deck.pdf",
        mailbox_name="Inbox",
        message_id="AAMkADk1ZTRiexampleone",
    )
    _seed_candidate(
        brain.conn,
        brain.src_dir,
        row_id=3,
        filename="blocked_deck.pdf",
        mailbox_name="Inbox",
        message_id="AAMkADk1ZTRiexampletwo",
    )
    brain.conn.commit()
    _fill(curate, "Area/one", curate.SOFT_CAPS["Area/one"])
    verdicts = {
        3: {"folder": "Area/one", "confidence": "high"},
        1: {"folder": "Area/two", "confidence": "high"},
    }

    assert _run(curate, monkeypatch, verdicts, max_new=1) == [3]
    assert _run(curate, monkeypatch, verdicts, max_new=1) == [1]

    assert _placed(curate, "Area/two") == ["202608110900_second_deck.pdf"]
    assert _state(curate)["deferred"]["3"]["attempts"] == 1


def test_low_confidence_is_still_recorded_as_processed(curate, brain, monkeypatch):
    """Unchanged semantics: a weak verdict is a property of the document."""
    _seed_candidate(
        brain.conn,
        brain.src_dir,
        row_id=1,
        filename="email_deck.pdf",
        mailbox_name="Inbox",
        message_id="AAMkADk1ZTRiexample",
    )
    brain.conn.commit()
    verdicts = {1: {"folder": "Area/one", "confidence": "low"}}

    assert _run(curate, monkeypatch, verdicts) == [1]
    state = _state(curate)
    assert state["processed_ids"] == [1]
    assert state["deferred"] == {}
    assert _placed(curate, "Area/one") == []

    # Second run must not re-decide it: no classification call at all.
    assert _run(curate, monkeypatch, verdicts) == []
