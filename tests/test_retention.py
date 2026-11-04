"""`brain maint forget --before DATE` (issue 174): one path across rows, full text, vectors and
files; a dry run by default that changes nothing and counts what --apply then removes."""

import json
import sqlite3
from argparse import Namespace
from pathlib import Path

import numpy as np
import pytest

from src.store import retention
from src.store.schema import apply_gated, create_database

OLD = "2024-06-01T10:00:00Z"
NEW = "2026-10-01T10:00:00Z"
CUTOFF = "2025-01-01"


@pytest.fixture
def roots(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "attachments").mkdir(parents=True)
    monkeypatch.setattr("src.config.DATA_ROOT", data)
    monkeypatch.setattr("src.config.ATTACHMENTS_DIR", data / "attachments")
    monkeypatch.setattr("src.config.RAW_BATCH_DIR", data / "raw")
    monkeypatch.setattr("src.config.SHAREPOINT_DATA_DIR", data / "sharepoint")
    monkeypatch.setattr("src.store.embeddings.EMBEDDINGS_FILE", data / "embeddings.npz")
    return data


def _store(path: Path, vectors: bool) -> sqlite3.Connection:
    conn = create_database(str(path))
    if vectors:
        apply_gated(conn, owner=True)
        conn.commit()
    return conn


def _email(conn, roots, n: int, when: str, *, file_name: str | None = None) -> dict:
    eid = conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, content, conversation_id,"
        " internet_message_id) VALUES (?, ?, ?, ?, ?, ?)",
        (f"M{n}", when, f"subject{n}", f"body word{n}", f"conv{n}", f"<imid{n}>"),
    ).lastrowid
    name = file_name or f"f{n}.pdf"
    folder = roots / "attachments" / str(n)
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(b"x" * 10)
    aid = conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, file_path, exported_at, sha256)"
        " VALUES (?, ?, ?, ?, 'now', ?)",
        (eid, n, name, f"/elsewhere/attachments/{n}/{name}", f"sha{n}"),
    ).lastrowid
    cid = conn.execute(
        "INSERT INTO attachment_content (attachment_id, extraction_status, extracted_text,"
        " summary) VALUES (?, 'ok', ?, 'sum')",
        (aid, f"attachtext{n}"),
    ).lastrowid
    conn.execute(
        "INSERT INTO attachment_chunks (attachment_id, chunk_no, content_id, start_char,"
        " end_char, chars, text_chars, chunked_at) VALUES (?, 0, ?, 0, 5, 5, 5, 'now')",
        (aid, cid),
    )
    did = conn.execute(
        "INSERT INTO decisions (email_id, decision) VALUES (?, ?)", (eid, f"decide{n}")
    ).lastrowid
    conn.execute("INSERT INTO key_facts (email_id, fact) VALUES (?, ?)", (eid, f"fact{n}"))
    conn.execute("INSERT INTO action_items (email_id, task) VALUES (?, ?)", (eid, f"task{n}"))
    conn.execute(
        "INSERT INTO item_labels (item_table, item_id, label, method, thread_key, run_id,"
        " created_at) VALUES ('decisions', ?, 'completed_by', 'rule', 't', 'r', 'now')",
        (did,),
    )
    tid = conn.execute(
        "INSERT INTO topics (name, display_name) VALUES (?, ?)", (f"t{n}", f"T{n}")
    ).lastrowid
    conn.execute("INSERT INTO email_topics (email_id, topic_id) VALUES (?, ?)", (eid, tid))
    conn.execute(
        "INSERT INTO email_aliases (message_id, email_id, recorded_at) VALUES (?, ?, 'now')",
        (f"A{n}", eid),
    )
    return {"email": eid, "attachment": aid, "content": cid, "topic": tid}


def _teams(conn, n: int, when: str) -> int:
    chat = conn.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, first_seen_at) VALUES (?, 'oneOnOne',"
        " 'now')",
        (f"chat{n}",),
    ).lastrowid
    thread = conn.execute(
        "INSERT INTO teams_threads (chat_id, thread_kind, started_at, ended_at, summary)"
        " VALUES (?, 'chat_session', ?, ?, ?)",
        (chat, when, when, f"teamsum{n}"),
    ).lastrowid
    conn.execute(
        "INSERT INTO teams_messages (teams_message_id, chat_id, thread_id, composed_at,"
        " content_text) VALUES (?, ?, ?, ?, ?)",
        (f"tm{n}", chat, thread, when, f"teamsword{n}"),
    )
    conn.execute(
        "INSERT INTO key_facts (teams_thread_id, fact) VALUES (?, ?)", (thread, f"tfact{n}")
    )
    return thread


def _whatsapp(conn, n: int, when: str) -> int:
    chat = conn.execute(
        "INSERT INTO whatsapp_chats (chat_jid, chat_kind, first_seen_at) VALUES (?, 'direct', 'now')",
        (f"jid{n}",),
    ).lastrowid
    thread = conn.execute(
        "INSERT INTO whatsapp_threads (chat_id, anchor_message_id, started_at, ended_at)"
        " VALUES (?, ?, ?, ?)",
        (chat, f"w{n}", when, when),
    ).lastrowid
    conn.execute(
        "INSERT INTO whatsapp_messages (chat_jid, message_id, chat_id, thread_id, sent_at,"
        " content) VALUES (?, ?, ?, ?, ?, ?)",
        (f"jid{n}", f"w{n}", chat, thread, when, f"waword{n}"),
    )
    return thread


def _event(conn, n: int, when: str) -> int:
    ev = conn.execute(
        "INSERT INTO calendar_events (outlook_event_id, subject, start_at, end_at, ingested_at)"
        " VALUES (?, ?, ?, ?, 'now')",
        (f"ev{n}", f"meeting{n}", when, when),
    ).lastrowid
    conn.execute(
        "INSERT INTO event_attendees (event_id, email, response_status) VALUES (?, 'a@x', 'ok')",
        (ev,),
    )
    conn.execute("INSERT INTO decisions (event_id, decision) VALUES (?, ?)", (ev, f"evdec{n}"))
    return ev


def _conversation(conn, n: int, when: str) -> int:
    cid = conn.execute(
        "INSERT INTO conversations (session_id, started_at, ended_at, created_at, summary)"
        " VALUES (?, ?, ?, 'now', ?)",
        (f"s{n}", when, when, f"convsum{n}"),
    ).lastrowid
    turn = conn.execute(
        "INSERT INTO conversation_turns (conversation_id, turn_index, timestamp, speaker,"
        " content) VALUES (?, 0, ?, 'user', ?)",
        (cid, when, f"turnword{n}"),
    ).lastrowid
    conn.execute(
        "INSERT INTO action_items (conversation_turn_id, task) VALUES (?, ?)", (turn, f"ct{n}")
    )
    return cid


def _vector_ids(ids: dict, teams: int, wa: int, conv: int) -> list[int]:
    from src.store import embeddings as emb

    return [
        ids["email"],
        -ids["content"],
        emb.chunk_vector_id(ids["content"], 0),
        emb.email_chunk_vector_id(ids["email"], 0),
        emb.TEAMS_THREAD_ID_OFFSET - teams,
        emb.WHATSAPP_THREAD_ID_OFFSET - wa,
        emb.CONVERSATION_ID_OFFSET - conv,
    ]


def _populate(conn, roots):
    old = _email(conn, roots, 1, OLD)
    new = _email(conn, roots, 2, NEW)
    olds = (_teams(conn, 1, OLD), _whatsapp(conn, 1, OLD), _conversation(conn, 1, OLD))
    news = (_teams(conn, 2, NEW), _whatsapp(conn, 2, NEW), _conversation(conn, 2, NEW))
    _event(conn, 1, OLD)
    _event(conn, 2, NEW)
    conn.commit()
    return _vector_ids(old, *olds), _vector_ids(new, *news)


def _write_vectors(conn, ids: list[int], with_table: bool) -> None:
    from src.store import embeddings as emb
    from src.store import vector_store

    vecs = np.random.default_rng(0).standard_normal((len(ids), 8)).astype(np.float32)
    if with_table:
        vector_store.write(conn, ids, vecs, 1)
        conn.commit()
    else:
        emb._atomic_savez(emb.EMBEDDINGS_FILE, np.array(ids, dtype=np.int64), vecs, {})


def _snapshot(conn) -> dict:
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE '%fts%'"
        )
    ]
    return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def _fts_hits(conn, word: str) -> int:
    hits = 0
    for table in (
        "emails_fts",
        "attachment_content_fts",
        "teams_messages_fts",
        "whatsapp_messages_fts",
        "conversation_turns_fts",
        "teams_threads_fts",
        "conversations_fts",
    ):
        hits += conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {table} MATCH ?", (word,)
        ).fetchone()[0]
    return hits


@pytest.mark.parametrize("with_table", [True, False], ids=["vectors-table", "npz"])
def test_dry_run_changes_nothing_and_apply_removes_only_the_old(tmp_path, roots, with_table):
    conn = _store(tmp_path / "brain.db", vectors=with_table)
    old_ids, new_ids = _populate(conn, roots)
    _write_vectors(conn, old_ids + new_ids, with_table)
    before = _snapshot(conn)
    npz_before = (roots / "embeddings.npz").read_bytes() if not with_table else None

    dry = retention.forget(conn, cutoff=CUTOFF)

    assert not dry.applied
    assert _snapshot(conn) == before
    if npz_before is not None:
        assert (roots / "embeddings.npz").read_bytes() == npz_before
    assert (roots / "attachments" / "1" / "f1.pdf").exists()
    assert dry.tables["emails"] == 1
    assert dry.tables["teams_threads"] == 1 and dry.tables["whatsapp_threads"] == 1
    assert dry.tables["calendar_events"] == 1 and dry.tables["conversations"] == 1
    assert dry.tables["decisions"] == 2  # the email's and the meeting's
    assert dry.vectors == len(old_ids)
    assert dry.files == 1

    done = retention.forget(conn, cutoff=CUTOFF, apply=True)

    assert done.applied
    assert done.tables == dry.tables
    assert done.vectors == dry.vectors
    after = _snapshot(conn)
    for table, n in done.tables.items():
        assert before[table] - after[table] == n, table
    for word in (
        "word1",
        "attachtext1",
        "teamsword1",
        "waword1",
        "turnword1",
        "teamsum1",
        "convsum1",
    ):
        assert _fts_hits(conn, word) == 0, word
    for word in ("word2", "attachtext2", "teamsword2", "waword2", "turnword2"):
        assert _fts_hits(conn, word) == 1, word
    assert not (roots / "attachments" / "1").exists()
    assert (roots / "attachments" / "2" / "f2.pdf").exists()
    if with_table:
        held = {r[0] for r in conn.execute("SELECT id FROM vectors")}
    else:
        held = set(np.load(roots / "embeddings.npz")["ids"].tolist())
    assert held == set(new_ids)
    assert conn.execute("SELECT COUNT(*) FROM item_labels").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM email_aliases").fetchone()[0] == 1
    # Names stay: the topic of the forgotten email is kept.
    assert conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 2


def test_a_store_without_gated_steps(tmp_path, roots):
    conn = create_database(str(tmp_path / "brain.db"), gated=False)
    _populate(conn, roots)
    result = retention.forget(conn, cutoff=CUTOFF, apply=True)
    assert result.tables["emails"] == 1
    assert result.vector_backend == "embeddings.npz"


def test_a_file_a_kept_row_names_stays(tmp_path, roots):
    conn = _store(tmp_path / "brain.db", vectors=False)
    _email(conn, roots, 1, OLD)
    keep = _email(conn, roots, 2, NEW)
    # The new email's attachment row names the old one's file too.
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, file_path, exported_at)"
        " VALUES (?, 2, 'f1.pdf', '/x/attachments/1/f1.pdf', 'now')",
        (keep["email"],),
    )
    outside = roots.parent / "outside.json"
    outside.write_text("{}")
    conn.commit()
    result = retention.forget(conn, cutoff=CUTOFF, apply=True)
    assert result.files == 0
    assert (roots / "attachments" / "1" / "f1.pdf").exists()
    assert outside.exists()


def test_staging_batches_before_the_date_go(tmp_path, roots):
    import os

    conn = _store(tmp_path / "brain.db", vectors=False)
    staging = roots / "staging"
    staging.mkdir()
    old, new = staging / "batch-00001.json", staging / "batch-00002.json"
    old.write_text("{}")
    new.write_text("{}")
    os.utime(old, (1_700_000_000, 1_700_000_000))  # 2023-11
    result = retention.forget(conn, cutoff=CUTOFF, apply=True)
    assert result.files == 1
    assert not old.exists() and new.exists()


def test_topic_pages_citing_forgotten_items_go(tmp_path, roots):
    conn = _store(tmp_path / "brain.db", vectors=False)
    old = _email(conn, roots, 1, OLD)
    new = _email(conn, roots, 2, NEW)
    for topic, ref in (("a", f"email:{old['email']}"), ("b", f"email:{new['email']}")):
        conn.execute(
            "INSERT INTO topic_pages (topic, display_name, members, selected_by, body, citations,"
            " evidence_keys, built_at, newest_evidence_at, prompt_hash)"
            " VALUES (?, ?, '[]', 'x', 'body', ?, ?, 'now', ?, 'h')",
            (topic, topic, json.dumps([{"ref": ref}]), json.dumps([ref]), NEW),
        )
    conn.commit()
    result = retention.forget(conn, cutoff=CUTOFF, apply=True)
    assert result.tables["topic_pages"] == 1
    assert [r[0] for r in conn.execute("SELECT topic FROM topic_pages")] == ["b"]


def test_only_the_sources_named(tmp_path, roots):
    conn = _store(tmp_path / "brain.db", vectors=False)
    _populate(conn, roots)
    result = retention.forget(conn, cutoff=CUTOFF, sources={"teams"})
    assert set(result.tables) >= {"teams_threads", "teams_messages"}
    assert "emails" not in result.tables and "conversations" not in result.tables


def test_every_table_is_covered_or_kept_on_purpose(tmp_path):
    conn = _store(tmp_path / "brain.db", vectors=True)
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not r[0].endswith(
            ("_fts", "_fts_data", "_fts_idx", "_fts_docsize", "_fts_config", "_fts_content")
        )
    }
    assert not retention.COVERED & retention.KEPT
    assert tables - retention.COVERED - retention.KEPT == set()


def test_cutoff_in_the_future_is_refused():
    with pytest.raises(ValueError):
        retention.parse_cutoff("2999-01-01")
    assert retention.parse_cutoff("2025-01-01") == "2025-01-01"


def test_deleted_at_source_reports_without_deleting(tmp_path, roots, monkeypatch):
    monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", "me@x.org")
    conn = _store(tmp_path / "brain.db", vectors=False)
    gone = _email(conn, roots, 1, NEW)
    sent = _email(conn, roots, 2, NEW)
    _email(conn, roots, 3, NEW)
    # Email 2 is the owner's own: its Sent Items copy may still be in the mailbox.
    conn.execute("UPDATE emails SET sender_address = 'Me@X.org' WHERE id = ?", (sent["email"],))
    for m in ("M1", "M2", "M9"):
        conn.execute(
            "INSERT INTO sync_metadata (key, value) VALUES (?, ?)",
            (f"mail_location:{m}", json.dumps({"location": "Deleted Items"})),
        )
    conn.commit()
    before = _snapshot(conn)
    report = retention.deleted_at_source(conn)
    assert report["email_ids"] == [gone["email"]]
    assert report["owner_sent_held_back"] == 1
    assert report["emails_by_location"] == {"Deleted Items": 1}
    assert _snapshot(conn) == before


def test_command_deleted_at_source_forgets_only_the_candidates(tmp_path, roots, capsys):
    db = tmp_path / "brain.db"
    conn = _store(db, vectors=False)
    _email(conn, roots, 1, NEW)
    _email(conn, roots, 2, NEW)
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('mail_location:M1', ?)",
        (json.dumps({"location": "Junk Email"}),),
    )
    conn.commit()
    conn.close()
    args = _args(db, before=None, deleted_at_source=True, apply=True, backup=str(tmp_path / "b.db"))
    assert retention.cmd_forget(args) == 0
    left = [r[0] for r in sqlite3.connect(db).execute("SELECT message_id FROM emails")]
    assert left == ["M2"]


# --- the command ---------------------------------------------------------------------------


def _args(db, **kw) -> Namespace:
    base = {
        "db": str(db),
        "before": CUTOFF,
        "source": None,
        "apply": False,
        "backup": None,
        "deleted_at_source": False,
    }
    base.update(kw)
    return Namespace(**base)


def test_command_dry_run_prints_counts_and_what_it_cannot_reach(tmp_path, roots, capsys):
    db = tmp_path / "brain.db"
    conn = _store(db, vectors=False)
    _populate(conn, roots)
    conn.close()
    assert retention.cmd_forget(_args(db)) == 0
    out = capsys.readouterr().out
    assert "dry run" in out.lower()
    assert "emails: 1" in out
    assert "restic" in out
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 2


def test_command_apply_needs_a_backup(tmp_path, roots, capsys):
    db = tmp_path / "brain.db"
    _store(db, vectors=False).close()
    with pytest.raises(SystemExit) as exc:
        retention.cmd_forget(_args(db, apply=True))
    assert exc.value.code == 2


def test_command_apply_with_backup(tmp_path, roots, capsys):
    db = tmp_path / "brain.db"
    conn = _store(db, vectors=False)
    _populate(conn, roots)
    conn.close()
    backup = tmp_path / "pre-forget.db"
    assert retention.cmd_forget(_args(db, apply=True, backup=str(backup))) == 0
    assert backup.exists()
    assert sqlite3.connect(backup).execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 2
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 1


def test_command_refuses_on_a_replica(tmp_path, roots, monkeypatch, capsys):
    db = tmp_path / "brain.db"
    _store(db, vectors=False).close()
    monkeypatch.setattr("src.config.is_replica", lambda: True)
    assert retention.cmd_forget(_args(db)) == 2
