"""What a calendar extraction failure costs: a retry, not the event.

Two layers, and they fail for different reasons.

``extract_event`` retries in-process. An auth error a reauth can cure never reaches the
caller at all — that is call_with_policy doing its job, and the first test pins it.

``cmd_calendar_sync`` catches whatever survives that. Before this branch it caught the
exception, kept the empty default extraction and upserted the event with its NEW
modified_at, so the next run's change detector scored it ``skipped_unchanged`` and the
body_summary, decisions and action items were gone for good — no sentinel, no row to retry
from, nothing anywhere recording that an extraction was owed. On sb-calendar-sync, a 300s
unit whose 90s budget cannot fund a token-push wait, Linux answers the FIRST auth error
with UNRECOVERABLE_AUTH, so every event that changed in that five-minute window was written
blank and never revisited.

Now the facts still go in — they come from the Graph API, not from the model, and they were
never in doubt — but the absence of an extraction is recorded as a debt rather than as a
result. ``calendar_events.llm_status`` carries it, mirroring
``attachment_content.llm_status``, and ``pending`` is the one value that makes the change
detector re-offer an event whose modified_at has not moved.
"""

import sqlite3
import types
from unittest.mock import patch

import google.auth.exceptions as gauth
import pytest

from src.extract import calendar_extractor, claude_extract, vertex_auth
from src.llm_policy import ReauthResult
from src.store.schema import run_migrations

# 60 chars > the 50-char minimum that triggers an LLM call in extract_event.
_LONG_BODY = "A" * 60


def test_one_bad_event_is_recovered_rather_than_dropped(monkeypatch):
    """One auth error triggers reauth; the second call succeeds.

    Before the fix: RefreshError escapes from extract_event with no retry.
    cmd_calendar_sync's per-event try/except catches it — pre-existing, and now also the
    thing that records the debt — so the run continues and the cost is the event, not the
    sync. That guard is why this test asserts on extract_event's own return value; the
    run-level cost is the next test's subject.
    After the fix: call_with_policy retries after reauth and returns a result.

    running_on_linux is pinned False so decide() chooses REAUTH_RETRY (macOS
    path) and does not give up immediately, as it would on a Linux VPS where
    UNRECOVERABLE_AUTH fires on the first auth failure.

    Mutation checks:
    - Remove call_with_policy (bare create): RefreshError propagates,
      assert result is not None fails (exception, not a dict).
    - Remove the retry (loop exits after 1): len(calls) == 1, not 2.
    """
    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                raise gauth.RefreshError("invalid_grant: Bad Request")
            return type(
                "R",
                (),
                {
                    "stop_reason": "end_turn",
                    "content": [type("C", (), {"text": "{}"})()],
                },
            )()

    fake = type("Client", (), {"messages": FakeMessages()})()
    # Patch _get_client_and_model at calendar_extractor's module level (it is a
    # module-level import there), so _do_call picks up the fake client on each attempt.
    monkeypatch.setattr(calendar_extractor, "_get_client_and_model", lambda: (fake, "m"))
    # Pin the platform so decide() uses the macOS budget (REAUTH_RETRY, not UNRECOVERABLE_AUTH).
    monkeypatch.setattr(claude_extract, "running_on_linux", lambda: False)
    with patch.object(claude_extract, "reauth", return_value=ReauthResult.SUCCEEDED):
        result = calendar_extractor.extract_event({"id": "e1", "subject": "s"}, body=_LONG_BODY)

    assert result is not None
    assert len(calls) == 2


# ------------------------------------------------ what a surviving failure costs the run

_EVENT_ID = "AAMkAGI2example="

# Shaped like one entry of outlook-cli's list-calendar output, so the real parse_event runs
# over it rather than over a hand-built internal dict.
_RAW_EVENT = {
    "Id": _EVENT_ID,
    "Subject": "Quarterly review",
    "Organizer": {"EmailAddress": {"Address": "chair@example.com", "Name": "Chair"}},
    "Start": {"DateTime": "2026-08-12T09:00:00"},
    "End": {"DateTime": "2026-08-12T10:00:00"},
    "Location": {"DisplayName": "Room 4"},
    "ResponseStatus": {"Response": "Accepted"},
    "CreatedDateTime": "2026-08-01T09:00:00Z",
    "LastModifiedDateTime": "2026-08-10T09:00:00Z",
    "Attendees": [
        {
            "EmailAddress": {"Address": "chair@example.com", "Name": "Chair"},
            "Status": {"Response": "Organizer"},
        }
    ],
}


def _calendar_db(tmp_path) -> str:
    """A v12 database, migrated up. The same shape tests/test_calendar_loader.py builds.

    Starting at 12 rather than at CURRENT_SCHEMA_VERSION is what makes run_migrations
    actually build the calendar tables, and then apply the llm_status migration on top.
    """
    db_path = tmp_path / "calendar.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, email TEXT,
                             role TEXT, department TEXT);
        CREATE TABLE decisions (id INTEGER PRIMARY KEY, email_id INTEGER,
                                decision TEXT NOT NULL, decided_by TEXT, decision_date TEXT);
        CREATE TABLE action_items (id INTEGER PRIMARY KEY, email_id INTEGER, task TEXT NOT NULL,
                                   owner TEXT, deadline TEXT, status TEXT DEFAULT 'open');
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (12);
        """
    )
    conn.commit()
    run_migrations(conn)
    conn.close()
    return str(db_path)


def _sync_args(db_path: str):
    return types.SimpleNamespace(
        db=db_path, backfill=False, since=None, until=None, skip_extraction=False
    )


def _row(db_path: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT subject, start_at, body_summary, body_extracted_at, llm_status "
        "FROM calendar_events WHERE outlook_event_id = ?",
        (_EVENT_ID,),
    ).fetchone()
    conn.close()
    return row


def _run_sync(monkeypatch, db_path: str, body: str, extract, fetches: list | None = None):
    """One cmd_calendar_sync run over one unchanged event, with the Outlook calls stubbed.

    Patched at their SOURCE modules: cmd_calendar_sync imports them inside the function
    body, so the name it binds is looked up at call time and the source module is the
    interception point.
    """
    from src import cli
    from src.export import calendar_export
    from src.extract import calendar_extractor as extractor

    def _body(event_id):
        if fetches is not None:
            fetches.append(event_id)
        return {**_RAW_EVENT, "Body": {"Content": body}}

    monkeypatch.setattr(calendar_export, "list_events", lambda since, until: [_RAW_EVENT])
    monkeypatch.setattr(calendar_export, "get_event_body", _body)
    monkeypatch.setattr(extractor, "extract_event", extract)
    cli.cmd_calendar_sync(_sync_args(db_path))


def test_a_failed_extraction_leaves_the_facts_and_is_retried_by_the_next_run(monkeypatch, tmp_path):
    """THE property, and the one a single-run test cannot see.

    Run one fails; run two must ATTEMPT THE EVENT AGAIN even though nothing about it
    changed in Outlook. That second attempt is the whole fix. The bug was invisible from
    the first run's write, which looked like a reasonable "store what we have", and showed
    itself only on the next tick, when the event was scored unchanged and skipped.

    Would this pass with the behaviour removed? No, and three separate mutations break it:

      * Restore the modified_at-only guard. Run two matches, scores skipped_unchanged, and
        ``attempts`` reads 1 rather than 2 — the regression itself.
      * Record 'extracted', 'skipped' or 'failed' on an auth failure. Same outcome: the
        guard's llm_status test does not fire and run two never asks.
      * Stop calling load_event on the failure path — the tempting "do not write a blank
        row" reading of the fix. The two facts assertions on run one fail: the ruling is
        that the API's own data is good and is stored either way.
    """
    monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", tmp_path / "needs_gcloud_reauth")
    db_path = _calendar_db(tmp_path)
    attempts = []

    def failing(event, body):
        attempts.append(event)
        raise gauth.RefreshError("invalid_grant: Bad Request")

    def succeeding(event, body):
        attempts.append(event)
        return {"body_summary": "Reviewed the quarter", "decisions": [], "action_items": []}

    _run_sync(monkeypatch, db_path, _LONG_BODY, failing)

    after_failure = _row(db_path)
    # The facts are in, from the API and not from the model.
    assert after_failure["subject"] == "Quarterly review"
    assert after_failure["start_at"] == "2026-08-12T09:00:00"
    # The extraction is not, and the row says so rather than passing an empty result off
    # as a real one.
    assert not after_failure["body_summary"]
    assert after_failure["body_extracted_at"] is None
    assert after_failure["llm_status"] == "pending"
    assert (tmp_path / "needs_gcloud_reauth").exists()

    _run_sync(monkeypatch, db_path, _LONG_BODY, succeeding)

    assert len(attempts) == 2, "the second run must re-offer the event, not skip it"
    after_retry = _row(db_path)
    assert after_retry["body_summary"] == "Reviewed the quarter"
    assert after_retry["llm_status"] == "extracted"


def test_a_permanent_failure_is_recorded_but_not_retried_forever(monkeypatch, tmp_path):
    """The auth-versus-permanent split, decided by the same classifier attachments use.

    sb-calendar-sync runs every few minutes. An event whose body reliably breaks the
    extractor would burn one LLM call per run, forever, inside a 90s budget, and would
    never get better on its own: a re-auth is not the remedy for a parse error.

    Would this pass with the behaviour removed? No. Marking every failure 'pending', the
    simpler reading of "leave it retryable", makes run two re-offer it and ``attempts``
    reads 2. The row stays queryable as 'failed', which is what a human acts on.
    """
    monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", tmp_path / "needs_gcloud_reauth")
    db_path = _calendar_db(tmp_path)
    attempts = []

    def failing(event, body):
        attempts.append(event)
        raise ValueError("bad json on line 5")

    _run_sync(monkeypatch, db_path, _LONG_BODY, failing)
    assert _row(db_path)["llm_status"] == "failed"
    # A permanent failure is not an auth outage, so it must not raise the reauth sentinel.
    assert not (tmp_path / "needs_gcloud_reauth").exists()

    _run_sync(monkeypatch, db_path, _LONG_BODY, failing)
    assert len(attempts) == 1


def test_an_event_with_nothing_to_extract_is_not_re_offered(monkeypatch, tmp_path):
    """'skipped' is why the vocabulary has four values rather than three.

    Most invites have no body worth summarising, so this is the COMMON case, not an edge
    one, and it has to be distinguishable from both a result and a failure. Recorded as
    'extracted' it would claim a summary that was never produced; recorded as 'pending' it
    would put the majority of the calendar on an infinite retry loop.

    Would this pass with the behaviour removed? No. Initialising llm_status to 'pending'
    rather than 'skipped' — the natural default if you read it as "not done yet" —
    re-offers this event on every run and ``fetches`` reads 2.
    """
    db_path = _calendar_db(tmp_path)
    fetches: list[str] = []

    def never_called(event, body):
        raise AssertionError("extract_event must not run on a sub-50-char body")

    _run_sync(monkeypatch, db_path, "too short", never_called, fetches)
    assert _row(db_path)["llm_status"] == "skipped"

    _run_sync(monkeypatch, db_path, "too short", never_called, fetches)
    assert len(fetches) == 1


def test_load_event_refuses_a_status_outside_the_vocabulary(tmp_path):
    """A typo reads as "not pending", so the event would silently never be retried again.

    Would this pass with the behaviour removed? No. Dropping the guard writes 'extacted'
    into the column and nothing is raised.
    """
    from src.store.calendar_loader import load_event

    db_path = _calendar_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with pytest.raises(ValueError, match="llm_status must be one of"):
        load_event(
            conn,
            {
                "outlook_event_id": "x",
                "start_at": "2026-08-12T09:00:00",
                "end_at": "2026-08-12T10:00:00",
            },
            {},
            llm_status="extacted",
        )
    conn.close()
