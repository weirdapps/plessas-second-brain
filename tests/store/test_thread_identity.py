"""Thread identity (issue 179), the gated step `thread_identity`.

Mail with no Outlook ConversationId was threaded by the loader's fallback: the hash of the
first References id, else of In-Reply-To, else of the subject, unbounded in time. The step
re-roots a legacy reply on the stored message it references (by Message-ID), cuts a
subject-only thread at every silence over 14 days, and records each email's old id in
thread_id_map so `revert` puts it back. No row is deleted. On a store without the step the
loader threads exactly as before. Every value is invented.
"""

import sqlite3

import pytest

from src.store import thread_identity as ti
from src.store.loader import conversation_id_of, load_single_email
from src.store.schema import (
    apply_gated,
    create_database,
    pending_gated,
    subject_to_conversation_id,
)
from tests import factories as f

ROOT_MID = "<root@corp.example>"


def _outlook(n: int) -> str:
    """An invented Outlook ConversationId."""
    return f"outlook-thread-{n}"


def _ref_hash(message_id: str) -> str:
    return conversation_id_of({"references": message_id})


def _old_store(tmp_path) -> sqlite3.Connection:
    return create_database(str(tmp_path / "brain.db"), gated=False)


def _thread(conn, email_id: int) -> str:
    return conn.execute("SELECT conversation_id FROM emails WHERE id = ?", (email_id,)).fetchone()[
        0
    ]


def _apply(conn) -> None:
    apply_gated(conn)
    conn.commit()


# --- the step on stored mail -----------------------------------------------------------------


def test_a_legacy_reply_joins_the_outlook_thread_of_the_root_it_references(tmp_path):
    db = _old_store(tmp_path)
    root = f.email(
        db,
        subject="Okapi budget",
        conversation_id=_outlook(1),
        internet_message_id=ROOT_MID,
        date_received="2025-04-01T09:00:00Z",
    )
    reply = f.email(
        db,
        subject="RE: Okapi budget",
        conversation_id=_ref_hash(ROOT_MID),
        references=ROOT_MID,
        in_reply_to=ROOT_MID,
        date_received="2025-04-02T09:00:00Z",
    )

    _apply(db)

    assert _thread(db, reply) == _thread(db, root) == _outlook(1)
    row = db.execute("SELECT old_id, new_id, rule FROM thread_id_map WHERE email_id = ?", (reply,))
    assert tuple(row.fetchone()) == (_ref_hash(ROOT_MID), _outlook(1), "reference")


def test_a_reply_takes_the_earliest_referenced_message_held(tmp_path):
    """References lists the root first; when the root is not held, the next one is."""
    db = _old_store(tmp_path)
    middle = f.email(
        db,
        subject="RE: Okapi budget",
        conversation_id=_outlook(2),
        internet_message_id="<middle@corp.example>",
    )
    reply = f.email(
        db,
        subject="RE: RE: Okapi budget",
        conversation_id=_ref_hash("<absent@corp.example>"),
        references="<absent@corp.example> <middle@corp.example>",
    )

    _apply(db)

    assert _thread(db, reply) == _thread(db, middle)


def test_a_reply_whose_references_are_all_absent_keeps_its_id(tmp_path):
    db = _old_store(tmp_path)
    old = _ref_hash("<absent@corp.example>")
    reply = f.email(
        db, subject="RE: Okapi", conversation_id=old, references="<absent@corp.example>"
    )

    _apply(db)

    assert _thread(db, reply) == old
    assert db.execute("SELECT rule FROM thread_id_map WHERE email_id = ?", (reply,)).fetchone()[
        0
    ] == ("kept")


def test_a_chain_of_legacy_replies_follows_its_root_to_the_end(tmp_path):
    db = _old_store(tmp_path)
    root = f.email(
        db,
        subject="Okapi",
        conversation_id=_outlook(3),
        internet_message_id=ROOT_MID,
        date_received="2025-04-01T09:00:00Z",
    )
    first = f.email(
        db,
        subject="RE: Okapi",
        conversation_id=conversation_id_of({"in_reply_to": ROOT_MID}),
        in_reply_to=ROOT_MID,
        internet_message_id="<r1@corp.example>",
        date_received="2025-04-02T09:00:00Z",
    )
    second = f.email(
        db,
        subject="RE: RE: Okapi",
        conversation_id=conversation_id_of({"in_reply_to": "<r1@corp.example>"}),
        in_reply_to="<r1@corp.example>",
        date_received="2025-04-03T09:00:00Z",
    )

    _apply(db)

    assert _thread(db, first) == _thread(db, second) == _thread(db, root)


def test_a_subject_thread_is_cut_at_a_silence_over_fourteen_days(tmp_path):
    db = _old_store(tmp_path)
    base = subject_to_conversation_id("Weekly okapi report")
    dates = ["2025-01-01", "2025-01-08", "2025-01-22", "2025-02-20", "2025-02-25", "2025-06-01"]
    ids = [
        f.email(
            db,
            subject="Weekly okapi report",
            conversation_id=base,
            date_received=f"{d}T08:00:00Z",
        )
        for d in dates
    ]

    _apply(db)

    threads = [_thread(db, i) for i in ids]
    # 14 days exactly (01-08 -> 01-22) is no cut; 29 days and 96 days are.
    assert threads[0] == threads[1] == threads[2] == base
    assert threads[3] == threads[4] != base
    assert threads[5] not in (base, threads[3])
    assert threads[3] == ti.segment_id(base, "2025-02-20T08:00:00Z")
    rules = dict(db.execute("SELECT email_id, rule FROM thread_id_map").fetchall())
    assert [rules[i] for i in ids] == ["kept"] * 3 + ["subject_gap"] * 3


def test_a_reply_to_a_cut_subject_thread_joins_its_segment(tmp_path):
    db = _old_store(tmp_path)
    base = subject_to_conversation_id("Okapi plan")
    f.email(db, subject="Okapi plan", conversation_id=base, date_received="2025-01-01T08:00:00Z")
    late = f.email(
        db,
        subject="Okapi plan",
        conversation_id=base,
        internet_message_id=ROOT_MID,
        date_received="2025-03-01T08:00:00Z",
    )
    reply = f.email(
        db,
        subject="RE: Okapi plan",
        conversation_id=_ref_hash(ROOT_MID),
        references=ROOT_MID,
        date_received="2025-03-02T08:00:00Z",
    )

    _apply(db)

    assert _thread(db, reply) == _thread(db, late) != base


def test_outlook_news_documents_and_blank_subjects_are_left_alone(tmp_path):
    db = _old_store(tmp_path)
    blank = subject_to_conversation_id("")
    kept = [
        f.email(db, subject="Okapi", conversation_id=_outlook(4)),
        f.email(
            db,
            subject="Okapi news",
            conversation_id="news:monitor:2025-01-01",
            mailbox_name="News",
            source_class="news",
        ),
        f.email(db, subject="Okapi doc", conversation_id=None, source_class="document"),
        f.email(db, subject="", conversation_id=blank, date_received="2025-01-01T08:00:00Z"),
        f.email(db, subject="", conversation_id=blank, date_received="2025-06-01T08:00:00Z"),
    ]
    before = [_thread(db, i) for i in kept]

    _apply(db)

    assert [_thread(db, i) for i in kept] == before
    mapped = {r[0] for r in db.execute("SELECT email_id FROM thread_id_map")}
    assert mapped.isdisjoint(kept)


def test_no_row_is_deleted_and_revert_puts_every_id_back(tmp_path):
    db = _old_store(tmp_path)
    base = subject_to_conversation_id("Okapi plan")
    for d in ("2025-01-01", "2025-03-01", "2025-06-01"):
        f.email(db, subject="Okapi plan", conversation_id=base, date_received=f"{d}T08:00:00Z")
    f.email(db, subject="Okapi", conversation_id=_outlook(5), internet_message_id=ROOT_MID)
    f.email(db, subject="RE: Okapi", conversation_id=_ref_hash(ROOT_MID), references=ROOT_MID)
    before = db.execute("SELECT id, conversation_id FROM emails ORDER BY id").fetchall()

    _apply(db)
    assert db.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == len(before)
    assert db.execute("SELECT id, conversation_id FROM emails ORDER BY id").fetchall() != before

    assert ti.revert(db) == 3
    db.commit()

    assert db.execute("SELECT id, conversation_id FROM emails ORDER BY id").fetchall() == before
    assert "thread_identity" in pending_gated(db)
    # The map stays, as the record of what was done.
    assert db.execute("SELECT COUNT(*) FROM thread_id_map").fetchone()[0] == 4


def test_revert_on_a_store_without_the_step_changes_nothing(tmp_path):
    db = _old_store(tmp_path)
    f.email(db, subject="Okapi")
    assert ti.revert(db) == 0


def test_counts_show_threads_largest_and_rerooted(tmp_path):
    db = _old_store(tmp_path)
    base = subject_to_conversation_id("Okapi plan")
    for d in ("2025-01-01", "2025-01-02", "2025-03-01"):
        f.email(db, subject="Okapi plan", conversation_id=base, date_received=f"{d}T08:00:00Z")
    f.email(db, subject="Okapi", conversation_id=_outlook(6), internet_message_id=ROOT_MID)
    f.email(db, subject="RE: Okapi", conversation_id=_ref_hash(ROOT_MID), references=ROOT_MID)
    before = ti.counts(db)

    _apply(db)
    after = ti.counts(db)

    assert before["email threads"] == 3 and after["email threads"] == 3
    assert before["largest email thread"] == 3 and after["largest email thread"] == 2
    assert after["thread_id_map reference"] == 1
    assert after["thread_id_map subject_gap"] == 1


# --- the loader, after the step and before it ----------------------------------------------


def _mail(**fields) -> dict:
    return {
        "message_id": fields.pop("message_id"),
        "subject": "Okapi plan",
        "sender": {"name": "Alice", "address": "alice@corp.example"},
        "mailbox_name": "Inbox",
        "content": "hello",
        **fields,
    }


@pytest.mark.parametrize("gated", [False, True], ids=["before", "after"])
def test_the_loader_threads_new_legacy_mail_by_the_step_only_once_applied(tmp_path, gated):
    db = create_database(str(tmp_path / "brain.db"), gated=gated)
    assert ("thread_identity" in pending_gated(db)) is not gated
    load_single_email(
        db,
        _mail(
            message_id="m1",
            conversation_id=_outlook(7),
            internet_message_id=ROOT_MID,
            date_received="2025-01-01T08:00:00Z",
        ),
        {"summary": "s"},
    )
    load_single_email(
        db,
        _mail(message_id="m2", references=ROOT_MID, date_received="2025-01-02T08:00:00Z"),
        {"summary": "s"},
    )
    load_single_email(
        db, _mail(message_id="m3", date_received="2025-01-03T08:00:00Z"), {"summary": "s"}
    )
    load_single_email(
        db, _mail(message_id="m4", date_received="2025-03-01T08:00:00Z"), {"summary": "s"}
    )
    load_single_email(
        db, _mail(message_id="m5", date_received="2025-03-05T08:00:00Z"), {"summary": "s"}
    )
    ids = dict(db.execute("SELECT message_id, conversation_id FROM emails").fetchall())
    base = subject_to_conversation_id("Okapi plan")
    if not gated:
        assert ids["m2"] == _ref_hash(ROOT_MID)
        assert ids["m3"] == ids["m4"] == ids["m5"] == base
        assert "thread_id_map" not in {
            r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    else:
        assert ids["m2"] == _outlook(7)
        assert ids["m3"] == base
        assert ids["m4"] == ids["m5"] == ti.segment_id(base, "2025-03-01T08:00:00Z")
        assert db.execute("SELECT COUNT(*) FROM thread_id_map").fetchone()[0] == 4


def test_the_thread_state_of_new_mail_uses_the_stored_thread(tmp_path):
    """thread_state compares the stored id with the new email's; with the step applied it is
    the id the loader will give, so the reply sees its root's items."""
    from src.extract import thread_state

    db = create_database(str(tmp_path / "brain.db"), gated=True)
    load_single_email(
        db,
        _mail(
            message_id="m1",
            conversation_id=_outlook(8),
            internet_message_id=ROOT_MID,
            date_received="2025-01-01T08:00:00Z",
        ),
        {"summary": "s"},
    )
    reply = _mail(message_id="m2", references=ROOT_MID, date_received="2025-01-02T08:00:00Z")
    state = thread_state.thread_state(db, reply)
    assert state is not None and state["conversation_id"] == _outlook(8)


# --- the commands ----------------------------------------------------------------------------


@pytest.fixture
def legacy_path(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ROLE", "producer")
    path = tmp_path / "old.db"
    db = create_database(str(path), gated=False)
    base = subject_to_conversation_id("Okapi plan")
    for d in ("2025-01-01", "2025-03-01"):
        f.email(db, subject="Okapi plan", conversation_id=base, date_received=f"{d}T08:00:00Z")
    db.close()
    return path


def test_the_gated_dry_run_shows_the_thread_diff(legacy_path, monkeypatch, capsys):
    from tests.brain_cli import brain

    code, out, _ = brain(
        monkeypatch,
        capsys,
        "--db",
        str(legacy_path),
        "maint",
        "migrate",
        "--apply-gated",
        "--dry-run",
    )
    assert code == 0 and "thread_identity" in out
    assert "email threads: 1 -> 2  (+1)" in out
    assert "largest email thread: 2 -> 1  (-1)" in out
    db = sqlite3.connect(legacy_path)
    assert db.execute("SELECT COUNT(DISTINCT conversation_id) FROM emails").fetchone()[0] == 1
    db.close()


def test_revert_and_rollups_commands(legacy_path, tmp_path, monkeypatch, capsys):
    from tests.brain_cli import brain

    db_arg = ("--db", str(legacy_path))
    code, out, _ = brain(monkeypatch, capsys, *db_arg, "maint", "thread-rollups")
    assert code == 1 and "apply-gated" in out
    code, _, _ = brain(
        monkeypatch,
        capsys,
        *db_arg,
        "maint",
        "migrate",
        "--apply-gated",
        "--backup",
        str(tmp_path / "b1.db"),
    )
    assert code == 0

    code, out, _ = brain(monkeypatch, capsys, *db_arg, "maint", "thread-rollups")
    assert code == 0 and "2 would be written" in out
    code, out, _ = brain(monkeypatch, capsys, *db_arg, "maint", "thread-rollups", "--apply")
    assert code == 0 and "2 written" in out

    code, out, _ = brain(monkeypatch, capsys, *db_arg, "maint", "thread-identity-revert")
    assert code == 0 and "dry run, 1 emails would go back" in out
    code, _, err = brain(monkeypatch, capsys, *db_arg, "maint", "thread-identity-revert", "--apply")
    assert code == 2 and "--backup" in err
    code, out, _ = brain(
        monkeypatch,
        capsys,
        *db_arg,
        "maint",
        "thread-identity-revert",
        "--apply",
        "--backup",
        str(tmp_path / "b2.db"),
    )
    assert code == 0 and "1 emails back" in out
    db = sqlite3.connect(legacy_path)
    assert db.execute("SELECT COUNT(DISTINCT conversation_id) FROM emails").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM thread_rollups").fetchone()[0] == 0
    db.close()


def test_a_replica_never_reverts(legacy_path, monkeypatch, capsys):
    from tests.brain_cli import brain

    monkeypatch.setenv("BRAIN_ROLE", "replica")
    code, _, _ = brain(
        monkeypatch, capsys, "--db", str(legacy_path), "maint", "thread-identity-revert"
    )
    assert code == 2
