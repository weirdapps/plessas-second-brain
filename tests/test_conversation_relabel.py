"""The conversations stored before the exporter read provenance are relabelled once.

They hold every user-role turn as the owner's. relabel-conversations demotes the
turns the owner did not write to speaker 'agent': from the session's transcript
where the producer still holds it (the record's own fields, the session's agent
name), else from how the stored turn reads. A conversation left with no owner
turn is an agent session from then on. Turns are relabelled, never re-split, so
nothing the owner typed loses his label.

Every transcript and turn here is synthetic.
"""

import json
import sys

import pytest

from src.export.conversation_relabel import relabel_conversations
from src.store.schema import create_database
from tests.patching import unset_env

TS = "2026-10-01T10:00:00Z"


def _user(text, **fields):
    return {"type": "user", "sessionId": "s", "timestamp": TS, "message": {"role": "user", "content": text}, **fields}  # fmt: skip


def _assistant(text, **fields):
    content = [{"type": "text", "text": text}]
    return {"type": "assistant", "sessionId": "s", "timestamp": TS, "message": {"role": "assistant", "content": content}, **fields}  # fmt: skip


def _typed(text):
    return _user(text, origin={"kind": "human"}, promptSource="typed")


TASK = "<task-notification><summary>okapi done</summary></task-notification>"
SKILL = "Base directory for this skill: /skills/okapi\n\nPlan the okapi."


@pytest.fixture
def store(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    (root / "-work-okapi").mkdir(parents=True)
    return root


def _transcript(projects, session, *records):
    path = projects / "-work-okapi" / f"{session}.jsonl"
    path.write_text("\n".join(json.dumps({**r, "sessionId": session}) for r in records) + "\n")


def _stored(conn, session, *turns, preferences=()):
    """A conversation as the old exporter stored it: turns of (speaker, content)."""
    cid = conn.execute(
        "INSERT INTO conversations (session_id, started_at, turn_count, created_at)"
        " VALUES (?, ?, ?, ?)",
        (session, TS, len(turns), TS),
    ).lastrowid
    for i, (speaker, content) in enumerate(turns):
        conn.execute(
            "INSERT INTO conversation_turns (conversation_id, turn_index, timestamp, speaker,"
            " content) VALUES (?, ?, ?, ?, ?)",
            (cid, i, TS, speaker, content),
        )
    last = conn.execute(
        "SELECT MAX(id) FROM conversation_turns WHERE conversation_id = ?", (cid,)
    ).fetchone()[0]
    for pref in preferences:
        conn.execute(
            "INSERT INTO key_facts (fact, conversation_turn_id) VALUES (?, ?)",
            (f"[PREFERENCE] {pref}", last),
        )
    conn.commit()
    return cid


def _speakers(conn, cid):
    rows = conn.execute(
        "SELECT speaker FROM conversation_turns WHERE conversation_id = ? ORDER BY turn_index",
        (cid,),
    )
    return [r[0] for r in rows]


def test_the_transcript_says_which_turns_the_owner_did_not_write(store, projects):
    _transcript(
        projects,
        "s",
        _typed("plan the okapi"),
        _assistant("ok"),
        _user(TASK, origin={"kind": "task-notification"}, promptSource="system"),
        _assistant("noted"),
        _user("Summarise it", promptSource="sdk"),
        _assistant("summary"),
        _typed("thanks"),
    )
    cid = _stored(
        store,
        "s",
        ("user", "plan the okapi"),
        ("assistant", "ok"),
        ("user", TASK),
        ("assistant", "noted"),
        ("user", "Summarise it"),
        ("assistant", "summary"),
        ("user", "thanks"),
    )

    counts = relabel_conversations(store, projects)

    assert _speakers(store, cid) == ["user", "assistant", "agent", "assistant", "agent", "assistant", "user"]  # fmt: skip
    assert counts["by_transcript"] == 1
    assert counts["turns_relabelled"] == 2


def test_a_turn_holding_anything_the_owner_typed_keeps_his_label(store, projects):
    """The old exporter joined adjacent user-role records into one turn."""
    _transcript(
        projects,
        "s",
        _typed("/okapi plan it"),
        _user(SKILL, isMeta=True),
        _assistant("planned"),
        _typed("thanks"),
    )
    cid = _stored(
        store,
        "s",
        ("user", "/okapi plan it\n" + SKILL),
        ("assistant", "planned"),
        ("user", "thanks"),
    )

    relabel_conversations(store, projects)

    assert _speakers(store, cid) == ["user", "assistant", "user"]


def test_a_teammates_session_becomes_an_agent_session(store, projects):
    _transcript(
        projects,
        "t",
        _user("<teammate-message>plan the okapi</teammate-message>", teamName="team"),
        _assistant("ok", teamName="team"),
        _user("and the budget", teamName="team"),
        _assistant("ok", teamName="team"),
    )
    cid = _stored(
        store,
        "t",
        ("user", "<teammate-message>plan the okapi</teammate-message>"),
        ("assistant", "ok"),
        ("user", "and the budget"),
        ("assistant", "ok"),
        preferences=["Verifier must be read-only"],
    )

    counts = relabel_conversations(store, projects)

    assert _speakers(store, cid) == ["agent", "assistant", "agent", "assistant"]
    assert (counts["agent_sessions_before"], counts["agent_sessions_after"]) == (0, 1)
    assert counts["agent_preferences"] == 1


def test_without_a_transcript_the_turns_text_decides(store, projects):
    cid = _stored(
        store,
        "gone",
        ("user", "<teammate-message>plan the okapi</teammate-message>"),
        ("assistant", "ok"),
        ("user", TASK + "\nyes, go ahead"),
        ("assistant", "going"),
        ("user", SKILL),
        ("assistant", "planned"),
        ("user", "Another Claude session sent a message:\nstatus?"),
    )

    counts = relabel_conversations(store, projects)

    assert _speakers(store, cid) == ["agent", "assistant", "user", "assistant", "agent", "assistant", "agent"]  # fmt: skip
    assert (counts["by_text"], counts["misaligned"]) == (1, 0)


def test_a_stored_session_the_re_auth_nudge_alone_opened_becomes_an_agent_session(store, projects):
    nudge = "gcloud auth refreshed. If a task was genuinely in progress, continue it from where"
    cid = _stored(
        store,
        "gone",
        ("agent", "<local-command-caveat>Caveat</local-command-caveat>"),
        ("user", nudge + " it stopped."),
        ("assistant", "DONE!"),
    )

    counts = relabel_conversations(store, projects)

    assert _speakers(store, cid) == ["agent", "agent", "assistant"]
    assert counts["agent_sessions_after"] == 1


def test_a_transcript_that_is_not_the_stored_copy_falls_back_to_the_text(store, projects):
    _transcript(projects, "s", _user("something else entirely"), _assistant("ok"))
    cid = _stored(store, "s", ("user", "plan the okapi"), ("assistant", "ok"), ("user", TASK))

    counts = relabel_conversations(store, projects)

    assert _speakers(store, cid) == ["user", "assistant", "agent"]
    assert (counts["by_text"], counts["misaligned"]) == (1, 1)


def test_a_dry_run_counts_and_writes_nothing(store, projects):
    cid = _stored(store, "gone", ("user", TASK), ("assistant", "ok"), ("user", SKILL))

    counts = relabel_conversations(store, projects, dry_run=True)

    assert counts["turns_relabelled"] == 2
    assert counts["agent_sessions_after"] == 1
    assert _speakers(store, cid) == ["user", "assistant", "user"]


def test_a_second_run_changes_nothing(store, projects):
    _stored(store, "gone", ("user", TASK), ("assistant", "ok"), ("user", "plan the okapi"))
    relabel_conversations(store, projects)

    counts = relabel_conversations(store, projects)

    assert counts["turns_relabelled"] == 0
    assert counts["agent_sessions_before"] == counts["agent_sessions_after"] == 0


def test_the_command_refuses_a_replica_even_for_a_dry_run(tmp_path, monkeypatch, capsys):
    from src import cli, config

    stamp = tmp_path / "db-pull.stamp"
    stamp.write_text("x")
    monkeypatch.setattr(config, "REPLICA_STAMP", stamp)
    unset_env(monkeypatch, "BRAIN_ROLE")
    monkeypatch.setattr(cli, "cmd_relabel_conversations", lambda args: pytest.fail("ran"))
    argv = ["brain", "--db", str(tmp_path / "b.db"), "relabel-conversations", "--dry-run"]
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert "Refusing" in capsys.readouterr().err


def test_the_command_reports_its_counts(tmp_path, monkeypatch, capsys):
    from src import cli, config

    conn = create_database(str(tmp_path / "b.db"))
    _stored(conn, "gone", ("user", TASK), ("assistant", "ok"), ("user", SKILL))
    conn.close()
    monkeypatch.setattr(config, "CLAUDE_CODE_PROJECTS_DIR", tmp_path / "none")
    monkeypatch.setenv("BRAIN_ROLE", "producer")
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    argv = ["brain", "--db", str(tmp_path / "b.db"), "relabel-conversations", "--dry-run"]
    monkeypatch.setattr(sys, "argv", argv)

    cli.main()

    out = capsys.readouterr().out
    assert "2 turns" in out and "dry run" in out
