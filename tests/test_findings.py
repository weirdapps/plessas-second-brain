"""Findings research sessions file back, in a ```findings block of a note they write.

Research teammates make most brain calls, and what they verified ended in hand-edited
memory files or was lost. A note a session writes is already ingested
(src/export/session_notes.py); a fenced block in it now loads as findings. The agents
that write notes read third-party mail, so a mail can make one write a block: every line
fails closed. It is accepted, and only then shown or used, when it parses strictly, its
source is a primary item the store holds, and its quote or value is in that item's text;
otherwise it is stored rejected with the reason, never shown, and listed for review.
"""

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.export.session_notes import collect_session_notes, ingest_session_notes
from src.store import findings
from src.store.schema import create_database
from tests import factories as f

TODAY = date(2026, 10, 16)
BODY = "Monthly report. The okapi share reached 61.5% in August, up from 58%."


@pytest.fixture
def store(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


@pytest.fixture
def report(store):
    """An email whose body holds the figures the findings below cite."""
    return f.email(
        store, subject="Okapi report", content=BODY, date_received="2026-09-05T08:00:00Z"
    )


def _block(*rows) -> str:
    lines = [r if isinstance(r, str) else json.dumps(r) for r in rows]
    return "# Notes\n\nSome prose.\n\n```findings\n" + "\n".join(lines) + "\n```\n"


def _sync(store, *rows, path="/n/a.md", author="agent", written="2026-10-10T10:00:00Z"):
    stats = findings.sync_note_findings(
        store, path, "s1", written, _block(*rows), author_kind=author, today=TODAY
    )
    store.commit()
    return stats


def _status(store, path="/n/a.md"):
    return [
        tuple(r)
        for r in store.execute(
            "SELECT status, reason FROM findings WHERE note_path = ? ORDER BY ordinal", (path,)
        )
    ]


def _ids(store, path):
    return [r[0] for r in store.execute("SELECT id FROM findings WHERE note_path = ?", (path,))]


def _ok(report, **fields):
    return {"claim": "Okapi share", "value": "61.5%", "source_ref": f"email:{report}", **fields}


# ------------------------------------------------------- 1. checked against the source


def test_a_value_found_in_its_source_is_accepted(store, report):
    _sync(store, _ok(report, as_of="2026-08"))

    assert _status(store) == [("accepted", "ok")]


def test_a_quote_found_in_its_source_is_accepted(store, report):
    _sync(store, _ok(report, value="61.5", quote="okapi SHARE reached  61.5% in August"))

    assert _status(store) == [("accepted", "ok")]


@pytest.mark.parametrize(
    "fields, why",
    [
        ({"value": "77.7%"}, "the value is not in the source"),
        ({"value": "ugust"}, "the value is not in the source"),  # inside August, not whole
        ({"value": "58"}, "the value is too short to check: give a quote"),
        ({"value": "58%"}, "the value is too short to check: give a quote"),
        ({"value": None}, "nothing to check against the source"),
        ({"quote": "the okapi share fell sharply"}, "the quote is not in the source"),
        ({"quote": "okapi share reached 61.5%", "value": "99%"}, "the value is in neither"),
    ],
)
def test_a_claim_its_source_does_not_hold_is_rejected(store, report, fields, why):
    _sync(store, _ok(report, **fields))

    ((status, reason),) = _status(store)
    assert status == "rejected" and reason.startswith(why)


@pytest.mark.parametrize(
    "ref, why",
    [
        ("email:999", "not in the store"),
        ("the August deck", "source_ref is not kind:ref"),
        ("email:abc", "source_ref is not kind:ref"),
        ("finding:1", "a finding cannot be the source of a finding"),
        ("topic_page:1", "a topic page is derived"),
        ("conversation:1", "a session is derived, not a primary source"),
        ("image:1", "unknown kind"),
    ],
)
def test_a_source_that_is_not_a_held_primary_item_rejects_the_line(store, report, ref, why):
    _sync(store, _ok(report, source_ref=ref))

    ((status, reason),) = _status(store)
    assert status == "rejected" and reason.startswith(why)


def test_a_session_note_is_derived_and_cannot_source_a_finding(store):
    note = f.email(
        store,
        subject="[Session note] /n/a.md",
        content=BODY,
        mailbox_name="External",
        source_class="session_note",
    )
    attachment_id = f.attachment(store, email_id=note, text=BODY)

    _sync(store, {"claim": "x share", "value": "61.5%", "source_ref": f"document:{note}"})
    _sync(
        store,
        {"claim": "x share", "value": "61.5%", "source_ref": f"attachment:{attachment_id}"},
        path="/n/b.md",
    )

    assert _status(store) == [("rejected", "a session note is derived")]
    assert _status(store, "/n/b.md") == [("rejected", "a session note is derived")]


def test_an_attachment_and_a_teams_thread_are_checked_by_their_own_text(store):
    attachment_id = f.attachment(store, text="Table 3: okapi count 1,204 in Q3")
    thread_id = f.teams_thread(store, title="Okapi chat")
    f.teams_message(store, thread_id=thread_id, content_text="we agreed the okapi target 300k")

    _sync(
        store,
        {"claim": "Okapi count", "value": "1,204", "source_ref": f"attachment:{attachment_id}"},
        {"claim": "Okapi target", "value": "300k", "source_ref": f"teams:{thread_id}"},
    )

    assert _status(store) == [("accepted", "ok"), ("accepted", "ok")]


# ------------------------------------------------------------------- 2. strict parsing

_REF = "email:1"


@pytest.mark.parametrize(
    "line, why",
    [
        ("not json at all, secret 4111", "not one JSON object"),
        ('["a", "list"]', "not one JSON object"),
        ('{"claim": "Okapi share", "value": "61.5%"', "not one JSON object"),
        ({"claim": "Okapi share", "value": "61.5%", "note": "trust me"}, "1 unknown field(s)"),
        ({"value": "61.5%", "source_ref": _REF}, "claim must be text"),
        ({"claim": "x" * 301, "source_ref": _REF}, "claim must be text"),
        ({"claim": "ok", "source_ref": _REF}, "claim must be text"),
        ({"claim": "Okapi share", "value": True, "source_ref": _REF}, "value must be text"),
        ({"claim": "Okapi share", "value": "9" * 101, "source_ref": _REF}, "value must be"),
        ({"claim": "Okapi share", "value": "61.5%"}, "no source_ref"),
        ({"claim": "Okapi share", "quote": "short", "source_ref": _REF}, "quote must be"),
        ({"claim": "Okapi share", "as_of": "August", "source_ref": _REF}, "as_of is not"),
        ({"claim": "Okapi share", "as_of": "2026-02-30", "source_ref": _REF}, "as_of is not a"),
        ({"claim": "Okapi share", "as_of": "2026-11", "source_ref": _REF}, "as_of is in the"),
        ({"claim": "Okapi share", "supersedes": "the old one", "source_ref": _REF}, "supersedes"),
    ],
)
def test_a_malformed_line_is_rejected_never_guessed_and_never_echoed(store, report, line, why):
    _sync(store, line)

    ((status, reason),) = _status(store)
    assert status == "rejected" and reason.startswith(why)
    assert "4111" not in reason and "trust me" not in reason


def test_a_bad_line_rejects_that_line_only(store, report):
    stats = _sync(store, "garbage", _ok(report))

    assert _status(store) == [("rejected", "not one JSON object"), ("accepted", "ok")]
    assert (stats["accepted"], stats["rejected"]) == (1, 1)


@pytest.mark.parametrize("as_of", ["2026-10-16", "2026-10", "2026-Q4", "2026", "2025-12-31"])
def test_a_date_or_period_up_to_today_is_accepted(store, report, as_of):
    _sync(store, _ok(report, as_of=as_of))

    assert _status(store) == [("accepted", "ok")]


def test_blank_lines_and_other_fences_are_ignored():
    text = "```python\nprint('x')\n```\n" + _block("", {"claim": "kept"}, "")

    assert [line for _, _, line in findings.blocks(text)] == ['{"claim": "kept"}']


def test_a_note_without_a_block_has_no_findings():
    assert findings.blocks('# Notes\n\n```json\n{"claim": "x"}\n```\n') == []


# ----------------------------------------------------------------- 3. supersedes


def _two_reports(store):
    old = f.email(
        store,
        subject="July",
        content="okapi share 58.0% in July",
        date_received="2026-08-03T08:00:00Z",
    )
    new = f.email(
        store,
        subject="August",
        content="okapi share 61.5% in August",
        date_received="2026-09-05T08:00:00Z",
    )
    return old, new


def test_supersedes_marks_an_older_finding_of_the_same_claim(store):
    old, new = _two_reports(store)
    first = {"claim": "Okapi share", "value": "58.0%", "source_ref": f"email:{old}"}
    _sync(store, first, path="/n/old.md")
    (old_id,) = _ids(store, "/n/old.md")
    later = {"claim": "okapi  SHARE", "value": "61.5%", "source_ref": f"email:{new}"}
    _sync(store, {**later, "supersedes": f"finding:{old_id}"}, path="/n/new.md")

    rows = {r["value"]: r for r in findings.findings_matching(store, words=["okapi"])}

    assert _status(store, "/n/new.md") == [("accepted", "ok")]
    assert rows["58.0%"]["superseded_by"] == f"finding:{rows['61.5%']['id']}"
    assert rows["61.5%"]["superseded_by"] is None


@pytest.mark.parametrize(
    "claim, target, why",
    [
        ("Okapi count", "old", "supersedes names a finding with another claim"),
        ("Okapi share", "new", "supersedes names a finding whose source is not older"),
    ],
)
def test_supersedes_that_breaks_a_rule_rejects_the_line(store, claim, target, why):
    old, new = _two_reports(store)
    source, value = {"old": (old, "58.0%"), "new": (new, "61.5%")}[target]
    _sync(
        store,
        {"claim": "Okapi share", "value": value, "source_ref": f"email:{source}"},
        path="/n/old.md",
    )
    (target_id,) = _ids(store, "/n/old.md")

    line = {"claim": claim, "value": "58.0%", "source_ref": f"email:{old}"}
    _sync(store, {**line, "supersedes": f"finding:{target_id}"}, path="/n/new.md")

    assert _status(store, "/n/new.md") == [("rejected", why)]


def test_supersedes_cannot_name_a_rejected_finding_or_a_primary_item(store, report):
    _sync(store, _ok(report, value="99%"), path="/n/old.md")
    (rejected_id,) = _ids(store, "/n/old.md")

    _sync(
        store,
        _ok(report, supersedes=f"finding:{rejected_id}"),
        _ok(report, supersedes=f"email:{report}"),
    )

    assert _status(store) == [
        ("rejected", "supersedes names no accepted finding"),
        ("rejected", "supersedes must be finding:<id>"),
    ]


# ------------------------------------------------------------------ 4. author kind


def test_the_owners_findings_rank_above_an_agents(store, report):
    _sync(store, _ok(report, claim="Okapi by agent"), path="/n/agent.md", written="2026-10-12")
    _sync(
        store,
        _ok(report, claim="Okapi by owner"),
        path="/n/owner.md",
        author="owner",
        written="2026-10-01",
    )

    rows = findings.findings_matching(store, words=["okapi"])

    assert [(r["author_kind"], r["claim"]) for r in rows] == [
        ("owner", "Okapi by owner"),
        ("agent", "Okapi by agent"),
    ]


# ---------------------------------------------------------- shown only when accepted


def test_a_rejected_finding_is_never_returned_but_is_listed_for_review(store, report):
    _sync(store, _ok(report), _ok(report, value="77.7%"))

    shown = findings.findings_matching(store, words=["okapi"])
    review = findings.listed(store, findings.REJECTED)

    assert [r["value"] for r in shown] == ["61.5%"]
    assert [(r["value"], r["reason"]) for r in review] == [
        ("77.7%", "the value is not in the source")
    ]


def test_the_review_command_lists_the_rejected_with_why(store, report, capsys):
    _sync(store, _ok(report), _ok(report, value="77.7%"))
    path = store.execute("PRAGMA database_list").fetchone()[2]

    findings.cmd(SimpleNamespace(db=path, accepted=False, limit=10))

    out = capsys.readouterr().out.splitlines()
    assert "accepted (agent) 1" in out[0] and "rejected (agent) 1" in out[0]
    (listed,) = out[1:]
    assert "rejected" in listed and "the value is not in the source" in listed


def test_findings_match_a_topic_by_their_source_items_topics(store):
    tagged = f.email(store, subject="Report", content="plan cost 5,000", topics=["okapi plan"])
    other = f.email(store, subject="Other", content="zebra cost 5,000", topics=["zebra"])
    _sync(
        store,
        {"claim": "Plan cost", "value": "5,000", "source_ref": f"email:{tagged}"},
        {"claim": "Other cost", "value": "5,000", "source_ref": f"email:{other}"},
    )
    topic_id = store.execute("SELECT id FROM topics WHERE name = 'okapi plan'").fetchone()[0]

    rows = findings.findings_matching(store, topic_ids=[topic_id])

    assert [r["claim"] for r in rows] == ["Plan cost"]
    assert rows[0]["ref"] == f"finding:{rows[0]['id']}" and rows[0]["kind"] == "finding"


# ------------------------------------------------------------------- 6. fail closed


def test_an_error_while_checking_is_a_rejection_and_a_log_line(store, report, monkeypatch, caplog):
    def broken(conn, ref):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(findings, "read_source", broken)

    _sync(store, _ok(report))

    assert _status(store) == [("rejected", "validation error: RuntimeError")]
    assert "finding rejected" in caplog.text and "disk on fire" not in caplog.text


# ------------------------------------------------------------ storing a note again


def test_a_note_written_again_keeps_the_ids_of_unchanged_findings(store, report):
    _sync(store, _ok(report), _ok(report, claim="Okapi earlier", value="58%", quote="up from 58%"))
    ids = [r[0] for r in store.execute("SELECT id FROM findings ORDER BY ordinal")]

    stats = _sync(store, _ok(report), _ok(report, claim="Okapi earlier", value="77.7%"))

    rows = store.execute("SELECT id, value, status FROM findings ORDER BY ordinal").fetchall()
    assert [tuple(r) for r in rows] == [
        (ids[0], "61.5%", "accepted"),
        (ids[1], "77.7%", "rejected"),
    ]
    assert stats["changed"] == 1


def test_a_finding_whose_source_arrives_later_is_accepted(store):
    line = {"claim": "Okapi share", "value": "61.5%", "source_ref": "email:1"}
    _sync(store, line)
    assert _status(store) == [("rejected", "not in the store")]

    f.email(store, subject="Okapi report", content=BODY)
    _sync(store, line)

    assert _status(store) == [("accepted", "ok")]


def test_a_shorter_block_removes_the_findings_it_dropped(store, report):
    _sync(store, _ok(report), _ok(report))
    findings.sync_note_findings(store, "/n/a.md", "s1", "2026-10-11", "no block", today=TODAY)

    assert store.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 0


# --------------------------------------------------------- through session notes


def _use(i, path, content, session="s1"):
    return {
        "type": "assistant",
        "sessionId": session,
        "timestamp": f"2026-10-15T10:00:{i:02d}Z",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": f"{session}-t{i}",
                    "name": "Write",
                    "input": {"file_path": path, "content": content},
                }
            ],
        },
    }


def _result(i, session="s1"):
    return {
        "type": "user",
        "sessionId": session,
        "timestamp": f"2026-10-15T10:00:{i:02d}Z",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": f"{session}-t{i}"}],
        },
    }


def _prompt(text="what is the okapi share?", **extra):
    return {
        "type": "user",
        "sessionId": "s1",
        "timestamp": "2026-10-15T09:59:00Z",
        "message": {"role": "user", "content": text},
        "promptSource": "typed",
        **extra,
    }


def _transcript(path: Path, *records) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def test_a_session_note_with_a_block_loads_its_findings(store, report, tmp_path):
    note = _block(_ok(report, as_of="2026-08"))
    transcript = _transcript(
        tmp_path / "s1.jsonl", _prompt(), _use(1, "/work/okapi-findings.md", note), _result(1)
    )

    stats = ingest_session_notes(store, [transcript])

    row = store.execute(
        "SELECT note_path, session_id, author_kind, written_at, claim, value, as_of, status"
        " FROM findings"
    ).fetchone()
    assert tuple(row) == (
        "/work/okapi-findings.md",
        "s1",
        "owner",
        "2026-10-15T10:00:01Z",
        "Okapi share",
        "61.5%",
        "2026-08",
        "accepted",
    )
    assert (stats["findings_accepted"], stats["findings_rejected"]) == (1, 0)


@pytest.mark.parametrize(
    "lead",
    [
        # A teammate's session: its records name the agent.
        [_prompt(agentName="researcher", teamName="t")],
        # A subagent's, or a scripted run: no prompt the owner typed.
        [],
        # A teammate's message is not the owner typing.
        [_prompt("<teammate-message>go</teammate-message>")],
    ],
)
def test_a_note_an_agent_wrote_is_marked_so(tmp_path, lead):
    records = [*lead, _use(1, "/work/a.md", "text"), _result(1)]

    notes = collect_session_notes(_transcript(tmp_path / "s.jsonl", *records))

    assert notes["/work/a.md"].author_kind == "agent"


def test_a_note_read_again_unchanged_keeps_its_findings(store, report, tmp_path):
    note = _block(_ok(report))
    transcript = _transcript(tmp_path / "s1.jsonl", _use(1, "/work/a.md", note), _result(1))
    ingest_session_notes(store, [transcript])
    first = [tuple(r) for r in store.execute("SELECT id, loaded_at, status FROM findings")]

    ingest_session_notes(store, [transcript])

    assert [tuple(r) for r in store.execute("SELECT id, loaded_at, status FROM findings")] == first
