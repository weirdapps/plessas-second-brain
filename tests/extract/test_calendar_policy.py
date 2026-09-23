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
from pathlib import Path
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
    "@odata.etag": 'W/"change-1"',
    "Attendees": [
        {
            "EmailAddress": {"Address": "chair@example.com", "Name": "Chair"},
            "Status": {"Response": "Organizer"},
        }
    ],
}

# WHAT list-calendar ACTUALLY RETURNS: a SUBSET, without Attendees or ResponseStatus. That
# is why cmd_calendar_sync issues a second get-event call per changed event at all, and it
# is the whole reason a failed fetch must write nothing rather than write 'pending' — the
# event dict still in hand at that point is this one, and load_event replaces the attendee
# rows wholesale from it. A fixture that returned the full event from both calls would make
# that difference invisible, and did: it let "write pending on a fetch failure" survive
# mutation testing until the fixture was corrected.
#
# And no LastModifiedDateTime: list-calendar's $select is
# Id,Subject,Start,End,Organizer,Location,IsAllDay. This fixture used to keep it, which
# is how a change detector keyed on modified_at passed here while never matching in
# production, so every event was fetched and re-extracted on every run. The etag comes
# back with every entry whatever the $select, and changes whenever the event does.
_LIST_EVENT = {
    k: v
    for k, v in _RAW_EVENT.items()
    if k in ("Id", "Subject", "Start", "End", "Organizer", "Location", "@odata.etag")
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


def _attendee_count(db_path: str) -> int:
    """Attendees are the facts a failed fetch would silently destroy.

    load_event replaces the attendee rows wholesale, and the list-calendar entry the
    caller still holds after a failed get-event carries none — so a row rebuilt from it
    would come back with zero.
    """
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM event_attendees").fetchone()[0]
    conn.close()
    return n


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


def _run_sync(
    monkeypatch,
    db_path: str,
    body: str,
    extract,
    fetches: list | None = None,
    *,
    outlook_down: bool = False,
    skip_extraction: bool = False,
):
    """One cmd_calendar_sync run over one unchanged event, with the Outlook calls stubbed.

    Patched at their SOURCE modules: cmd_calendar_sync imports them inside the function
    body, so the name it binds is looked up at call time and the source module is the
    interception point.

    ``outlook_down`` returns None from get_event_body, which is exactly what the real one
    does on any error — it catches everything and logs. That indistinguishability from an
    empty body is the bug the three-run tests below are about.
    """
    from src import cli
    from src.export import calendar_export
    from src.extract import calendar_extractor as extractor

    def _body(event_id):
        if fetches is not None:
            fetches.append(event_id)
        if outlook_down:
            return None
        return {**_RAW_EVENT, "Body": {"Content": body}}

    monkeypatch.setattr(
        calendar_export, "list_events", lambda since, until, failures=None: [_LIST_EVENT]
    )
    monkeypatch.setattr(calendar_export, "get_event_body", _body)
    monkeypatch.setattr(extractor, "extract_event", extract)
    args = _sync_args(db_path)
    args.skip_extraction = skip_extraction
    cli.cmd_calendar_sync(args)


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


def test_every_sync_run_repairs_stacked_duplicates_and_self_flags(monkeypatch, tmp_path):
    """Both repairs run even when every event is unchanged, which is the point:
    the rows they fix belong to events no upsert will ever touch again."""
    import src.config

    monkeypatch.setattr(src.config, "USER_EMAIL_PATTERN", "owner@example.com")
    db_path = _calendar_db(tmp_path)

    def succeeding(event, body):
        return {"body_summary": "s", "decisions": [{"decision": "Go"}], "action_items": []}

    _run_sync(monkeypatch, db_path, _LONG_BODY, succeeding)
    conn = sqlite3.connect(db_path)
    event_id = conn.execute("SELECT id FROM calendar_events").fetchone()[0]
    conn.execute("INSERT INTO decisions (decision, event_id) VALUES ('Go', ?)", (event_id,))
    conn.execute("UPDATE event_attendees SET is_self = 1")
    conn.commit()
    conn.close()

    _run_sync(monkeypatch, db_path, _LONG_BODY, succeeding)

    conn = sqlite3.connect(db_path)
    decisions = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE event_id = ?", (event_id,)
    ).fetchone()[0]
    selves = conn.execute("SELECT COUNT(*) FROM event_attendees WHERE is_self = 1").fetchone()[0]
    attendees = conn.execute("SELECT COUNT(*) FROM event_attendees").fetchone()[0]
    conn.close()
    assert (decisions, selves) == (1, 0)
    assert attendees == 1


def test_an_empty_owner_pattern_leaves_the_stored_flags_alone(monkeypatch, tmp_path, capsys):
    """A run without the pattern (a manual run under another HOME, a host missing
    its settings file) would otherwise rewrite every event and attendee to
    not-self. It skips the refresh and says so."""
    import src.config

    monkeypatch.setattr(src.config, "USER_EMAIL_PATTERN", "")
    db_path = _calendar_db(tmp_path)
    _run_sync(
        monkeypatch,
        db_path,
        _LONG_BODY,
        lambda event, body: {"body_summary": "s", "decisions": [], "action_items": []},
    )
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE event_attendees SET is_self = 1")
    conn.commit()
    conn.close()

    _run_sync(
        monkeypatch,
        db_path,
        _LONG_BODY,
        lambda event, body: {"body_summary": "s", "decisions": [], "action_items": []},
    )

    conn = sqlite3.connect(db_path)
    selves = conn.execute("SELECT COUNT(*) FROM event_attendees WHERE is_self = 1").fetchone()[0]
    conn.close()
    assert selves == 1
    assert "BRAIN_USER_EMAIL_PATTERN is unset" in capsys.readouterr().err


def test_an_unparseable_re_extraction_keeps_the_previous_decisions(monkeypatch, tmp_path):
    """A re-extraction whose reply is not JSON (truncated at max_tokens, or with
    prose around it) must not be recorded as 'extracted', or the replace-on-
    extraction rule would delete the event's decisions for nothing."""
    monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", tmp_path / "needs_gcloud_reauth")
    db_path = _calendar_db(tmp_path)
    _run_sync(
        monkeypatch,
        db_path,
        _LONG_BODY,
        lambda event, body: {
            "body_summary": "s",
            "decisions": [{"decision": "Go"}],
            "action_items": [],
        },
    )
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE calendar_events SET llm_status = 'pending'")  # re-offer it
    conn.commit()
    conn.close()

    def garbage(event, body):
        return calendar_extractor.parse_extraction_response('{"body_summary": "x", "decisions": [')

    _run_sync(monkeypatch, db_path, _LONG_BODY, garbage)

    conn = sqlite3.connect(db_path)
    decisions = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    status = conn.execute("SELECT llm_status FROM calendar_events").fetchone()[0]
    conn.close()
    assert (decisions, status) == (1, "failed")


def test_a_run_that_lost_part_of_the_window_exits_non_zero(monkeypatch, tmp_path):
    """sb-calendar-sync reported success whatever happened, so its dead-man
    switch pinged green while chunks, bodies or extractions failed."""
    from src import cli
    from src.export import calendar_export

    db_path = _calendar_db(tmp_path)

    def one_chunk_failed(since, until, failures=None):
        if failures is not None:
            failures.append("2026-08-01..2026-09-01: outlook-cli timed out")
        return []

    monkeypatch.setattr(calendar_export, "list_events", one_chunk_failed)

    assert cli.cmd_calendar_sync(_sync_args(db_path)) == 1


def test_a_clean_run_exits_zero(monkeypatch, tmp_path):
    from src import cli
    from src.export import calendar_export

    db_path = _calendar_db(tmp_path)
    monkeypatch.setattr(calendar_export, "list_events", lambda since, until, failures=None: [])

    assert cli.cmd_calendar_sync(_sync_args(db_path)) == 0


def test_a_failed_body_fetch_or_extraction_makes_the_run_fail(monkeypatch, tmp_path):
    """Both are already recorded so the next run re-offers or reports them; the
    exit code is what tells the scheduler this run did not do its job."""
    monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", tmp_path / "needs_gcloud_reauth")
    from src import cli
    from src.export import calendar_export

    db_path = _calendar_db(tmp_path)
    monkeypatch.setattr(
        calendar_export, "list_events", lambda since, until, failures=None: [_LIST_EVENT]
    )
    monkeypatch.setattr(calendar_export, "get_event_body", lambda event_id: None)

    assert cli.cmd_calendar_sync(_sync_args(db_path)) == 1


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


def _failing_auth(event, body):
    raise gauth.RefreshError("invalid_grant: Bad Request")


def _succeeding(event, body):
    return {"body_summary": "Reviewed the quarter", "decisions": [], "action_items": []}


def test_a_failed_body_fetch_does_not_discharge_the_extraction_debt(monkeypatch, tmp_path):
    """THREE runs, because the loss only becomes visible on the third.

    The reviewer's sequence, and it is a real one: sb-auth-watch.sh fires calendar-sync on
    `gcloud_or_outlook`, so the run dispatched to recover from an outage is precisely the
    one that can execute while the OTHER dependency is still down.

      run 1  Outlook up, gcloud dead   -> 'pending', correctly
      run 2  gcloud back, Outlook down -> get_event_body returns None, which used to be
                                          indistinguishable from an empty body: blank
                                          body_html, past the 50-char gate, 'skipped'
      run 3  both healthy              -> nothing owed, never re-offered, summary gone

    A two-run test cannot see this. Run two's write looks locally reasonable — the event
    has no body as far as the code can tell — and only run three reveals that the debt was
    silently discharged by a failure.

    Would this pass with the behaviour removed? No. Restore the old
    ``if body_raw and isinstance(body_raw, dict)`` and let the loop fall through: run two
    writes 'skipped' and the run-two assertion fails; even without it, run three never
    calls the extractor and ``attempts`` reads 1 instead of 2. Writing 'pending' on the
    fetch failure instead of writing nothing passes the status assertions but fails
    ``attendees``: the row would be rebuilt from the list-calendar subset, which carries no
    Attendees, wiping the ones already stored.
    """
    monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", tmp_path / "needs_gcloud_reauth")
    db_path = _calendar_db(tmp_path)
    attempts = []

    def failing(event, body):
        attempts.append(event)
        return _failing_auth(event, body)

    def succeeding(event, body):
        attempts.append(event)
        return _succeeding(event, body)

    # Run 1 — Outlook up, gcloud dead.
    _run_sync(monkeypatch, db_path, _LONG_BODY, failing)
    assert _row(db_path)["llm_status"] == "pending"
    attendees_after_run_1 = _attendee_count(db_path)
    assert attendees_after_run_1 == 1

    # Run 2 — gcloud back, Outlook down. The debt must survive a failure that says nothing
    # about whether an extraction is owed.
    _run_sync(monkeypatch, db_path, _LONG_BODY, succeeding, outlook_down=True)
    assert _row(db_path)["llm_status"] == "pending"
    assert _attendee_count(db_path) == attendees_after_run_1, (
        "a failed fetch must not rebuild the row from the list-calendar subset"
    )
    assert len(attempts) == 1, "no body, so nothing to extract on run two"

    # Run 3 — both healthy. The event must still be re-offered.
    _run_sync(monkeypatch, db_path, _LONG_BODY, succeeding)
    assert len(attempts) == 2, "run three must re-offer the event"
    assert _row(db_path)["llm_status"] == "extracted"
    assert _row(db_path)["body_summary"] == "Reviewed the quarter"


def test_skip_extraction_does_not_discharge_a_debt_it_did_not_look_at(monkeypatch, tmp_path):
    """Same shape, second route in: a run told not to extract must not record 'skipped'.

    'skipped' means "nothing was owed". A --skip-extraction run has no basis for that
    claim: it did not look. Writing it would discharge a debt an earlier run recorded and
    the event would never be re-offered, which is the fetch-failure loss reached through a
    flag instead of an outage.

    Latent in production today, because sb-calendar-sync.sh runs the command bare. Closed
    because it is the same line and the same mistake.

    Would this pass with the behaviour removed? No. Drop the ``prior_status == "pending"``
    arm and run two writes 'skipped', so run three finds nothing owed, never calls the
    extractor, and ``attempts`` reads 1 rather than 2.
    """
    monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", tmp_path / "needs_gcloud_reauth")
    db_path = _calendar_db(tmp_path)
    attempts = []

    def failing(event, body):
        attempts.append(event)
        return _failing_auth(event, body)

    def succeeding(event, body):
        attempts.append(event)
        return _succeeding(event, body)

    _run_sync(monkeypatch, db_path, _LONG_BODY, failing)
    assert _row(db_path)["llm_status"] == "pending"

    _run_sync(monkeypatch, db_path, _LONG_BODY, succeeding, skip_extraction=True)
    assert _row(db_path)["llm_status"] == "pending"
    assert len(attempts) == 1

    _run_sync(monkeypatch, db_path, _LONG_BODY, succeeding)
    assert len(attempts) == 2
    assert _row(db_path)["llm_status"] == "extracted"


def test_a_genuinely_short_body_still_discharges_the_debt(monkeypatch, tmp_path):
    """The other side of the same boundary, so the fix is not "never downgrade pending".

    A SUCCESSFUL fetch returning a short body is authoritative: the event really has
    nothing worth summarising now, whatever it had before. 'skipped' is the honest record
    and the event must stop being re-offered, or a body that shrank would be fetched
    forever.

    Would this pass with the behaviour removed? No. Preserving 'pending' whenever nothing
    was attempted — the simpler, blunter version of the two fixes above — leaves this row
    'pending' and ``fetches`` reads 2 because run three re-offers it again.
    """
    monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", tmp_path / "needs_gcloud_reauth")
    db_path = _calendar_db(tmp_path)
    fetches: list[str] = []

    def failing(event, body):
        return _failing_auth(event, body)

    def never_called(event, body):
        raise AssertionError("extract_event must not run on a sub-50-char body")

    _run_sync(monkeypatch, db_path, _LONG_BODY, failing)
    assert _row(db_path)["llm_status"] == "pending"

    _run_sync(monkeypatch, db_path, "too short", never_called, fetches)
    assert _row(db_path)["llm_status"] == "skipped"

    _run_sync(monkeypatch, db_path, "too short", never_called, fetches)
    assert len(fetches) == 1, "an authoritative empty body must stop the re-offering"


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


def _sync_rc(monkeypatch, db_path, list_rows, body_fn, extract=_succeeding) -> int:
    """One cmd_calendar_sync run over ``list_rows``; returns its exit code."""
    from src import cli
    from src.export import calendar_export
    from src.extract import calendar_extractor as extractor

    monkeypatch.setattr(
        calendar_export, "list_events", lambda since, until, failures=None: list(list_rows)
    )
    monkeypatch.setattr(calendar_export, "get_event_body", body_fn)
    monkeypatch.setattr(extractor, "extract_event", extract)
    return cli.cmd_calendar_sync(_sync_args(db_path))


def test_an_edited_event_is_fetched_and_extracted_again(monkeypatch, tmp_path):
    """The etag is what says the event changed, since the list carries no
    modified time. A new etag re-offers the event even after a clean extraction."""
    db_path = _calendar_db(tmp_path)
    fetches: list[str] = []

    def body(event_id):
        fetches.append(event_id)
        return {**_RAW_EVENT, "Body": {"Content": _LONG_BODY}}

    _sync_rc(monkeypatch, db_path, [_LIST_EVENT], body)
    _sync_rc(monkeypatch, db_path, [_LIST_EVENT], body)
    assert len(fetches) == 1, "an unchanged etag must not be fetched again"

    edited = {**_LIST_EVENT, "@odata.etag": 'W/"change-2"'}
    _sync_rc(monkeypatch, db_path, [edited], body)
    assert len(fetches) == 2

    # The row keeps the LIST entry's etag, not get-event's (here still the old
    # one): that is what the next run compares against.
    _sync_rc(monkeypatch, db_path, [edited], body)
    assert len(fetches) == 2


def test_a_list_row_without_an_etag_is_never_taken_as_unchanged(monkeypatch, tmp_path):
    """No etag on either side must not read as "same etag": that would freeze
    every event after its first load. Rows stored before the column existed
    have none either."""
    db_path = _calendar_db(tmp_path)
    fetches: list[str] = []

    def body(event_id):
        fetches.append(event_id)
        return {**_RAW_EVENT, "Body": {"Content": "short"}}

    bare = {k: v for k, v in _LIST_EVENT.items() if k != "@odata.etag"}
    _sync_rc(monkeypatch, db_path, [bare], body)
    _sync_rc(monkeypatch, db_path, [bare], body)

    assert len(fetches) == 2


def test_a_permanent_extraction_failure_fails_one_run_not_every_run(monkeypatch, tmp_path):
    """The failed event is not re-offered, so it cannot turn every later run red.
    With the old detector it was re-extracted and re-counted on every run."""
    monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", tmp_path / "needs_gcloud_reauth")
    db_path = _calendar_db(tmp_path)

    def body(event_id):
        return {**_RAW_EVENT, "Body": {"Content": _LONG_BODY}}

    def broken(event, body):
        raise ValueError("bad json on line 5")

    rcs = [_sync_rc(monkeypatch, db_path, [_LIST_EVENT], body, broken) for _ in range(3)]

    assert rcs == [1, 0, 0]


def test_without_an_etag_a_failure_already_on_record_is_not_counted_again(monkeypatch, tmp_path):
    """With no etag every event is offered again, so a broken one is retried on
    every run. It fails the run that first records it, not every run after."""
    monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", tmp_path / "needs_gcloud_reauth")
    db_path = _calendar_db(tmp_path)
    attempts = []

    def body(event_id):
        return {**_RAW_EVENT, "Body": {"Content": _LONG_BODY}}

    def broken(event, body):
        attempts.append(event)
        raise ValueError("bad json on line 5")

    bare = {k: v for k, v in _LIST_EVENT.items() if k != "@odata.etag"}
    rcs = [_sync_rc(monkeypatch, db_path, [bare], body, broken) for _ in range(3)]

    assert (rcs, len(attempts)) == ([1, 0, 0], 3)


def test_an_unfetchable_event_stops_failing_the_run_after_three_runs(monkeypatch, tmp_path):
    """It is still retried every run and still logged, but after three runs in a
    row it no longer counts against the exit code: one event that get-event can
    never return must not keep the unit red for as long as it sits in the window."""
    db_path = _calendar_db(tmp_path)
    fetches: list[str] = []
    healthy = {**_LIST_EVENT, "Id": "AAMkAGI2healthy="}

    def one_unfetchable(event_id):
        fetches.append(event_id)
        if event_id == _EVENT_ID:
            return None
        return {**_RAW_EVENT, "Id": event_id, "Body": {"Content": "short"}}

    rcs = [
        _sync_rc(monkeypatch, db_path, [healthy, _LIST_EVENT], one_unfetchable) for _ in range(5)
    ]

    assert rcs == [1, 1, 1, 0, 0]
    assert fetches.count(_EVENT_ID) == 5


def test_when_no_fetch_has_worked_every_failure_keeps_counting(monkeypatch, tmp_path):
    """A get-event that fails for everything must not go green after three runs:
    that is an outage, not one bad event."""
    db_path = _calendar_db(tmp_path)

    def broken(event_id):
        return None

    rcs = [_sync_rc(monkeypatch, db_path, [_LIST_EVENT], broken) for _ in range(5)]

    assert rcs == [1, 1, 1, 1, 1]


def test_the_cap_lapses_once_the_last_good_fetch_is_a_day_old(monkeypatch, tmp_path):
    import json

    db_path = _calendar_db(tmp_path)
    state = Path(db_path).parent / "state" / "calendar_fetch_failures.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps({"events": {_EVENT_ID: 5}, "last_fetch_ok": "2020-01-01T00:00:00+00:00"})
    )

    def broken(event_id):
        return None

    assert _sync_rc(monkeypatch, db_path, [_LIST_EVENT], broken) == 1


def test_a_successful_fetch_resets_the_unfetchable_count(monkeypatch, tmp_path):
    db_path = _calendar_db(tmp_path)

    def unfetchable(event_id):
        return None

    def fetchable(event_id):
        return {**_RAW_EVENT, "Body": {"Content": "short"}}

    for _ in range(4):
        _sync_rc(monkeypatch, db_path, [_LIST_EVENT], unfetchable)
    _sync_rc(monkeypatch, db_path, [_LIST_EVENT], fetchable)
    edited = {**_LIST_EVENT, "@odata.etag": 'W/"change-2"'}

    assert _sync_rc(monkeypatch, db_path, [edited], unfetchable) == 1


def test_an_outlook_session_that_expires_mid_run_stops_and_exits_4(monkeypatch, tmp_path):
    """The run stops asking (every later fetch would fail the same way), leaves
    the rows alone for the next run, and exits 4, the shared re-authenticate
    code, as it does when the session is already dead at list time."""
    from src.export.outlook_cli import OutlookCliAuthRequired

    db_path = _calendar_db(tmp_path)
    fetches: list[str] = []

    def expired(event_id):
        fetches.append(event_id)
        raise OutlookCliAuthRequired("session expired")

    second = {**_LIST_EVENT, "Id": "AAMkAGI2second="}
    rc = _sync_rc(monkeypatch, db_path, [_LIST_EVENT, second], expired)

    assert rc == 4
    assert len(fetches) == 1, "the rest of the run is deferred, not hammered"
    assert _row(db_path) is None


def test_an_outlook_session_dead_at_list_time_exits_4(monkeypatch, tmp_path):
    from src import cli
    from src.export import calendar_export
    from src.export.outlook_cli import OutlookCliAuthRequired

    db_path = _calendar_db(tmp_path)

    def expired(since, until, failures=None):
        raise OutlookCliAuthRequired("session expired")

    monkeypatch.setattr(calendar_export, "list_events", expired)

    assert cli.cmd_calendar_sync(_sync_args(db_path)) == 4


def test_a_transient_extraction_failure_is_retried_by_the_next_run(monkeypatch, tmp_path):
    """A 5xx, a timeout or a quota error is the service's, not the event's.
    Recorded 'failed', it was never offered again once the change detector
    worked; 'pending' re-offers it. The run still fails, so the outage shows."""
    monkeypatch.setattr(vertex_auth, "GCLOUD_SENTINEL", tmp_path / "needs_gcloud_reauth")
    db_path = _calendar_db(tmp_path)
    attempts = []

    def body(event_id):
        return {**_RAW_EVENT, "Body": {"Content": _LONG_BODY}}

    def unavailable(event, body):
        attempts.append(event)
        raise ConnectionError("503 Service Unavailable")

    def succeeding(event, body):
        attempts.append(event)
        return _succeeding(event, body)

    assert _sync_rc(monkeypatch, db_path, [_LIST_EVENT], body, unavailable) == 1
    assert _row(db_path)["llm_status"] == "pending"

    assert _sync_rc(monkeypatch, db_path, [_LIST_EVENT], body, succeeding) == 0
    assert len(attempts) == 2
    assert _row(db_path)["llm_status"] == "extracted"


def _heartbeat(db_path):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM sync_metadata WHERE key = 'calendar_last_listed'"
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    conn.close()
    return row[0] if row else None


def test_a_quiet_calendar_still_leaves_a_heartbeat(monkeypatch, tmp_path):
    """With a working change detector an unchanged calendar writes no event row,
    so MAX(ingested_at) stopped proving the sync ran. The run stamps its own."""
    db_path = _calendar_db(tmp_path)

    def body(event_id):
        return {**_RAW_EVENT, "Body": {"Content": "short"}}

    _sync_rc(monkeypatch, db_path, [_LIST_EVENT], body)
    first = _heartbeat(db_path)
    _sync_rc(monkeypatch, db_path, [_LIST_EVENT], body)  # unchanged: no event row written

    assert first is not None
    assert _heartbeat(db_path) >= first


def test_no_heartbeat_when_part_of_the_window_could_not_be_listed(monkeypatch, tmp_path):
    from src import cli
    from src.export import calendar_export

    db_path = _calendar_db(tmp_path)

    def one_chunk_failed(since, until, failures=None):
        failures.append("2026-08-01..2026-09-01: outlook-cli timed out")
        return []

    monkeypatch.setattr(calendar_export, "list_events", one_chunk_failed)
    cli.cmd_calendar_sync(_sync_args(db_path))

    assert _heartbeat(db_path) is None


def test_skip_extraction_leaves_a_long_body_owed(monkeypatch, tmp_path):
    """A run told not to extract has no basis for recording that nothing was
    owed, and with the etag stored a 'skipped' row would never be offered again."""
    db_path = _calendar_db(tmp_path)

    _run_sync(monkeypatch, db_path, _LONG_BODY, _succeeding, skip_extraction=True)

    assert _row(db_path)["llm_status"] == "pending"


def test_an_unwritable_state_dir_does_not_fail_the_run(monkeypatch, tmp_path, capsys):
    """The state file is bookkeeping for the fetch-failure cap. The rows are
    already committed, so failing to write it must not turn the run red."""
    db_path = _calendar_db(tmp_path)
    state_dir = Path(db_path).parent / "state"
    state_dir.mkdir()
    state_dir.chmod(0o500)

    def body(event_id):
        return {**_RAW_EVENT, "Body": {"Content": "short"}}

    try:
        rc = _sync_rc(monkeypatch, db_path, [_LIST_EVENT], body)
    finally:
        state_dir.chmod(0o700)

    assert rc == 0
    err = capsys.readouterr().err
    assert "calendar_fetch_failures" in err
