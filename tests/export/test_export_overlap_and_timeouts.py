"""The hourly export lists with an overlap, skips what is held, and survives a slow download.

The cursor is the newest ReceivedDateTime seen, and listing from it with no
overlap lost for good a message that turned visible after a newer one was
listed (held by a scanner, released from quarantine). Each run now lists from
six hours before the cursor and skips, before any get-mail, the messages the
store or a staging batch already holds.

A download-attachments call that outlived its timeout raised
subprocess.TimeoutExpired, which nothing caught: the run died before the cursor
save, so the folder re-listed a growing window every run and never got past the
one message. The timeout now counts against that message on the retry list and
the run goes on; anything still uncaught exits 5, "upstream misbehaved".
"""

import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from src.export import outlook_export
from src.export.state import OutlookSyncState, load_outlook_sync_state, save_outlook_sync_state
from src.store import sql_readonly
from src.store.schema import create_database

CURSOR = "2026-10-01T12:00:00Z"
LATER = "2026-10-01T13:00:00Z"


@pytest.fixture
def held(tmp_path, monkeypatch):
    """A store holding one message and an alias of another, and a staged third."""
    monkeypatch.setattr(sql_readonly, "REPLICA_STAMP", tmp_path / "absent-db-pull.stamp")
    db = tmp_path / "brain.db"
    conn = create_database(str(db))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, mailbox_name)"
        " VALUES ('AAMk-stored', '2026-10-01T10:00:00Z', 's', 'Archive')"
    )
    conn.execute(
        "INSERT INTO email_aliases (message_id, email_id, recorded_at)"
        " VALUES ('AAMk-alias', 1, '2026-10-01T10:00:00Z')"
    )
    conn.commit()
    conn.close()
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "batch-50001.json").write_text(
        json.dumps({"emails": [{"message_id": "AAMk-staged"}]}), encoding="utf-8"
    )
    return db, staging


@pytest.fixture
def export(tmp_path, held):
    """export(listed, downloads=None, retries=None): one hourly run of the Archive folder.

    `listed` is [(Graph id, ReceivedDateTime)]; `downloads` maps an id to "timeout".
    Every call outlook-cli receives is kept in .calls and staged ids in .staged.
    """
    db, staging = held
    path = tmp_path / "outlook_sync_archive.json"
    calls: list[list[str]] = []
    staged: list[str] = []

    def go(listed, downloads=None, retries=None):
        state = (
            load_outlook_sync_state(path)
            if path.exists()
            else OutlookSyncState(last_seen_received_at=CURSOR, folder="Archive")
        )
        if retries is not None:
            state.fetch_retries = retries
        save_outlook_sync_state(path, state)
        received = dict(listed)

        def cli(args, timeout_sec=60):
            calls.append(list(args))
            if args[0] == "auth-check":
                return {"ok": True}
            if args[0] == "list-mail":
                return [{"Id": i, "ReceivedDateTime": t} for i, t in listed]
            if args[0] == "get-mail":
                return {
                    "Id": args[1],
                    "ReceivedDateTime": received.get(args[1], CURSOR),
                    "HasAttachments": True,
                    "Body": {"Content": "x"},
                }
            if args[0] == "download-attachments":
                if (downloads or {}).get(args[1]) == "timeout":
                    raise subprocess.TimeoutExpired(cmd="outlook-cli", timeout=timeout_sec)
                return {"saved": []}
            raise AssertionError(f"unexpected call {args}")

        with (
            patch.object(outlook_export, "run_outlook_cli", cli),
            patch.object(
                outlook_export,
                "commit_messages_to_db",
                lambda messages, folder: staged.extend(m["Id"] for m in messages),
            ),
        ):
            return outlook_export.run_hourly_sync(
                state_path=path,
                folder="Archive",
                concurrency=1,
                db_path=db,
                staging_dir=staging,
            )

    go.calls = calls
    go.staged = staged
    go.state = lambda: load_outlook_sync_state(path)
    go.fetched = lambda: [c[1] for c in calls if c[0] == "get-mail"]
    return go


# --- Overlap and skip --------------------------------------------------------


def test_the_listing_starts_six_hours_before_the_cursor(export):
    export([])

    (listing,) = [c for c in export.calls if c[0] == "list-mail"]
    assert listing[listing.index("--since") + 1] == "2026-10-01T06:00:00Z"


def test_messages_the_store_or_staging_holds_are_not_fetched_again(export):
    listed = [(i, "2026-10-01T09:00:00Z") for i in ("AAMk-stored", "AAMk-alias", "AAMk-staged")]

    result = export([*listed, ("AAMk-new", LATER)])

    assert export.fetched() == ["AAMk-new"]
    assert export.staged == ["AAMk-new"]
    assert result["skipped"] == 3


def test_a_held_message_owed_a_retry_is_still_fetched(export):
    """A message whose attachments are still owed is staged already; skipping it
    as held would drop the retry for good."""
    retries = {"AAMk-stored": {"received": "2026-10-01T10:00:00Z", "attempts": 1}}

    export([("AAMk-stored", "2026-10-01T10:00:00Z")], retries=retries)

    assert export.fetched() == ["AAMk-stored"]
    assert export.state().fetch_retries == {}


def test_the_cursor_advances_when_everything_listed_is_held(export):
    export([("AAMk-stored", LATER)])

    assert export.fetched() == []
    assert export.state().last_seen_received_at == LATER


def test_the_cursor_never_moves_back(export):
    """With the overlap a run can list nothing newer than the cursor, as when the
    newest message has since left the folder."""
    export([("AAMk-older", "2026-10-01T09:00:00Z")])

    assert export.staged == ["AAMk-older"]
    assert export.state().last_seen_received_at == CURSOR


# --- Attachment download timeouts ---------------------------------------------


def test_a_slow_attachment_download_is_recorded_and_the_run_goes_on(export):
    result = export(
        [("AAMk-slow", "2026-10-01T12:30:00Z"), ("AAMk-fine", LATER)],
        downloads={"AAMk-slow": "timeout"},
    )

    assert result["status"] == "ok"
    downloads = [c[1] for c in export.calls if c[0] == "download-attachments"]
    assert sorted(downloads) == ["AAMk-fine", "AAMk-slow"]
    state = export.state()
    assert state.fetch_retries == {"AAMk-slow": {"received": "2026-10-01T12:30:00Z", "attempts": 1}}
    assert state.last_seen_received_at == LATER, "the cursor still advances"


def test_a_download_that_keeps_timing_out_is_given_up_after_the_cap(export):
    slow = {"AAMk-slow": "timeout"}
    export([("AAMk-slow", LATER)], downloads=slow)
    for _ in range(outlook_export.FETCH_MAX_ATTEMPTS - 1):
        export([], downloads=slow)

    state = export.state()
    assert "AAMk-slow" not in state.fetch_retries
    assert [g["id"] for g in state.fetch_gave_up] == ["AAMk-slow"]
    # When, so a health check can tell a fresh give-up from the kept history.
    assert state.fetch_gave_up[0]["gave_up_at"].endswith("Z")


def test_main_maps_an_uncaught_timeout_to_exit_5_and_keeps_the_cursor(tmp_path, held, monkeypatch):
    db, _ = held
    path = tmp_path / "outlook_sync_archive.json"
    save_outlook_sync_state(path, OutlookSyncState(last_seen_received_at=CURSOR, folder="Archive"))

    def cli(args, timeout_sec=60):
        if args[0] == "auth-check":
            return {"ok": True}
        raise subprocess.TimeoutExpired(cmd="outlook-cli", timeout=timeout_sec)

    monkeypatch.setattr(
        sys,
        "argv",
        ["outlook_export", "--folder", "Archive", "--state-path", str(path), "--db", str(db)],
    )
    with patch.object(outlook_export, "run_outlook_cli", cli):
        assert outlook_export.main() == 5

    state = load_outlook_sync_state(path)
    assert state.last_seen_received_at == CURSOR
    assert state.consecutive_failures == 1
