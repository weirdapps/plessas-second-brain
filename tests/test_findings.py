"""Findings research sessions file back, in a ```findings block of a note they write.

Research teammates make most brain calls, and what they verified ended in hand-edited
memory files or was lost. A note a session writes is already ingested
(src/export/session_notes.py); a fenced block in it now loads as findings: one JSON
object a line, {"claim", "value", "as_of", "source_ref", "supersedes"}. A finding is
derived: it is sourced only when source_ref names a primary item the store holds, and
never when it names another finding, a topic page, a session or a session note.
"""

import json
from pathlib import Path

import pytest

from src.export.session_notes import ingest_session_notes
from src.store import findings
from src.store.schema import create_database
from tests import factories as f


@pytest.fixture
def store(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


def _block(*rows, fence="findings") -> str:
    lines = [r if isinstance(r, str) else json.dumps(r) for r in rows]
    return "# Notes\n\nSome prose.\n\n```" + fence + "\n" + "\n".join(lines) + "\n```\n"


# ------------------------------------------------------------------- parsing


def test_each_line_of_the_block_is_one_finding():
    text = _block(
        {"claim": "Okapi share", "value": "61%", "as_of": "2026-08", "source_ref": "email:1"},
        {"claim": "Okapi count", "value": 12, "as_of": "2026-09-30", "source_ref": "attachment:2"},
    )

    rows, errors = findings.parse_findings(text)

    assert errors == []
    assert [r["claim"] for r in rows] == ["Okapi share", "Okapi count"]
    assert rows[1]["value"] == "12"
    assert rows[0]["source_ref"] == "email:1"


def test_blank_lines_and_other_fences_are_ignored():
    text = "```python\nprint('x')\n```\n" + _block(
        "", {"claim": "kept", "source_ref": "email:1"}, ""
    )

    rows, errors = findings.parse_findings(text)

    assert [r["claim"] for r in rows] == ["kept"]
    assert errors == []


def test_a_bad_line_is_counted_and_never_echoed():
    text = _block("not json at all, secret 4111", {"value": "orphan value"}, ["a", "list"])

    rows, errors = findings.parse_findings(text)

    assert rows == []
    assert len(errors) == 3
    assert not any("4111" in e or "orphan" in e for e in errors)


def test_two_blocks_in_one_note_both_count():
    text = _block({"claim": "one"}) + "\nmore prose\n" + _block({"claim": "two"})

    rows, _ = findings.parse_findings(text)

    assert [r["claim"] for r in rows] == ["one", "two"]


def test_a_note_without_a_block_has_no_findings():
    assert findings.parse_findings('# Notes\n\n```json\n{"claim": "x"}\n```\n') == ([], [])


# -------------------------------------------------------------- source check


def test_a_primary_item_the_store_holds_is_a_source(store):
    email_id = f.email(store, subject="Okapi report")
    attachment_id = f.attachment(store, email_id=email_id)

    assert findings.check_source(store, f"email:{email_id}") == (True, "ok")
    assert findings.check_source(store, f"attachment:{attachment_id}") == (True, "ok")


@pytest.mark.parametrize(
    "ref, why",
    [
        (None, "no source_ref"),
        ("", "no source_ref"),
        ("the August deck", "source_ref is not kind:ref"),
        ("email:abc", "source_ref is not kind:ref"),
        ("email:999", "not in the store"),
        ("finding:1", "a finding cannot be the source of a finding"),
        ("topic_page:1", "a topic page is derived"),
        ("conversation:s1", "a session is derived, not a primary source"),
        ("spreadsheet:1", "unknown kind"),
    ],
)
def test_anything_else_is_kept_but_unsourced(store, ref, why):
    sourced, check = findings.check_source(store, ref)

    assert sourced is False
    assert check.startswith(why)


def test_a_session_note_is_derived_and_cannot_source_a_finding(store):
    note = f.email(
        store,
        subject="[Session note] /n/a.md",
        mailbox_name="External",
        source_class="session_note",
    )
    attachment_id = f.attachment(store, email_id=note)

    assert findings.check_source(store, f"document:{note}") == (False, "a session note is derived")
    assert findings.check_source(store, f"attachment:{attachment_id}") == (
        False,
        "a session note is derived",
    )


# ------------------------------------------------------------------- storing


def test_a_note_written_again_keeps_the_ids_of_unchanged_findings(store):
    email_id = f.email(store, subject="Okapi report")
    first = _block(
        {"claim": "Okapi share", "value": "70%", "source_ref": f"email:{email_id}"},
        {"claim": "Okapi count", "value": "12", "source_ref": "email:999"},
    )
    findings.sync_note_findings(store, "/n/a.md", "s1", "2026-10-10T10:00:00Z", first)
    ids = [r[0] for r in store.execute("SELECT id FROM findings ORDER BY ordinal")]

    second = _block(
        {"claim": "Okapi share", "value": "70%", "source_ref": f"email:{email_id}"},
        {"claim": "Okapi count", "value": "13", "source_ref": "email:999"},
    )
    stats = findings.sync_note_findings(store, "/n/a.md", "s1", "2026-10-11T10:00:00Z", second)

    rows = store.execute("SELECT id, value, sourced, source_check FROM findings ORDER BY ordinal")
    assert [tuple(r) for r in rows] == [
        (ids[0], "70%", 1, "ok"),
        (ids[1], "13", 0, "not in the store"),
    ]
    assert stats["changed"] == 1 and stats["unchanged"] == 1


def test_a_finding_whose_source_arrives_later_becomes_sourced(store):
    text = _block({"claim": "Okapi share", "source_ref": "email:1"})
    findings.sync_note_findings(store, "/n/a.md", "s1", "2026-10-10", text)
    assert store.execute("SELECT sourced FROM findings").fetchone()[0] == 0

    f.email(store, subject="Okapi report")
    findings.sync_note_findings(store, "/n/a.md", "s1", "2026-10-10", text)

    assert tuple(store.execute("SELECT sourced, source_check FROM findings").fetchone()) == (
        1,
        "ok",
    )


def test_a_shorter_block_removes_the_findings_it_dropped(store):
    findings.sync_note_findings(
        store, "/n/a.md", "s1", "2026-10-10", _block({"claim": "a"}, {"claim": "b"})
    )
    findings.sync_note_findings(store, "/n/a.md", "s1", "2026-10-11", "no block any more")

    assert store.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 0


def test_supersedes_names_the_finding_it_replaces(store):
    findings.sync_note_findings(
        store,
        "/n/old.md",
        "s1",
        "2026-10-01",
        _block({"claim": "Okapi digital share", "value": ">80%"}),
    )
    old_id = store.execute("SELECT id FROM findings").fetchone()[0]
    findings.sync_note_findings(
        store,
        "/n/new.md",
        "s2",
        "2026-10-15",
        _block(
            {
                "claim": "Okapi digital share",
                "value": "61%",
                "supersedes": f"finding:{old_id}",
            }
        ),
    )

    rows = {r["value"]: r for r in findings.findings_matching(store, words=["okapi"])}

    assert rows[">80%"]["superseded_by"] == f"finding:{rows['61%']['id']}"
    assert rows["61%"]["superseded_by"] is None
    assert [r["value"] for r in findings.findings_matching(store, words=["okapi"])] == [
        "61%",
        ">80%",
    ]


def test_supersedes_may_name_the_claim_it_replaces(store):
    findings.sync_note_findings(
        store, "/n/old.md", "s1", "2026-10-01", _block({"claim": "Okapi count", "value": "10"})
    )
    findings.sync_note_findings(
        store,
        "/n/new.md",
        "s2",
        "2026-10-15",
        _block({"claim": "Okapi count, revised", "value": "12", "supersedes": "okapi COUNT"}),
    )

    by_value = {r["value"]: r for r in findings.findings_matching(store, words=["okapi"])}

    assert by_value["10"]["superseded_by"] == f"finding:{by_value['12']['id']}"


def test_findings_match_a_topic_by_their_source_items_topics(store):
    tagged = f.email(store, subject="Report", topics=["okapi plan"])
    other = f.email(store, subject="Other", topics=["zebra"])
    findings.sync_note_findings(
        store,
        "/n/a.md",
        "s1",
        "2026-10-10",
        _block(
            {"claim": "Share held", "source_ref": f"email:{tagged}"},
            {"claim": "Share elsewhere", "source_ref": f"email:{other}"},
        ),
    )
    topic_id = store.execute("SELECT id FROM topics WHERE name = 'okapi plan'").fetchone()[0]

    rows = findings.findings_matching(store, topic_ids=[topic_id])

    assert [r["claim"] for r in rows] == ["Share held"]
    assert rows[0]["ref"] == f"finding:{rows[0]['id']}"
    assert rows[0]["kind"] == "finding"


# ------------------------------------------------------- through session notes


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


def _transcript(path: Path, *records) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def test_a_session_note_with_a_block_loads_its_findings(store, tmp_path):
    email_id = f.email(store, subject="Okapi report")
    note = _block(
        {
            "claim": "Okapi share",
            "value": "61%",
            "as_of": "2026-08",
            "source_ref": f"email:{email_id}",
        }
    )
    transcript = _transcript(
        tmp_path / "s1.jsonl", _use(1, "/work/okapi-findings.md", note), _result(1)
    )

    stats = ingest_session_notes(store, [transcript])

    row = store.execute(
        "SELECT note_path, session_id, written_at, claim, value, as_of, sourced FROM findings"
    ).fetchone()
    assert tuple(row) == (
        "/work/okapi-findings.md",
        "s1",
        "2026-10-15T10:00:01Z",
        "Okapi share",
        "61%",
        "2026-08",
        1,
    )
    assert stats["findings"] == 1


def test_a_note_read_again_unchanged_keeps_its_findings(store, tmp_path):
    note = _block({"claim": "Okapi share", "value": "61%"})
    transcript = _transcript(tmp_path / "s1.jsonl", _use(1, "/work/a.md", note), _result(1))
    ingest_session_notes(store, [transcript])
    first = store.execute("SELECT id, loaded_at FROM findings").fetchall()

    ingest_session_notes(store, [transcript])

    assert store.execute("SELECT id, loaded_at FROM findings").fetchall() == first
