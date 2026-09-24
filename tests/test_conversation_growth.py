"""A Claude Code session that goes on after it was loaded is ingested again.

Export skipped every session already in the store, extraction every session it
had extracted, and the loader every session it held, so the turns of a session
resumed after its first ingest were never read: 9% of sessions have a gap of
over two hours, longer than the settle time that holds a live one back.

A session went on when it ends later AND holds more turns: a later end alone
comes from trailing events that add no conversation (6 of 78 sessions in a week
on the producer), and more turns alone would follow any change in how the
transcript parser counts them.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.extract import local
from src.store.loader import load_single_conversation
from src.store.schema import create_database


def _transcript(session_id, ended_at, turns):
    return {
        "session_id": session_id,
        "started_at": "2026-09-01T09:00:00Z",
        "ended_at": ended_at,
        "workspace": "w",
        "project_name": "p",
        "turn_count": len(turns),
        "turns": [{"speaker": s, "content": c, "timestamp": ended_at} for s, c in turns],
    }


def test_a_loaded_conversation_that_went_on_is_staged_again(tmp_path, monkeypatch):
    from src.export import conversation_export as ce

    transcripts = {
        "grew.jsonl": _transcript("grew", "2026-09-01T12:00:00Z", [("user", "a"), ("user", "b")]),
        "same.jsonl": _transcript("same", "2026-09-01T10:00:00Z", [("user", "a")]),
        "later.jsonl": _transcript("later", "2026-09-01T12:00:00Z", [("user", "a")]),
        "new.jsonl": _transcript("new", "2026-09-01T11:00:00Z", [("user", "a")]),
    }
    monkeypatch.setattr(ce, "CONVERSATION_STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(
        ce,
        "scan_conversation_files",
        lambda days=None, workspace_filter=None: list(map(Path, transcripts)),
    )
    monkeypatch.setattr(ce, "parse_conversation", lambda path: dict(transcripts[path.name]))

    loaded_at_ten = ("2026-09-01T10:00:00Z", 1)  # (ended_at, turns) as loaded
    result = ce.export_conversations(
        loaded={"grew": loaded_at_ten, "same": loaded_at_ten, "later": loaded_at_ten}
    )

    staged = json.loads(Path(result["batch_file"]).read_text())["conversations"]
    by_id = {c["session_id"]: c for c in staged}
    assert set(by_id) == {"grew", "new"}
    assert by_id["grew"]["regrown_from"] == "2026-09-01T10:00:00Z"  # what was loaded
    assert "regrown_from" not in by_id["new"]
    assert result["skipped"] == 2  # "later" ends later with no new turn


@pytest.fixture
def extraction_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(local, "CONV_EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(local, "CONV_STATE_FILE", tmp_path / "state" / "conv.json")
    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    local.CONV_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)


def _extract(staged):
    with (
        patch.object(local, "collect_conversations", return_value=staged),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(
            local,
            "extract_conversation_inline",
            side_effect=lambda conv: (conv["session_id"], {"summary": "x"}, False, None),
        ) as ex,
    ):
        local.run_conversation_extraction()
    return [call.args[0]["session_id"] for call in ex.call_args_list]


def test_a_conversation_that_went_on_is_extracted_again_once(extraction_paths):
    """A session extracted before it went on is extracted again, once: the copy it
    was extracted from is kept, and only a copy that went on past it (a later end
    and more turns) brings it back."""
    local.CONV_STATE_FILE.write_text(json.dumps({"processed_ids": ["grew", "same", "legacy"]}))
    staged = [
        {
            "session_id": "grew",
            "ended_at": "2026-09-01T12:00:00",
            "turn_count": 2,
            "regrown_from": "2026-09-01T10:00:00",
        },
        {
            "session_id": "same",
            "ended_at": "2026-09-01T10:00:00",
            "turn_count": 1,
            "regrown_from": "2026-09-01T10:00:00",
        },
        {"session_id": "legacy", "ended_at": "2026-09-01T10:00:00", "turn_count": 1},
    ]

    assert _extract(staged) == ["grew"]
    state = json.loads(local.CONV_STATE_FILE.read_text())
    assert state["extracted_from"]["grew"] == ["2026-09-01T12:00:00", 2]
    assert _extract(staged) == []

    staged[0] = {**staged[0], "ended_at": "2026-09-01T15:00:00"}  # a later end alone
    assert _extract(staged) == []
    staged[0] = {**staged[0], "turn_count": 3}  # and a new turn: it went on again
    assert _extract(staged) == ["grew"]


def test_the_copy_extracted_from_is_saved_where_processed_ids_is(extraction_paths, monkeypatch):
    """At every save: the periodic one, which is all a run that dies part-way
    leaves, and the last, which is all a run the deadline cuts short gets."""
    import itertools

    staged = [
        {
            "session_id": f"s{i}",
            "started_at": f"2026-09-01T1{i}:00",
            "ended_at": f"2026-09-01T1{i}:00",
            "turn_count": 1,
        }
        for i in range(4)
    ]
    ended = {c["session_id"]: c["ended_at"] for c in staged}

    def saved():
        return json.loads(local.CONV_STATE_FILE.read_text()).get("extracted_from")

    def run(extract, **kwargs):
        with (
            patch.object(local, "collect_conversations", return_value=staged),
            patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
            patch.object(local, "extract_conversation_inline", side_effect=extract) as ex,
        ):
            local.run_conversation_extraction(**kwargs)
        return [call.args[0]["session_id"] for call in ex.call_args_list]

    monkeypatch.setattr(local, "SAVE_INTERVAL", 2)
    calls = itertools.count()

    def dies_on_the_third(conv):
        if next(calls) == 2:
            raise RuntimeError("killed")
        return conv["session_id"], {"summary": "x"}, False, None

    with pytest.raises(RuntimeError):
        run(dies_on_the_third)
    assert saved() == {"s3": [ended["s3"], 1], "s2": [ended["s2"], 1]}  # newest first

    local.CONV_STATE_FILE.unlink()
    monkeypatch.setattr(local, "SAVE_INTERVAL", 50)
    clock = itertools.count(0.0, 10.0)
    monkeypatch.setattr(local.time, "monotonic", lambda: next(clock))
    done = run(lambda conv: (conv["session_id"], {"summary": "x"}, False, None), deadline_s=25.0)
    assert 0 < len(done) < len(staged)
    assert saved() == {sid: [ended[sid], 1] for sid in done}


def test_a_conversation_extracted_but_not_yet_loaded_that_went_on_is_extracted_again(
    extraction_paths,
):
    """Loaded or not, a session extracted before it went on is extracted again; one
    whose newer copy only ends later (a tab closed, which adds an event and no
    turn) is not."""
    extracted = ["2026-09-01T10:00:00", 1]
    local.CONV_STATE_FILE.write_text(
        json.dumps(
            {"processed_ids": ["s", "t"], "extracted_from": {"s": extracted, "t": extracted}}
        )
    )
    staged = [
        {"session_id": "s", "ended_at": "2026-09-01T11:00:00", "turn_count": 2},
        {"session_id": "t", "ended_at": "2026-09-01T11:00:00", "turn_count": 1},
    ]

    assert _extract(staged) == ["s"]


def _extraction(summary, decision, task, fact, topic):
    return {
        "summary": summary,
        "topics": [topic],
        "decisions": [{"decision": decision}],
        "action_items": [{"task": task}] if task else [],
        "key_facts": [fact],
    }


def _count(conn, rows, *params):
    return conn.execute(f"SELECT count(*) FROM {rows}", params).fetchone()[0]


def _of(transcript, extraction):
    """An extraction as run_conversation_extraction writes it: naming the end of
    the transcript it read."""
    return {**extraction, "transcript_ended_at": transcript["ended_at"]}


def test_the_loader_replaces_a_conversation_that_went_on(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    first = _transcript("s", "2026-09-01T10:00:00Z", [("user", "okapi one")])
    assert load_single_conversation(conn, first, _extraction("first", "d1", "t1", "f1", "alpha"))
    (old_id,) = conn.execute("SELECT id FROM conversations WHERE session_id = 's'").fetchone()
    grown = _transcript(
        "s", "2026-09-01T12:00:00Z", [("user", "okapi one"), ("assistant", "zebra two")]
    )

    assert load_single_conversation(
        conn, grown, _of(grown, _extraction("second", "d2", None, "f2", "beta"))
    )

    rows = conn.execute(
        "SELECT id, summary, turn_count, ended_at FROM conversations WHERE session_id = 's'"
    ).fetchall()
    assert [tuple(r)[1:] for r in rows] == [("second", 2, "2026-09-01T12:00:00Z")]
    assert rows[0][0] != old_id  # a new id, so its vector is made again
    assert _count(conn, "conversation_turns WHERE conversation_id = ?", old_id) == 0
    assert [r[0] for r in conn.execute("SELECT decision FROM decisions")] == ["d2"]
    assert _count(conn, "action_items") == 0
    assert [r[0] for r in conn.execute("SELECT fact FROM key_facts")] == ["f2"]
    assert _count(conn, "conversation_topics WHERE conversation_id = ?", old_id) == 0
    assert _count(conn, "conversation_turns_fts WHERE conversation_turns_fts MATCH 'zebra'") == 1
    assert _count(conn, "conversations_fts WHERE conversations_fts MATCH 'first'") == 0


def test_the_loader_leaves_a_conversation_that_did_not_go_on(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    meta = _transcript("s", "2026-09-01T10:00:00Z", [("user", "okapi one")])
    assert load_single_conversation(conn, meta, _extraction("first", "d1", "t1", "f1", "alpha"))
    later = _transcript("s", "2026-09-01T12:00:00Z", [("user", "okapi one")])  # no new turn
    longer = _transcript("s", "2026-09-01T10:00:00Z", [("user", "okapi one"), ("user", "x")])

    for again in (meta, later, longer):
        assert not load_single_conversation(
            conn, again, _of(again, _extraction("x", "d", "t", "f", "z"))
        )
    assert [r[0] for r in conn.execute("SELECT summary FROM conversations")] == ["first"]
    # A copy with no end on record is never overtaken.
    endless = {**_transcript("n", "2026-09-01T10:00:00Z", [("user", "a")]), "ended_at": None}
    assert load_single_conversation(conn, endless, _extraction("none", "d", "t", "f", "z"))
    grown = _transcript("n", "2026-09-01T12:00:00Z", [("user", "a"), ("user", "b")])
    assert not load_single_conversation(
        conn, grown, _of(grown, _extraction("x", "d", "t", "f", "z"))
    )


def test_replace_loads_a_conversation_again_whatever_it_holds(tmp_path):
    """The hook that ingests a session while it runs replaces what it loaded
    before, grown or not, under an id above every one in use."""
    conn = create_database(str(tmp_path / "b.db"))
    meta = _transcript("s", "2026-09-01T10:00:00Z", [("user", "okapi one")])
    assert load_single_conversation(conn, meta, _extraction("first", "d1", "t1", "f1", "alpha"))
    other = _transcript("t", "2026-09-01T10:00:00Z", [("user", "x")])
    assert load_single_conversation(conn, other, _extraction("other", "d", "t", "f", "o"))

    assert load_single_conversation(
        conn, meta, _extraction("again", "d2", "t2", "f2", "beta"), replace=True
    )

    rows = conn.execute("SELECT id, session_id, summary FROM conversations ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(2, "t", "other"), (3, "s", "again")]


def test_a_conversation_loaded_again_is_embedded_again_and_its_old_vector_goes(
    tmp_path, monkeypatch
):
    """build_index embeds only an id it holds no vector for, so the new id brings
    the new summary in; the old id's vector matches no row, and would take a
    search result's place beside the new one. Other kinds' vectors stay."""
    import numpy as np

    from src.store import embeddings

    index = tmp_path / "embeddings.npz"
    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", index)
    monkeypatch.setattr(embeddings, "_get_client", lambda: None)
    embedded: list[str] = []

    def embed(texts, client=None):
        embedded.extend(texts)
        return np.ones((len(texts), 4), dtype=np.float32)

    monkeypatch.setattr(embeddings, "generate_embeddings", embed)
    # An email, two attachments, a thread. Attachment 2,000,003 is past the
    # conversation offset, where its vector id reads as a conversation's.
    others = [7, -3, -2_000_003, embeddings.TEAMS_THREAD_ID_OFFSET - 5]
    np.savez(str(index), ids=np.array(others, dtype=np.int64), vectors=np.ones((4, 4), np.float32))
    conn = create_database(str(tmp_path / "b.db"))
    conn.execute(
        "INSERT INTO attachments (id, message_id, filename, file_path, exported_at) "
        "VALUES (1, 1, 'a.pdf', '/a.pdf', '')"
    )
    conn.execute(
        "INSERT INTO attachment_content (id, attachment_id, extraction_status) "
        "VALUES (2000003, 1, 'extracted')"
    )
    first = _transcript("s", "2026-09-01T10:00:00Z", [("user", "a")])
    load_single_conversation(conn, first, _extraction("first", "d1", "t1", "f1", "alpha"))
    conn.commit()
    embeddings.build_index(conn)
    grown = _transcript("s", "2026-09-01T12:00:00Z", [("user", "a"), ("assistant", "b")])
    load_single_conversation(
        conn, grown, _of(grown, _extraction("second", "d2", None, "f2", "beta"))
    )
    conn.commit()
    embedded.clear()

    assert embeddings.build_index(conn) == 1

    assert embedded == ["[Conversation in p] second"]
    (new_id,) = conn.execute("SELECT id FROM conversations").fetchone()
    ids = sorted(int(i) for i in np.load(index, allow_pickle=False)["ids"])
    assert ids == sorted([*others, embeddings.CONVERSATION_ID_OFFSET - new_id])


def test_the_running_session_hook_replaces_what_it_loaded(tmp_path, monkeypatch):
    """It deleted the session and loaded it again, which gave the new row the old
    id whenever the session was the newest, so build_index kept its old vector."""
    import argparse

    from src import cli
    from src.export import conversation_export
    from src.extract import claude_extract
    from src.store.schema import get_connection

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db = tmp_path / "b.db"
    create_database(str(db)).close()
    versions = iter(
        [
            _transcript("s", "2026-09-01T10:00:00Z", [("user", "a")]),
            _transcript("s", "2026-09-01T12:00:00Z", [("user", "a"), ("assistant", "b")]),
        ]
    )
    monkeypatch.setattr(conversation_export, "parse_conversation", lambda path: next(versions))
    summaries = iter(["first", "second"])
    monkeypatch.setattr(
        claude_extract,
        "extract_conversation",
        lambda conv: _extraction(next(summaries), "d", "t", "f", "x"),
    )
    transcript = tmp_path / "t.jsonl"
    args = argparse.Namespace(transcript=str(transcript), session_id="s", db=db)
    rows = []
    for lines in (60, 120):  # 50 new lines since the last run bring it back
        transcript.write_text("{}\n" * lines)
        cli.cmd_ingest_conversation_incremental(args)
        conn = get_connection(str(db))
        rows += [tuple(r) for r in conn.execute("SELECT id, summary FROM conversations")]
        conn.close()

    assert rows == [(1, "first"), (2, "second")]


def test_an_extraction_is_loaded_with_the_copy_it_read(tmp_path, monkeypatch):
    """The loader took the newest staged copy, which is ahead of the extraction
    when a session goes on between the two. Refused, a session never loaded
    stayed out of the store for good once the newer copy was given up."""
    from src import config
    from src.store.loader import load_conversations
    from src.store.schema import get_connection

    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    staging = tmp_path / "staging" / "conversations"
    extracted = tmp_path / "extracted" / "conversations"
    staging.mkdir(parents=True)
    extracted.mkdir(parents=True)
    first = _transcript("s", "2026-09-01T10:00:00Z", [("user", "a")])
    grown = _transcript("s", "2026-09-01T12:00:00Z", [("user", "a"), ("assistant", "b")])
    for n, copy in enumerate((first, grown), 1):
        batch = {"conversations": [copy]}
        (staging / f"conversation-batch-{n:03d}.json").write_text(json.dumps(batch))
    db = tmp_path / "b.db"
    create_database(str(db)).close()

    def load(copy, summary):
        extraction = _of(copy, _extraction(summary, "d", "t", "f", "x"))
        (extracted / "s.json").write_text(json.dumps(extraction))
        load_conversations(str(db))
        conn = get_connection(str(db))
        rows = [tuple(r) for r in conn.execute("SELECT summary, turn_count FROM conversations")]
        conn.close()
        return rows

    assert load(first, "first") == [("first", 1)]
    assert load(grown, "second") == [("second", 2)]
    assert load(first, "stale") == [("second", 2)]  # never back to an older copy


def test_the_new_id_is_taken_under_the_write_lock(tmp_path):
    """MAX(id) was read before the delete, outside any transaction, so another
    writer could take that id first, and the insert failed."""
    import sqlite3

    db = tmp_path / "b.db"
    conn = create_database(str(db))
    first = _transcript("s", "2026-09-01T10:00:00Z", [("user", "a")])
    load_single_conversation(conn, first, _extraction("first", "d", "t", "f", "x"))
    conn.commit()
    other = sqlite3.connect(str(db), timeout=0)
    blocked = []

    class Racing:
        """The store's connection, with another writer adding a conversation just
        before the new id is read."""

        def execute(self, sql, *args):
            if "MAX(id)" in sql:
                try:
                    other.execute(
                        "INSERT INTO conversations (session_id, started_at, created_at) "
                        "VALUES ('t', '', '')"
                    )
                    other.commit()
                except sqlite3.OperationalError:
                    blocked.append(sql)
            return conn.execute(sql, *args)

    grown = _transcript("s", "2026-09-01T12:00:00Z", [("user", "a"), ("user", "b")])
    extraction = _of(grown, _extraction("second", "d", "t", "f", "x"))
    assert load_single_conversation(Racing(), grown, extraction)
    assert blocked  # the other writer waited for this one


def test_an_extraction_says_which_transcript_it_describes(extraction_paths):
    _extract([{"session_id": "s", "ended_at": "2026-09-01T10:00:00"}])

    saved = json.loads((local.CONV_EXTRACTED_DIR / "s.json").read_text())
    assert saved["transcript_ended_at"] == "2026-09-01T10:00:00"


def test_the_loader_waits_for_an_extraction_of_the_transcript_staged(tmp_path):
    """The loader pairs the newest staged transcript with the extraction on disk,
    which lags when extraction runs out of time before a session that went on.
    Loaded then, the new turns carried the old summary, and the new extraction
    never loaded, since the store already held that transcript."""
    conn = create_database(str(tmp_path / "b.db"))
    first = _transcript("s", "2026-09-01T10:00:00Z", [("user", "a")])
    grown = _transcript("s", "2026-09-01T12:00:00Z", [("user", "a"), ("assistant", "b")])
    old = {**_extraction("first", "d1", "t1", "f1", "a"), "transcript_ended_at": first["ended_at"]}
    new = {**_extraction("second", "d2", "t2", "f2", "b"), "transcript_ended_at": grown["ended_at"]}
    legacy = _extraction("legacy", "d3", "t3", "f3", "c")  # written before the record

    assert not load_single_conversation(conn, grown, old)  # a first load waits too
    assert load_single_conversation(conn, first, old)
    assert not load_single_conversation(conn, grown, old)
    assert not load_single_conversation(conn, grown, legacy)  # cannot tell what it read
    assert load_single_conversation(conn, grown, new)
    assert [r[0] for r in conn.execute("SELECT summary FROM conversations")] == ["second"]

    other = _transcript("t", "2026-09-01T10:00:00Z", [("user", "x")])
    assert load_single_conversation(conn, other, legacy)  # a first load, as before
    stale = {**legacy, "transcript_ended_at": "2026-09-01T09:00:00Z"}
    assert load_single_conversation(conn, other, stale, replace=True)  # the caller's own


def test_sync_embeds_the_conversations_it_loads(tmp_path, monkeypatch):
    """Step 5 embeds before Step 7 loads, and only when mail came in, so a session
    loaded again had no vector, and semantic search could not find it, until a
    sync with new mail. Failing to embed does not stop the sync."""
    from src import cli, llm_deadline

    db_path = tmp_path / "brain.db"
    conn = create_database(str(db_path))
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("src.cli.DATA_ROOT", tmp_path)
    monkeypatch.setattr(llm_deadline, "_detect_systemd_unit", lambda: None)
    args = type(
        "Args",
        (),
        {"db": db_path, "limit": None, "engine": "claude", "workers": 1, "skip_export": True},
    )()
    embedded = []

    def sync(loaded, embed):
        ok = {"extracted": 0, "failed": 0, "quota_paused": False}
        with (
            patch("src.extract.local.run_extraction", return_value=ok),
            patch("src.store.loader.load_extractions", return_value=0),
            patch("src.extract.attachment_pipeline.run_phase1", return_value={"processed": 0}),
            patch(
                "src.export.conversation_export.export_conversations", return_value={"exported": 1}
            ),
            patch("src.extract.local.run_conversation_extraction"),
            patch("src.store.loader.load_conversations", return_value=loaded),
            patch("src.store.embeddings.build_index", side_effect=embed),
            patch("src.extract.image_pipeline.run_backfill", return_value={}),
        ):
            return cli.cmd_sync(args)

    def down(conn):
        raise RuntimeError("embedding service down")

    assert sync(1, lambda conn: embedded.append("loaded")) == 0
    assert sync(0, lambda conn: embedded.append("none loaded")) == 0
    assert embedded == ["loaded"]
    assert sync(1, down) == 0
