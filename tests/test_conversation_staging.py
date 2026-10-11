"""Conversation staging: one file per session, pruned once it loads, no transcript read twice.

Every export wrote one batch of every session not yet loaded, and nothing removed a batch:
773 MB in 1,285 files on the producer by 2026-10-10, and three consecutive hourly batches
held 19 copies of 7 sessions. Each run's extraction and load parsed all of it, and every
extraction file besides. The export also parsed every transcript of the last week, 1.15 GB,
to skip the sessions already loaded.

A staged copy is now one file per session and version (its end and its turn count): the same
version exported again replaces its own file, and the copy an extraction read survives the
export of a newer one until it loads, as the loader pairs them. A load removes the copies
nothing can use any more and splits the batches written before into such files. The export
records each transcript it reads with its size and mtime, and reads one again only when it
changed, or when what it held is neither loaded nor staged.

Every transcript here is synthetic.
"""

import json
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from src import config
from src.export import conversation_export as ce
from src.extract import local
from src.store.loader import load_conversations
from src.store.schema import create_database, get_connection


def _conv(session_id, ended_at, turns):
    return {
        "session_id": session_id,
        "started_at": "2026-09-01T09:00:00Z",
        "ended_at": ended_at,
        "workspace": "/work/okapi",
        "project_name": "okapi",
        "turn_count": turns,
        "turns": [
            {"speaker": "user", "content": f"turn {n}", "timestamp": ended_at} for n in range(turns)
        ],
        "kind": "owner",
    }


@pytest.fixture
def data(tmp_path, monkeypatch):
    staging = tmp_path / "staging" / "conversations"
    extracted = tmp_path / "extracted" / "conversations"
    extracted.mkdir(parents=True)
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(ce, "CONVERSATION_STAGING_DIR", staging)
    monkeypatch.setattr(ce, "CLAUDE_CODE_PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(local, "CONV_STAGING_DIR", staging)
    monkeypatch.setattr(local, "CONV_EXTRACTED_DIR", extracted)
    monkeypatch.setattr(local, "CONV_STATE_FILE", tmp_path / "state" / "conv.json")
    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    db = tmp_path / "brain.db"
    conn = create_database(str(db))
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()
    return types.SimpleNamespace(
        root=tmp_path, staging=staging, extracted=extracted, db=db, projects=tmp_path / "projects"
    )


def _export(monkeypatch, *convs, loaded=None):
    """Export these conversations as parsed transcripts (no files, so nothing is recorded)."""
    by_name = {f"{c['session_id']}-{c['ended_at']}.jsonl": c for c in convs}
    monkeypatch.setattr(
        ce,
        "scan_conversation_files",
        lambda days=None, workspace_filter=None: [Path(name) for name in by_name],
    )
    monkeypatch.setattr(
        ce, "parse_conversation", lambda path: json.loads(json.dumps(by_name[path.name]))
    )
    return ce.export_conversations(loaded=loaded or {})


def _extracted(data, conv, summary):
    """An extraction as run_conversation_extraction writes it."""
    extraction = {
        "summary": summary,
        "topics": [],
        "transcript_ended_at": conv["ended_at"],
        "transcript_turn_count": conv["turn_count"],
    }
    (data.extracted / f"{conv['session_id']}.json").write_text(json.dumps(extraction))


def _files(data):
    return sorted(p.name for p in data.staging.glob("conversation-*.json"))


def _copies(data):
    """(session, end) of every staged copy, in staging order."""
    return [
        (conv["session_id"], conv["ended_at"])
        for _, conversations in ce.read_staged_conversations(data.staging)
        for conv in conversations
    ]


def _stored(data):
    conn = get_connection(str(data.db))
    rows = conn.execute(
        "SELECT session_id, ended_at, turn_count, summary FROM conversations ORDER BY session_id"
    ).fetchall()
    conn.close()
    return [tuple(r) for r in rows]


def test_each_session_is_staged_in_a_file_of_its_own(data, monkeypatch):
    a = _conv("a", "2026-09-01T10:00:00Z", 2)
    b = _conv("b", "2026-09-01T11:00:00Z", 3)

    result = _export(monkeypatch, a, b)

    assert result["exported"] == 2
    assert len(result["staged"]) == 2
    assert sorted(_copies(data)) == [("a", a["ended_at"]), ("b", b["ended_at"])]
    files = _files(data)
    assert len(files) == 2
    _export(monkeypatch, a, b)  # the same versions again
    assert _files(data) == files


def test_the_copy_an_extraction_read_stays_beside_a_newer_one_until_it_loads(data, monkeypatch):
    first = _conv("s", "2026-09-01T10:00:00Z", 2)
    grown = _conv("s", "2026-09-01T12:00:00Z", 4)
    _export(monkeypatch, first)
    _extracted(data, first, "first")
    _export(monkeypatch, grown)
    assert _copies(data) == [("s", first["ended_at"]), ("s", grown["ended_at"])]

    assert load_conversations(str(data.db)) == 1

    assert _stored(data) == [("s", first["ended_at"], 2, "first")]
    assert _copies(data) == [("s", grown["ended_at"])]
    _extracted(data, grown, "grown")
    assert load_conversations(str(data.db)) == 1
    assert _stored(data) == [("s", grown["ended_at"], 4, "grown")]
    assert _files(data) == []


def test_a_load_removes_what_it_loaded_and_what_nothing_can_use(data, monkeypatch):
    done = _conv("done", "2026-09-01T10:00:00Z", 2)
    waiting = _conv("waiting", "2026-09-01T10:00:00Z", 2)
    went_on = _conv("waiting", "2026-09-01T11:00:00Z", 3)
    newest = _conv("waiting", "2026-09-01T12:00:00Z", 4)
    _export(monkeypatch, done, waiting, went_on, newest)
    _extracted(data, done, "done")

    assert load_conversations(str(data.db)) == 1

    # The two older copies of a session nothing has extracted can never load: the
    # extraction takes the newest.
    assert _copies(data) == [("waiting", newest["ended_at"])]


def test_the_batches_written_before_are_split_and_removed(data):
    data.staging.mkdir(parents=True)
    older_t = _conv("t", "2026-09-01T09:00:00Z", 2)
    s = _conv("s", "2026-09-01T10:00:00Z", 2)
    t = _conv("t", "2026-09-01T11:00:00Z", 3)
    for stamp, conversations in (("20260901_090000", [older_t]), ("20260901_110000", [s, t])):
        batch = data.staging / f"conversation-batch-{stamp}.json"
        batch.write_text(json.dumps({"conversations": conversations}))
    _extracted(data, s, "s")

    assert load_conversations(str(data.db)) == 1

    assert _copies(data) == [("t", t["ended_at"])]
    assert not list(data.staging.glob("conversation-batch-*.json"))


def test_a_staged_file_a_load_removed_meanwhile_is_passed_over(data, monkeypatch, capsys):
    data.staging.mkdir(parents=True)
    kept = data.staging / "conversation-batch-1.json"
    kept.write_text(json.dumps({"conversations": [_conv("a", "2026-09-01T10:00:00Z", 2)]}))
    removed = data.staging / "conversation-batch-2.json"
    monkeypatch.setattr(ce, "staged_files", lambda staging_dir: [kept, removed])

    assert [c["session_id"] for c in local.collect_conversations()] == ["a"]
    assert not (data.staging / "quarantine").exists()


# ----------------------------------------------------------- transcripts read once


def _transcript(projects, session_id, exchanges, minute=0):
    """A settled transcript of `exchanges` typed prompts and replies."""
    path = projects / "-work-okapi" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for n in range(exchanges):
        ts = f"2026-09-01T10:{minute + n:02d}:00Z"
        base = {"sessionId": session_id, "cwd": "/work/okapi", "timestamp": ts}
        lines.append(
            {
                **base,
                "type": "user",
                "origin": {"kind": "human"},
                "promptSource": "typed",
                "message": {"role": "user", "content": f"step {n} of the okapi plan"},
            }
        )
        lines.append(
            {
                **base,
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": f"did {n}"}]},
            }
        )
    with open(path, "a", encoding="utf-8") as f:
        f.write("".join(json.dumps(line) + "\n" for line in lines))
    return path


def _counting_parses(monkeypatch):
    parsed: list[str] = []
    real = ce.parse_conversation

    def parse(path):
        parsed.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(ce, "parse_conversation", parse)
    return parsed


def _loaded(conv):
    return {conv["session_id"]: (conv["ended_at"], conv["turn_count"])}


def test_an_unchanged_transcript_is_read_once(data, monkeypatch):
    _transcript(data.projects, "s", 2)
    parsed = _counting_parses(monkeypatch)

    first = ce.export_conversations()
    (staged,) = [json.loads(Path(p).read_text())["conversations"][0] for p in first["staged"]]
    again = ce.export_conversations()  # staged and waiting for its load
    for path in first["staged"]:
        Path(path).unlink()  # as the load that stores it does
    after_load = ce.export_conversations(loaded=_loaded(staged))

    assert parsed == ["s.jsonl"]
    assert (first["exported"], again["exported"], again["pending"]) == (1, 0, 1)
    assert (after_load["exported"], after_load["skipped"]) == (0, 1)


def test_a_transcript_that_went_on_is_read_again(data, monkeypatch):
    path = _transcript(data.projects, "s", 2)
    first = ce.export_conversations()
    (staged,) = [json.loads(Path(p).read_text())["conversations"][0] for p in first["staged"]]
    _transcript(data.projects, "s", 1, minute=30)
    parsed = _counting_parses(monkeypatch)

    result = ce.export_conversations(loaded=_loaded(staged))

    assert parsed == [path.name]
    assert result["exported"] == 1


def test_a_session_the_store_no_longer_holds_is_read_and_staged_again(data, monkeypatch):
    _transcript(data.projects, "s", 2)
    first = ce.export_conversations()
    for path in first["staged"]:
        Path(path).unlink()
    parsed = _counting_parses(monkeypatch)

    result = ce.export_conversations(loaded={})  # a restore from before it loaded

    assert parsed == ["s.jsonl"]
    assert result["exported"] == 1


def test_a_change_to_the_exporter_reads_every_transcript_again(data, monkeypatch):
    _transcript(data.projects, "s", 2)
    ce.export_conversations()
    monkeypatch.setattr(ce, "_code_version", lambda: "another")
    parsed = _counting_parses(monkeypatch)

    ce.export_conversations()

    assert parsed == ["s.jsonl"]


def test_too_short_and_agent_transcripts_are_not_read_again(data, monkeypatch):
    _transcript(data.projects, "short", 1)
    agent = _transcript(data.projects, "agent", 2)
    records = [json.loads(line) for line in agent.read_text().splitlines()]
    agent.write_text("".join(json.dumps({**r, "teamName": "t"}) + "\n" for r in records))
    ce.export_conversations()
    parsed = _counting_parses(monkeypatch)

    left_out = ce.export_conversations()
    assert parsed == []
    assert (left_out["skipped"], left_out["agents"]) == (1, 1)

    with_agents = ce.export_conversations(include_agents=True)
    assert parsed == ["agent.jsonl"]
    assert with_agents["exported"] == 1


# ------------------------------------------------------------------- two sync runs


def _sync(data):
    from src import cli, llm_deadline

    args = types.SimpleNamespace(
        db=data.db, limit=None, engine="claude", workers=1, skip_export=True
    )
    ok = {"extracted": 0, "failed": 0, "quota_paused": False}

    def extract(conv):
        return (
            conv["session_id"],
            {"summary": f"about {conv['session_id']}", "topics": []},
            False,
            None,
        )

    with (
        patch("src.pipeline.sync.DATA_ROOT", data.root),
        patch.object(llm_deadline, "_detect_systemd_unit", return_value=None),
        patch("src.extract.local.run_extraction", return_value=ok),
        patch("src.store.loader.load_extractions", return_value=0),
        patch("src.extract.attachment_pipeline.run_phase1", return_value={"processed": 0}),
        patch("src.extract.image_pipeline.run_backfill", return_value={}),
        patch("src.store.embeddings.build_index"),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(local, "extract_conversation_inline", side_effect=extract),
    ):
        assert cli.cmd_sync(args) == 0


def test_a_second_sync_reads_none_of_what_the_first_loaded(data, monkeypatch):
    _transcript(data.projects, "s1", 2)
    _transcript(data.projects, "s2", 3)

    _sync(data)

    assert [row[0] for row in _stored(data)] == ["s1", "s2"]
    assert _files(data) == []

    staged_reads: list[Path] = []
    real_read = ce.read_staged_conversations

    def counting(staging_dir):
        for path, conversations in real_read(staging_dir):
            staged_reads.append(path)
            yield path, conversations

    monkeypatch.setattr(ce, "read_staged_conversations", counting)
    parsed = _counting_parses(monkeypatch)

    _sync(data)

    assert staged_reads == []
    assert parsed == []
    assert [row[0] for row in _stored(data)] == ["s1", "s2"]


def test_sync_loads_what_waits_in_staging_when_the_export_staged_nothing_new(data):
    from src import cli, llm_deadline

    args = types.SimpleNamespace(
        db=data.db, limit=None, engine="claude", workers=1, skip_export=True
    )
    ok = {"extracted": 0, "failed": 0, "quota_paused": False}
    waiting = {"exported": 0, "skipped": 0, "agents": 0, "errors": 0, "pending": 1, "staged": []}
    with (
        patch("src.pipeline.sync.DATA_ROOT", data.root),
        patch.object(llm_deadline, "_detect_systemd_unit", return_value=None),
        patch("src.extract.local.run_extraction", return_value=ok),
        patch("src.store.loader.load_extractions", return_value=0),
        patch("src.extract.attachment_pipeline.run_phase1", return_value={"processed": 0}),
        patch("src.extract.image_pipeline.run_backfill", return_value={}),
        patch("src.export.conversation_export.export_conversations", return_value=waiting),
        patch("src.extract.local.run_conversation_extraction") as extraction,
        patch("src.store.loader.load_conversations", return_value=1) as load,
        patch("src.store.embeddings.build_index") as embed,
    ):
        assert cli.cmd_sync(args) == 0

    extraction.assert_called_once()
    load.assert_called_once()
    embed.assert_called_once()
