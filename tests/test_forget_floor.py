"""The forget floor (issue 286): what `forget --before DATE --apply` removed is never stored
again by a later sync or backfill, one test per path that stores an item."""

import json
import sqlite3
import types
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.store import forget_floor, retention
from src.store.schema import create_database

OLD = "2024-06-01T10:00:00Z"
NEW = "2026-10-01T10:00:00Z"
CUTOFF = "2025-01-01"


@pytest.fixture(autouse=True)
def _fresh_counts():
    forget_floor.SKIPPED.clear()
    yield
    forget_floor.SKIPPED.clear()


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    yield path, conn
    conn.close()


def _set(conn, **floors):
    for source, day in floors.items():
        forget_floor.raise_floors(conn, [source], day)
    conn.commit()


# --- setting the floor --------------------------------------------------------------------------


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


def test_apply_sets_the_floor_of_each_source_it_ran_on(store, roots):
    _, conn = store
    retention.forget(conn, cutoff=CUTOFF, sources={"teams", "email"}, apply=True)
    assert forget_floor.floors(conn) == {"email": CUTOFF, "teams": CUTOFF}


def test_apply_on_every_source_sets_all_five(store, roots):
    _, conn = store
    retention.forget(conn, cutoff=CUTOFF, apply=True)
    assert forget_floor.floors(conn) == dict.fromkeys(forget_floor.SOURCES, CUTOFF)


def test_a_dry_run_sets_no_floor(store, roots):
    _, conn = store
    retention.forget(conn, cutoff=CUTOFF)
    assert forget_floor.floors(conn) == {}


def test_deleted_at_source_sets_no_floor(store, roots):
    _, conn = store
    retention.forget(conn, email_ids=[], apply=True)
    assert forget_floor.floors(conn) == {}


def test_the_floor_only_moves_forward(store, roots):
    _, conn = store
    retention.forget(conn, cutoff="2025-06-01", sources={"email"}, apply=True)
    retention.forget(conn, cutoff="2025-01-01", sources={"email"}, apply=True)
    assert forget_floor.floor(conn, "email") == "2025-06-01"
    retention.forget(conn, cutoff="2025-09-01", sources={"email"}, apply=True)
    assert forget_floor.floor(conn, "email") == "2025-09-01"


def test_the_floor_rolls_back_with_a_forget_that_fails(store, roots, monkeypatch):
    _, conn = store

    def boom(*a, **k):
        raise RuntimeError("mid-run")

    monkeypatch.setattr(retention, "_delete_pages", boom)
    with pytest.raises(RuntimeError):
        retention.forget(conn, cutoff=CUTOFF, apply=True)
    assert forget_floor.floors(conn) == {}


def test_the_command_lists_and_clears_with_a_warning(store, capsys, monkeypatch):
    path, conn = store
    _set(conn, email=CUTOFF, teams=CUTOFF)
    monkeypatch.setattr("src.config.is_replica", lambda: False)

    assert forget_floor.cmd_forget_floor(Namespace(db=str(path), clear=None)) == 0
    out = capsys.readouterr().out
    assert f"email: {CUTOFF}" in out and "calendar: (none)" in out

    assert forget_floor.cmd_forget_floor(Namespace(db=str(path), clear="teams")) == 0
    captured = capsys.readouterr()
    assert "WARNING" in captured.err and "teams" in captured.err
    assert forget_floor.floors(conn) == {"email": CUTOFF}


def test_the_command_refuses_to_clear_on_a_replica(store, monkeypatch):
    path, conn = store
    _set(conn, email=CUTOFF)
    monkeypatch.setattr("src.config.is_replica", lambda: True)
    assert forget_floor.cmd_forget_floor(Namespace(db=str(path), clear="email")) == 2
    assert forget_floor.floor(conn, "email") == CUTOFF


def test_the_command_is_registered_under_maint():
    import argparse
    import inspect

    from src import cli

    parser = argparse.ArgumentParser()
    forget_floor.add_maint_parsers(parser.add_subparsers())
    args = parser.parse_args(["forget-floor", "--clear", "email"])
    assert args.clear == "email" and args.func is forget_floor.cmd_forget_floor
    assert "forget_floor" in inspect.getsource(cli)


# --- mail ---------------------------------------------------------------------------------------


def _staged(n: int, when: str) -> dict:
    return {
        "message_id": f"M{n}",
        "date_received": when,
        "subject": f"subject {n}",
        "sender": {"name": "A", "address": "a@example.test"},
        "content": "body",
        "mailbox_name": "Inbox",
    }


def test_the_mail_load_refuses_an_email_older_than_the_floor(store):
    from src.store.loader import load_single_email

    _, conn = store
    _set(conn, email=CUTOFF)
    assert load_single_email(conn, _staged(1, OLD), {"summary": "s"}) is False
    assert load_single_email(conn, _staged(2, NEW), {"summary": "s"}) is True
    conn.commit()
    assert [r[0] for r in conn.execute("SELECT message_id FROM emails")] == ["M2"]
    assert forget_floor.SKIPPED["email"] == 1


def test_an_undated_email_is_let_through(store):
    from src.store.loader import load_single_email

    _, conn = store
    _set(conn, email=CUTOFF)
    assert load_single_email(conn, _staged(3, ""), {"summary": "s"}) is True


def test_a_batch_of_forgotten_mail_is_pruned_not_loaded_each_run(store, tmp_path):
    from src.store.loader import load_extractions

    path, conn = store
    _set(conn, email=CUTOFF)
    staging, extracted = tmp_path / "staging", tmp_path / "extracted"
    staging.mkdir()
    extracted.mkdir()
    (staging / "batch-00001.json").write_text(json.dumps({"emails": [_staged(1, OLD)]}))
    from src.extract.extraction_files import extraction_path

    target = extraction_path(extracted, "M1")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"summary": "s"}))

    assert load_extractions(str(path), str(extracted), str(staging)) == 0
    assert not (staging / "batch-00001.json").exists()
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 0


def test_extraction_leaves_out_mail_older_than_the_floor(store, monkeypatch, tmp_path):
    from src.extract import local

    path, conn = store
    _set(conn, email=CUTOFF)
    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    pending = [_staged(1, OLD), _staged(2, NEW)]
    kept = local._without_stored_copies(pending, pending, set(), path)
    assert [e["message_id"] for e in kept] == ["M2"]
    assert forget_floor.SKIPPED["email"] == 1


def test_the_outlook_export_fetches_nothing_older_than_the_floor(store, tmp_path, monkeypatch):
    from src.export import outlook_export
    from src.export.state import OutlookSyncState, save_outlook_sync_state

    path, conn = store
    _set(conn, email=CUTOFF)
    state = tmp_path / "s.json"
    save_outlook_sync_state(
        state, OutlookSyncState(last_seen_received_at="2024-05-01T00:00:00Z", folder="Inbox")
    )
    monkeypatch.setattr(outlook_export, "run_outlook_cli", lambda args: {})
    monkeypatch.setattr(
        outlook_export,
        "list_new_messages",
        lambda *a, **k: [
            {"Id": "old", "ReceivedDateTime": OLD},
            {"Id": "new", "ReceivedDateTime": NEW},
        ],
    )
    fetched: list[str] = []

    def fetch(ids, concurrency=5):
        fetched.extend(ids)
        return []

    monkeypatch.setattr(outlook_export, "fetch_bodies_concurrent", fetch)
    out = outlook_export.run_hourly_sync(
        state_path=state,
        folder="Inbox",
        fetch_attachments=False,
        db_path=path,
        staging_dir=tmp_path / "staging",
    )
    assert fetched == ["new"]
    assert out.get("below_floor") == 1


def test_mail_reconcile_reports_forgotten_mail_and_never_refetches_it(store):
    from src.export import mail_reconcile as mr

    _, conn = store
    _set(conn, email=CUTOFF)
    st = mr.read_store(conn)
    kinds = mr.classify(
        [{"Id": "old", "ReceivedDateTime": OLD}, {"Id": "new", "ReceivedDateTime": NEW}], st
    )
    assert [m["Id"] for m in kinds["forgotten"]] == ["old"]
    assert [m["Id"] for m in kinds["missing"]] == ["new"]


def test_a_document_older_than_the_floor_is_not_ingested(store):
    from src.extract.attachment_pipeline import ingest_text_document

    _, conn = store
    _set(conn, email=CUTOFF)
    outcome = ingest_text_document(
        conn,
        source="sharepoint",
        key="k",
        filename="plan.txt",
        mime_type="text/plain",
        text="words " * 20,
        sha256="a" * 64,
        method="direct_read",
        status="extracted",
        error=None,
        subject="[SharePoint] plan",
        sender_name="SharePoint",
        date=OLD,
    )
    assert outcome["skipped"] is True and "forget floor" in outcome["reason"]
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 0


def test_a_document_file_older_than_the_floor_is_not_ingested(store, tmp_path):
    import os

    from src.extract.attachment_pipeline import ingest_document

    path, conn = store
    _set(conn, email=CUTOFF)
    f = tmp_path / "old.txt"
    f.write_text("an old note " * 20)
    old = datetime(2024, 6, 1, tzinfo=UTC).timestamp()
    os.utime(f, (old, old))
    outcome = ingest_document(str(f), db_path=str(path))
    assert outcome["skipped"] is True and "forget floor" in outcome["reason"]
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 0


# --- Teams --------------------------------------------------------------------------------------


@pytest.fixture
def teams(tmp_path):
    from src.store.schema import (
        migrate_add_teams,
        migrate_add_teams_ingest_disabled_at,
        migrate_add_teams_last_pulled_at,
        run_migrations,
    )

    path = str(tmp_path / "teams.db")
    create_database(path).close()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    migrate_add_teams(conn)
    migrate_add_teams_last_pulled_at(conn)
    migrate_add_teams_ingest_disabled_at(conn)
    conn.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, first_seen_at)"
        " VALUES ('19:c@thread.v2', 'group', 'T', '2024-01-01T00:00:00')"
    )
    conn.commit()
    yield conn
    conn.close()


def _chat_msg(mid: str, at: str, text: str = "hello there", version: int | None = None) -> dict:
    return {
        "id": mid,
        "messagetype": "Text",
        "content": text,
        "composetime": at,
        "from": "https://teams.example.test/v1/users/ME/contacts/8:orgid:1",
        "imdisplayname": "Example",
        "version": version or int(mid),
        "properties": {},
    }


def test_the_teams_sync_refuses_a_message_older_than_the_floor(teams):
    from src.export.teams_export import _persist_messages

    _set(teams, teams=CUTOFF)
    chat = teams.execute("SELECT id FROM teams_chats").fetchone()[0]
    n = _persist_messages(teams, chat, {"messages": [_chat_msg("1", OLD), _chat_msg("2", NEW)]})
    assert n == 1
    assert [r[0] for r in teams.execute("SELECT composed_at FROM teams_messages")] == [NEW]
    assert forget_floor.SKIPPED["teams"] == 1


def test_a_stored_teams_message_older_than_the_floor_still_takes_its_edit(teams):
    """A thread that straddled the day was kept whole; its old messages stay current."""
    from src.export.teams_export import _persist_messages

    chat = teams.execute("SELECT id FROM teams_chats").fetchone()[0]
    _persist_messages(teams, chat, {"messages": [_chat_msg("1", OLD, "first")]})
    _set(teams, teams=CUTOFF)
    _persist_messages(teams, chat, {"messages": [_chat_msg("1", OLD, "edited", version=9)]})
    assert teams.execute("SELECT content_text FROM teams_messages").fetchone()[0] == "edited"
    assert forget_floor.SKIPPED["teams"] == 0


def test_teams_backfill_replies_refuses_replies_older_than_the_floor(teams):
    from src.export.teams_export import backfill_channel_replies

    teams.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, first_seen_at)"
        " VALUES ('19:ch@thread.tacv2', 'channel', 'P', '2024-01-01T00:00:00')"
    )
    chat = teams.execute("SELECT id FROM teams_chats WHERE chat_kind = 'channel'").fetchone()[0]
    post = {
        "id": "100",
        "message": {**_chat_msg("100", NEW), "composeTime": NEW},
        "replies": {
            "messages": [
                {**_chat_msg("101", OLD), "composeTime": OLD},
                {**_chat_msg("102", NEW), "composeTime": NEW},
            ]
        },
    }
    teams.execute(
        "INSERT INTO teams_messages (teams_message_id, chat_id, composed_at, raw_json)"
        " VALUES (?, ?, ?, ?)",
        (f"{chat}::100", chat, NEW, json.dumps(post)),
    )
    _set(teams, teams=CUTOFF)
    counts = backfill_channel_replies(teams)
    assert counts["replies_stored"] == 1
    assert counts["replies_below_floor"] == 1


# --- WhatsApp -----------------------------------------------------------------------------------


def test_the_whatsapp_sync_refuses_a_message_older_than_the_floor(store, tmp_path):
    from src.export.whatsapp_export import ingest_snapshot
    from tests.whatsapp.conftest import ALICE, DIRECT_JID, build_snapshot

    _, conn = store
    _set(conn, whatsapp=CUTOFF)
    rows = [
        ("m1", DIRECT_JID, ALICE, "old", "2024-06-01 10:00:00+03:00", 0, "", ""),
        ("m2", DIRECT_JID, ALICE, "new", "2026-10-01 10:00:00+03:00", 0, "", ""),
    ]
    out = ingest_snapshot(conn, build_snapshot(tmp_path, rows))
    assert out["messages_inserted"] == 1
    assert out["below_floor"] == 1
    assert [r[0] for r in conn.execute("SELECT message_id FROM whatsapp_messages")] == ["m2"]


# --- calendar -----------------------------------------------------------------------------------


def _graph(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.0000000")


def _listed(event_id: str, start: datetime) -> dict:
    return {
        "Id": event_id,
        "Subject": f"Meeting {event_id}",
        "Start": {"DateTime": _graph(start), "TimeZone": "UTC"},
        "End": {"DateTime": _graph(start + timedelta(hours=1)), "TimeZone": "UTC"},
        "@odata.etag": 'W/"1"',
    }


@pytest.mark.parametrize("backfill", [False, True])
def test_calendar_sync_and_backfill_refuse_an_event_older_than_the_floor(
    store, monkeypatch, backfill
):
    from src import cli
    from src.export import calendar_export

    path, conn = store
    soon = datetime.now(UTC).replace(microsecond=0, tzinfo=None) + timedelta(days=1)
    old = datetime(2024, 6, 1, 10)
    _set(conn, calendar=CUTOFF)
    monkeypatch.setattr(
        calendar_export,
        "list_events",
        lambda since, until, failures=None, **k: [_listed("old", old), _listed("new", soon)],
    )
    fetched: list[str] = []

    def body(event_id):
        fetched.append(event_id)
        listed = _listed(event_id, soon if event_id == "new" else old)
        return {**listed, "Body": {"Content": ""}}

    monkeypatch.setattr(calendar_export, "get_event_body", body)
    cli.cmd_calendar_sync(
        types.SimpleNamespace(
            db=str(path), backfill=backfill, since=None, until=None, skip_extraction=True
        )
    )
    assert "old" not in fetched
    ids = [r[0] for r in conn.execute("SELECT outlook_event_id FROM calendar_events")]
    assert "old" not in ids
    assert forget_floor.SKIPPED["calendar"] >= 1


# --- conversations ------------------------------------------------------------------------------


def _transcript(session_id: str, ended_at: str) -> dict:
    return {
        "session_id": session_id,
        "started_at": ended_at,
        "ended_at": ended_at,
        "workspace": "w",
        "project_name": "p",
        "turn_count": 1,
        "turns": [{"speaker": "user", "content": "a", "timestamp": ended_at}],
    }


def test_the_conversation_export_stages_nothing_older_than_the_floor(tmp_path, monkeypatch):
    from src.export import conversation_export as ce

    transcripts = {"old.jsonl": _transcript("old", OLD), "new.jsonl": _transcript("new", NEW)}
    monkeypatch.setattr(ce, "CONVERSATION_STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(
        ce,
        "scan_conversation_files",
        lambda days=None, workspace_filter=None: list(map(Path, transcripts)),
    )
    monkeypatch.setattr(ce, "parse_conversation", lambda path: dict(transcripts[path.name]))
    result = ce.export_conversations(loaded={}, floor=CUTOFF)
    staged = [c for p in result["staged"] for c in json.loads(Path(p).read_text())["conversations"]]
    assert [c["session_id"] for c in staged] == ["new"]
    assert result["below_floor"] == 1


def test_the_conversation_load_refuses_one_older_than_the_floor(store):
    from src.store.loader import load_single_conversation

    _, conn = store
    _set(conn, conversation=CUTOFF)
    assert load_single_conversation(conn, _transcript("old", OLD), {"summary": "x"}) is False
    assert load_single_conversation(conn, _transcript("new", NEW), {"summary": "x"}) is True
    assert [r[0] for r in conn.execute("SELECT session_id FROM conversations")] == ["new"]
    assert forget_floor.SKIPPED["conversation"] == 1


def test_a_staged_conversation_older_than_the_floor_is_pruned(store, monkeypatch, tmp_path):
    from src.export.conversation_export import staged_path
    from src.store.loader import load_conversations

    path, conn = store
    _set(conn, conversation=CUTOFF)
    data = tmp_path / "data"
    monkeypatch.setattr("src.config.DATA_ROOT", data)
    staging = data / "staging" / "conversations"
    extracted = data / "extracted" / "conversations"
    staging.mkdir(parents=True)
    extracted.mkdir(parents=True)
    conv = _transcript("old", OLD)
    target = staged_path(staging, conv)
    target.write_text(json.dumps({"conversations": [conv]}))
    (extracted / "old.json").write_text(json.dumps({"summary": "x", "transcript_ended_at": OLD}))

    assert load_conversations(str(path)) == 0
    assert not target.exists()
