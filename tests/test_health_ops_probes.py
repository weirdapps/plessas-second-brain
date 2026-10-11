"""The nightly health check's operational probes (issue #152).

The report asserted recency and counts, so it could read "ALL SYSTEMS HEALTHY" while the
query path, one mail folder, attachment text, the backups or delivered mail degraded, and it
watched a hand-kept list of units that had already missed two of them. These pin each probe's
verdicts on a temporary store and canned `systemctl` output; nothing here touches a real
data directory, unit or network (the one loopback test is marked as such).
"""

import functools
import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

HEALTH_CHECK_PATH = Path(__file__).parent.parent / "scripts" / "health_check.py"
REPO = HEALTH_CHECK_PATH.parent.parent


@pytest.fixture
def hc():
    spec = importlib.util.spec_from_file_location("health_check", HEALTH_CHECK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _iso(delta: timedelta) -> str:
    """A UTC timestamp `delta` before now, in the Graph form the store keeps."""
    return (datetime.now(UTC) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local(delta: timedelta) -> str:
    """A naive local timestamp `delta` before now, as the producer writes exported_at."""
    return (datetime.now() - delta).isoformat()


# --- The unit list is derived from systemd, not kept by hand -------------------------------


def _systemctl(listing: str, states: dict[str, dict] | None = None, list_error=None):
    """A fake subprocess.run for `systemctl --user`: list-unit-files prints `listing`, show
    prints the unit's entry in `states` (a unit not in it is not-found)."""
    calls: list[list[str]] = []
    states = states or {}

    def run(cmd, **kwargs):
        calls.append(cmd)
        if "list-unit-files" in cmd:
            if list_error is not None:
                raise list_error
            return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr="")
        unit = cmd[3]
        props = states.get(unit, {"LoadState": "not-found", "ActiveState": "inactive"})
        out = "".join(f"{k}={v}\n" for k, v in props.items())
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    return run, calls


_IDLE = {"LoadState": "loaded", "ActiveState": "inactive", "Result": "success"}


def test_units_systemd_knows_are_watched_though_nobody_listed_them(hc, monkeypatch):
    listing = (
        "sb-outlook-sync.service static -\n"
        "sb-whatsapp-sync.service static -\n"
        "sb-mcp.service enabled enabled\n"
        "sb-brand-new.service static -\n"
        "telegram-brain.service enabled enabled\n"
    )
    states = {
        "sb-outlook-sync.service": _IDLE,
        "sb-whatsapp-sync.service": _IDLE,
        "sb-mcp.service": {"LoadState": "loaded", "ActiveState": "active", "Result": "success"},
        "sb-brand-new.service": {**_IDLE, "Description": "A job added after the list"},
        "telegram-brain.service": {
            "LoadState": "loaded",
            "ActiveState": "failed",
            "Result": "exit-code",
        },
    }
    run, _calls = _systemctl(listing, states)
    monkeypatch.setattr(hc, "IS_MACOS", False)
    monkeypatch.setattr(hc.subprocess, "run", run)

    jobs = hc.check_jobs()

    assert jobs["sb-whatsapp-sync.service"]["status"] == "OK"
    assert jobs["sb-mcp.service"]["status"] == "RUNNING"
    assert jobs["sb-brand-new.service"]["desc"] == "A job added after the list"
    assert jobs["telegram-brain.service"]["status"] == "FAIL(result=exit-code)"


def test_a_listed_unit_systemd_no_longer_has_is_reported_not_skipped(hc, monkeypatch):
    run, _calls = _systemctl(
        "sb-outlook-sync.service static -\n", {"sb-outlook-sync.service": _IDLE}
    )
    monkeypatch.setattr(hc, "IS_MACOS", False)
    monkeypatch.setattr(hc.subprocess, "run", run)

    jobs = hc.check_jobs()

    assert set(hc.SYSTEMD_UNITS) <= set(jobs)
    assert jobs["sb-daily-sync.service"]["status"] == "NOT_LOADED"
    report, issues = hc.build_report([], jobs, {}, {}, [])
    assert "Job Daily full sync: NOT_LOADED" in issues


def test_without_systemctl_the_hand_list_is_the_fallback(hc, monkeypatch):
    run, _calls = _systemctl("", list_error=FileNotFoundError("systemctl"))
    monkeypatch.setattr(hc, "IS_MACOS", False)
    monkeypatch.setattr(hc.subprocess, "run", run)

    jobs = hc.check_jobs()

    assert set(jobs) == set(hc.SYSTEMD_UNITS)


def test_timers_and_templates_in_the_listing_are_not_jobs(hc, monkeypatch):
    listing = "sb-teams-sync.timer enabled enabled\nsb-thing@.service static -\n"
    run, _calls = _systemctl(listing)
    monkeypatch.setattr(hc, "IS_MACOS", False)
    monkeypatch.setattr(hc.subprocess, "run", run)

    jobs = hc.check_jobs()

    assert not any(u.endswith(".timer") or "@" in u for u in jobs)


def test_the_whatsapp_job_has_a_log_age_signal(hc, tmp_path):
    assert "whatsapp_sync" in hc.check_sync_logs(log_dir=tmp_path)


# --- --fix restarts only the loader, and answers sentinels ----------------------------------


def _record_kicks(hc, monkeypatch):
    kicked = []

    def run(cmd, **kwargs):
        kicked.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(hc, "IS_MACOS", False)
    monkeypatch.setattr(hc.subprocess, "run", run)
    return kicked


def test_fix_leaves_a_failed_job_that_is_not_the_loader_to_its_own_timer(hc, monkeypatch):
    """A failed nightly attachment pass rerun at 23:50 runs two hours before its own 02:00."""
    kicked = _record_kicks(hc, monkeypatch)

    actions = hc.auto_fix([{"type": "job_failed", "label": "sb-attachments.service"}])

    assert actions == [] and kicked == []


def test_fix_still_restarts_a_failed_loader(hc, monkeypatch):
    kicked = _record_kicks(hc, monkeypatch)

    actions = hc.auto_fix([{"type": "job_failed", "label": hc.LOADER_JOB}])

    assert kicked == [hc.LOADER_JOB]
    assert actions == [f"Queued {hc.LOADER_JOB}"]


# --- The embedding index is read without pickle ---------------------------------------------


def _emails_only_db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE emails (id INTEGER PRIMARY KEY, summary TEXT)")
    return db


def test_an_index_that_needs_pickle_is_reported_not_unpickled(hc, tmp_path):
    npz = tmp_path / "embeddings.npz"
    np.savez(str(npz), ids=np.array([object()], dtype=object), vectors=np.zeros((1, 3)))

    result = hc.check_embeddings(_emails_only_db(), npz_path=npz)

    assert result["status"] == "WARN"
    assert "pickle" in result["note"]


def test_a_plain_index_still_loads(hc, tmp_path):
    npz = tmp_path / "embeddings.npz"
    np.savez(str(npz), ids=np.array([1], dtype=np.int64), vectors=np.zeros((1, 3), np.float32))

    assert hc.check_embeddings(_emails_only_db(), npz_path=npz)["embedded"] == 1


# --- (a) A golden query through the store's own search functions ----------------------------


def _store():
    from src.store.schema import create_database

    conn = create_database(":memory:")
    for email_id, summary in ((1, "steering committee minutes"), (2, "budget review")):
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, subject, summary) "
            "VALUES (?, ?, '2026-10-01', ?, ?)",
            (email_id, email_id, summary, summary),
        )
    conn.commit()
    return conn


def _index(tmp_path):
    p = tmp_path / "emb.npz"
    np.savez(
        str(p),
        ids=np.array([1, 2], dtype=np.int64),
        vectors=np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
    )
    return p


def _semantic(tmp_path, embed):
    from src.store.embeddings import query_semantic

    return functools.partial(query_semantic, embed_fn=embed, index_path=str(_index(tmp_path)))


def _embedded(_texts):
    return np.array([[1.0, 0.0]], dtype=np.float32)


def _refused(_texts):
    raise RuntimeError("the embedding service refused the model")


def test_golden_query_ok_when_both_halves_answer(hc, tmp_path):
    r = hc.check_golden_query(
        _store(), "steering committee", semantic=_semantic(tmp_path, _embedded)
    )

    assert r["status"] == "OK"
    assert r["keyword_rows"] >= 1 and r["semantic_rows"] >= 1
    assert r["embed_backend"]


def test_golden_query_is_stale_when_semantic_falls_back_to_keyword_matches(hc, tmp_path):
    """The September outage ran two days at "semantic: keyword_seeded" and nothing said so."""
    r = hc.check_golden_query(
        _store(), "steering committee", semantic=_semantic(tmp_path, _refused)
    )

    assert r["status"] == "STALE"
    assert "keyword_seeded" in r["note"]


def test_golden_query_is_stale_when_semantic_search_cannot_run_at_all(hc, tmp_path):
    r = hc.check_golden_query(_store(), "zeppelin", semantic=_semantic(tmp_path, _refused))

    assert r["status"] == "STALE"


def test_golden_query_warns_when_the_phrase_matches_nothing(hc, tmp_path):
    r = hc.check_golden_query(_store(), "zeppelin", semantic=_semantic(tmp_path, _embedded))

    assert r["status"] == "WARN"
    assert "BRAIN_HEALTH_QUERY" in r["note"]


def test_golden_query_warns_without_an_index(hc, tmp_path):
    from src.store.embeddings import query_semantic

    semantic = functools.partial(query_semantic, index_path=str(tmp_path / "absent.npz"))
    r = hc.check_golden_query(_store(), "steering committee", semantic=semantic)

    assert r["status"] == "WARN"


def test_golden_query_fails_when_keyword_search_breaks(hc, tmp_path):
    def broken(conn, phrase, limit):
        raise sqlite3.OperationalError("no such table: emails_fts")

    r = hc.check_golden_query(
        _store(), "steering committee", keyword=broken, semantic=_semantic(tmp_path, _embedded)
    )

    assert r["status"] == "FAIL"


def test_golden_query_is_not_applicable_to_an_empty_store(hc):
    from src.store.schema import create_database

    assert hc.check_golden_query(create_database(":memory:"), "anything")["status"] == "N/A"


def test_golden_phrase_comes_from_the_environment_then_the_owner(hc, monkeypatch):
    monkeypatch.setenv("BRAIN_HEALTH_QUERY", "quarterly results")
    assert hc.golden_phrase() == "quarterly results"
    monkeypatch.delenv("BRAIN_HEALTH_QUERY")
    assert hc.golden_phrase()  # the owner's role or name, else a word every mailbox holds


# --- (b) Per-folder mail lag ----------------------------------------------------------------


def _mail_db(rows, export_ok_at=None):
    """rows: (message_id, date_received, mailbox_name)."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE emails (message_id, date_received TEXT, mailbox_name TEXT)")
    db.executemany("INSERT INTO emails VALUES (?, ?, ?)", rows)
    db.execute("CREATE TABLE sync_metadata (key TEXT PRIMARY KEY, value TEXT)")
    if export_ok_at:
        db.execute("INSERT INTO sync_metadata VALUES ('mail_export_ok_at', ?)", (export_ok_at,))
    db.commit()
    return db


def _fresh_mail(**over):
    folders = {
        "Inbox": timedelta(hours=1),
        "Archive": timedelta(hours=1),
        "Sent Items": timedelta(hours=1),
        "News": timedelta(hours=1),
    }
    folders.update(over)
    return [(i + 1, _iso(age), name) for i, (name, age) in enumerate(folders.items())]


def test_folders_within_their_thresholds_are_ok(hc, tmp_path):
    r = hc.check_emails(_mail_db(_fresh_mail(**{"Sent Items": timedelta(hours=20)})), tmp_path)

    assert r["status"] == "OK"


def test_one_quiet_folder_is_stale_while_the_others_flow(hc, tmp_path):
    """A global MAX(date_received) stayed green while one folder's export returned nothing."""
    r = hc.check_emails(_mail_db(_fresh_mail(Archive=timedelta(hours=7))), tmp_path)

    assert r["status"] == "STALE"
    assert [f["folder"] for f in r["late"]] == ["Archive"]
    assert r["loader_stale"] is True


def test_sent_items_has_a_day_before_it_is_stale(hc, tmp_path):
    db = _mail_db(_fresh_mail(**{"Sent Items": timedelta(hours=25)}))

    assert hc.check_emails(db, tmp_path)["status"] == "STALE"


def test_triaged_inbox_is_fresh_while_its_export_succeeds(hc, tmp_path):
    """Triage relabels mail Archive, so the newest row still labelled Inbox is routinely a day old."""
    db = _mail_db(_fresh_mail(Inbox=timedelta(days=3)), export_ok_at=_iso(timedelta(hours=1)))

    assert hc.check_emails(db, tmp_path)["status"] == "OK"


def test_inbox_is_stale_when_its_export_stops_too(hc, tmp_path):
    db = _mail_db(_fresh_mail(Inbox=timedelta(days=3)), export_ok_at=_iso(timedelta(hours=9)))

    r = hc.check_emails(db, tmp_path)

    assert r["status"] == "STALE"
    assert [f["folder"] for f in r["late"]] == ["Inbox"]


def test_the_export_cursor_file_counts_for_the_inbox(hc, tmp_path):
    (tmp_path / "outlook_sync.json").write_text(
        json.dumps({"folder": "Inbox", "last_sync_completed_at": _iso(timedelta(minutes=20))})
    )
    db = _mail_db(_fresh_mail(Inbox=timedelta(days=3)))

    assert hc.check_emails(db, tmp_path)["status"] == "OK"


def _cursor_ahead_by(tmp_path, ahead: timedelta) -> None:
    future = (datetime.now(UTC) + ahead).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp_path / "outlook_sync_archive.json").write_text(
        json.dumps({"folder": "Archive", "last_seen_received_at": future})
    )


def test_an_export_that_lists_from_the_future_is_stale(hc, tmp_path):
    """Its export succeeds and finds nothing until the clock catches up, while its
    heartbeat stays fresh, and the mail it jumps over is never fetched."""
    from src.export.outlook_export import CURSOR_OVERLAP

    _cursor_ahead_by(tmp_path, CURSOR_OVERLAP + timedelta(hours=2))

    r = hc.check_emails(_mail_db(_fresh_mail()), tmp_path)

    assert r["status"] == "STALE"
    assert r["cursor_ahead"] == ["Archive"]


def test_a_cursor_inside_the_listing_overlap_still_lists_the_present(hc, tmp_path):
    """Each export lists from CURSOR_OVERLAP before its cursor, so a cursor a few hours
    ahead still reaches mail arriving now."""
    _cursor_ahead_by(tmp_path, timedelta(hours=3))

    r = hc.check_emails(_mail_db(_fresh_mail()), tmp_path)

    assert (r["status"], r["cursor_ahead"]) == ("OK", [])


def test_stale_news_alone_does_not_kick_the_mail_loader(hc, tmp_path):
    r = hc.check_emails(_mail_db(_fresh_mail(News=timedelta(hours=13))), tmp_path)

    assert r["status"] == "STALE"
    assert r["loader_stale"] is False


def test_folder_thresholds_are_configurable(hc, tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_HEALTH_FOLDER_LAG_HOURS", "Sent Items=48, Archive=0")
    db = _mail_db(_fresh_mail(**{"Sent Items": timedelta(hours=30), "Archive": timedelta(days=9)}))

    assert hc.check_emails(db, tmp_path)["status"] == "OK"


def test_a_store_without_folder_labels_keeps_the_global_threshold(hc, tmp_path):
    db = _mail_db([(1, _iso(timedelta(hours=7)), None)])

    assert hc.check_emails(db, tmp_path)["status"] == "STALE"


# --- (c) Attachment text and summary lag ----------------------------------------------------


def _attachment_db(pending_text=(), pending_summary=()):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE attachments (id INTEGER PRIMARY KEY, exported_at TEXT)")
    db.execute(
        "CREATE TABLE attachment_content (id INTEGER PRIMARY KEY, attachment_id INTEGER, "
        "extracted_text TEXT, extraction_status TEXT, extracted_at TEXT, llm_status TEXT)"
    )
    # One finished attachment, which neither queue may count.
    db.execute(
        "INSERT INTO attachments (id, exported_at) VALUES (1, ?)", (_local(timedelta(days=9)),)
    )
    db.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_status,"
        " extracted_at, llm_status) VALUES (1, 'text', 'extracted', ?, 'extracted')",
        (_local(timedelta(days=9)),),
    )
    for age in pending_text:
        db.execute("INSERT INTO attachments (exported_at) VALUES (?)", (_local(age),))
    for age in pending_summary:
        cur = db.execute(
            "INSERT INTO attachments (exported_at) VALUES (?)", (_local(age + timedelta(hours=1)),)
        )
        db.execute(
            "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_status,"
            " extracted_at, llm_status) VALUES (?, 'text', 'extracted', ?, 'pending')",
            (cur.lastrowid, _local(age)),
        )
    db.commit()
    return db


def test_attachment_lag_ok_without_a_backlog(hc):
    assert hc.check_attachment_lag(_attachment_db())["status"] == "OK"


def test_a_fresh_backlog_is_counted_not_warned(hc):
    r = hc.check_attachment_lag(
        _attachment_db(pending_text=[timedelta(hours=2)], pending_summary=[timedelta(hours=3)])
    )

    assert r["status"] == "OK"
    assert (r["text_pending"], r["summary_pending"]) == (1, 1)


def test_attachment_waiting_a_day_for_text_warns(hc):
    r = hc.check_attachment_lag(_attachment_db(pending_text=[timedelta(hours=30)]))

    assert r["status"] == "WARN"
    assert r["text_age"] > timedelta(hours=29)


def test_attachment_waiting_a_day_for_its_summary_warns(hc):
    r = hc.check_attachment_lag(_attachment_db(pending_summary=[timedelta(hours=30)]))

    assert r["status"] == "WARN"


def test_attachment_lag_never_reads_a_finished_attachment_s_text(hc, tmp_path):
    """The covering indexes keep the nightly check off the stored text (3 GB on the producer).
    Every finished row's text is made unreadable here, and the probe must still answer."""
    from src.store.schema import create_database

    path = tmp_path / "poisoned.db"
    conn = create_database(str(path))
    for i in range(20):
        att = conn.execute(
            "INSERT INTO attachments (message_id, filename, mime_type, file_size, file_path,"
            " exported_at) VALUES (?, 'f.pdf', 'application/pdf', 1, ?, '2026-10-01')",
            (f"d{i}", f"/data/attachments/d{i}/f.pdf"),
        ).lastrowid
        conn.execute(
            "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
            " extraction_status, extracted_at, summary, llm_status, llm_extracted_at)"
            " VALUES (?, ?, 'pymupdf', 'extracted', '2026-10-01T00:00:00', 's', 'extracted',"
            " '2026-10-01T00:00:00')",
            (att, "x" * 50_000),
        )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    try:
        pages = [
            r[0]
            for r in conn.execute(
                "SELECT pageno FROM dbstat WHERE name = 'attachment_content' AND pagetype = 'overflow'"
            )
        ]
    except sqlite3.OperationalError:
        pytest.skip("this SQLite has no dbstat table")
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()
    assert pages
    with open(path, "r+b") as fh:
        for pageno in pages:
            fh.seek((pageno - 1) * page_size)
            fh.write(b"\xff" * page_size)

    r = hc.check_attachment_lag(sqlite3.connect(str(path)))

    assert (r["status"], r["text_pending"], r["summary_pending"]) == ("OK", 0, 0)


# --- (d) Backup age and the last restore drill ----------------------------------------------


def _aged(path: Path, age: timedelta) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    then = time.time() - age.total_seconds()
    os.utime(path, (then, then))
    return path


def _backups(tmp_path, snapshot=timedelta(hours=16), archive=timedelta(hours=16)):
    local = tmp_path / "backups"
    _aged(local / "brain-20261010.db", snapshot)
    _aged(local / "brain-20261001.db", timedelta(days=9))
    if archive is not None:
        _aged(local / "offsite" / "brain-20261010.db.zst.enc", archive)
    return local


def test_fresh_backups_are_ok(hc, tmp_path):
    r = hc.check_backups(_backups(tmp_path), drill_stamp=tmp_path / "none")

    assert r["status"] == "OK"
    assert r["total"] == 2


def test_a_missed_snapshot_is_stale(hc, tmp_path):
    r = hc.check_backups(
        _backups(tmp_path, snapshot=timedelta(hours=30)), drill_stamp=tmp_path / "none"
    )

    assert r["status"] == "STALE"


def test_a_missed_encrypted_archive_is_stale(hc, tmp_path):
    r = hc.check_backups(
        _backups(tmp_path, archive=timedelta(hours=40)), drill_stamp=tmp_path / "none"
    )

    assert r["status"] == "STALE"
    assert "archive" in r["note"]


def test_an_empty_backups_directory_is_stale(hc, tmp_path):
    (tmp_path / "backups").mkdir()

    assert (
        hc.check_backups(tmp_path / "backups", drill_stamp=tmp_path / "none")["status"] == "STALE"
    )


def test_a_host_without_backups_is_not_judged(hc, tmp_path):
    assert hc.check_backups(tmp_path / "absent", drill_stamp=tmp_path / "none")["status"] == "N/A"


def test_a_host_without_the_key_has_no_archives_to_judge(hc, tmp_path):
    r = hc.check_backups(_backups(tmp_path, archive=None), drill_stamp=tmp_path / "none")

    assert r["status"] == "OK"


def test_an_old_restore_drill_warns(hc, tmp_path):
    stamp = tmp_path / "restore-drill.stamp"
    stamp.write_text(_iso(timedelta(days=40)) + "\n")

    r = hc.check_backups(_backups(tmp_path), drill_stamp=stamp)

    assert r["status"] == "WARN"
    assert r["drill_age"] > timedelta(days=39)


def test_a_recent_restore_drill_is_reported(hc, tmp_path):
    stamp = tmp_path / "restore-drill.stamp"
    stamp.write_text(_iso(timedelta(days=3)) + "\n")

    r = hc.check_backups(_backups(tmp_path), drill_stamp=stamp)

    assert r["status"] == "OK"
    assert timedelta(days=2) < r["drill_age"] < timedelta(days=4)


def test_a_replica_does_not_judge_the_producer_s_snapshots(hc, tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ROLE", "replica")
    monkeypatch.delenv("BRAIN_BACKUP_OFFSITE_DIR", raising=False)
    r = hc.check_backups(
        _backups(tmp_path, snapshot=timedelta(days=5)), drill_stamp=tmp_path / "none"
    )

    assert r["status"] == "N/A"


# --- (e) Mail the export gave up on, and staging batches set aside --------------------------


def _mail_state(
    tmp_path, gave_up=(0, 0), quarantined=0, loss=None, given_up_ago=timedelta(hours=5)
):
    """Cursor files whose give-ups are dated `given_up_ago` (None: undated, as before
    they were dated), a quarantine with `quarantined` batches, and the counts file."""
    state, staging = tmp_path / "state", tmp_path / "staging"
    state.mkdir()
    for name, n in zip(("outlook_sync.json", "outlook_sync_archive.json"), gave_up, strict=True):
        entries = [{"id": f"m{i}", "received": None, "attempts": 5} for i in range(n)]
        if given_up_ago is not None:
            for entry in entries:
                entry["gave_up_at"] = _iso(given_up_ago)
        (state / name).write_text(json.dumps({"folder": name, "fetch_gave_up": entries}))
    quarantine = staging / "quarantine"
    quarantine.mkdir(parents=True)
    for i in range(quarantined):
        (quarantine / f"batch-{i:05d}.json").write_text("{")
    if loss is not None:
        (state / "mail_loss.json").write_text(json.dumps(loss))
    return state, staging


def test_no_mail_loss_is_ok(hc, tmp_path):
    state, staging = _mail_state(tmp_path)

    assert hc.check_mail_loss(state, staging)["status"] == "OK"


def test_messages_given_up_on_warn(hc, tmp_path):
    state, staging = _mail_state(tmp_path, gave_up=(2, 1))

    r = hc.check_mail_loss(state, staging)

    assert r["status"] == "WARN"
    assert r["fetch_gave_up"] == 3


def test_a_give_up_weeks_old_is_history_not_a_warning(hc, tmp_path):
    """The cursor keeps its give-ups for good, so the count never falls back to zero:
    warned on for ever, it would be a WARN nothing can clear."""
    state, staging = _mail_state(tmp_path, gave_up=(2, 1), given_up_ago=timedelta(days=30))

    r = hc.check_mail_loss(state, staging)

    assert (r["status"], r["fetch_gave_up"]) == ("OK", 3)
    assert "3 messages given up" in r["note"]


def test_give_ups_from_before_they_were_dated_are_history(hc, tmp_path):
    state, staging = _mail_state(tmp_path, gave_up=(1, 0), given_up_ago=None)

    assert hc.check_mail_loss(state, staging)["status"] == "OK"


def test_quarantined_batches_warn(hc, tmp_path):
    state, staging = _mail_state(tmp_path, quarantined=2)

    r = hc.check_mail_loss(state, staging)

    assert r["status"] == "WARN"
    assert r["quarantined"] == 2


def test_the_nightly_counts_file_is_preferred_while_fresh(hc, tmp_path):
    loss = {
        "updated_at": _iso(timedelta(hours=3)),
        "fetch_gave_up": 4,
        "quarantined": 0,
        "last_gave_up_at": _iso(timedelta(days=2)),
    }
    state, staging = _mail_state(tmp_path, loss=loss)

    r = hc.check_mail_loss(state, staging)

    assert (r["status"], r["fetch_gave_up"], r["source"]) == ("WARN", 4, "mail_loss.json")


def test_a_stale_counts_file_gives_way_to_the_state_files(hc, tmp_path):
    loss = {"updated_at": _iso(timedelta(hours=60)), "fetch_gave_up": 9, "quarantined": 9}
    state, staging = _mail_state(tmp_path, loss=loss)

    r = hc.check_mail_loss(state, staging)

    assert (r["status"], r["fetch_gave_up"], r["quarantined"]) == ("OK", 0, 0)


def test_a_host_without_export_state_is_not_judged(hc, tmp_path):
    assert hc.check_mail_loss(tmp_path / "absent", tmp_path / "absent")["status"] == "N/A"


# --- Report placement: STALE probes reach the freshness block -------------------------------


def test_stale_probes_land_in_the_block_the_freshness_ping_reads(hc):
    checks = [
        {"name": "Golden query", "status": "STALE", "note": "semantic fell back"},
        {"name": "Backups", "status": "STALE", "note": "no snapshot"},
    ]
    report, issues = hc.build_report(checks, {}, {}, {}, [])

    block = report.split("DATA SOURCES", 1)[1].split("SCHEDULED JOBS", 1)[0]
    assert "Golden query" in block and "Backups" in block
    assert {"Golden query: STALE", "Backups: STALE"} <= set(issues)
    ok, _ = hc.freshness_verdict(checks, {})
    assert ok is False


def test_the_new_probes_run_in_the_nightly_report(hc):
    import inspect

    src = inspect.getsource(hc.main)
    for call in (
        "check_golden_query()",
        "check_attachment_lag(db)",
        "check_backups()",
        "check_mail_loss()",
        "check_mcp_stats()",
    ):
        assert call in src, f"{call} is defined but the nightly report never runs it"


# --- The HTTP MCP server answers stats --------------------------------------------------------

TOKEN = "k" * 48


def _token(tmp_path, mode=0o600) -> Path:
    p = tmp_path / "mcp-token"
    p.write_text(TOKEN + "\n")
    p.chmod(mode)
    return p


class _Response:
    def __init__(self, status, headers, body):
        self.status, self._body = status, body
        self.headers = headers

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sse(message: dict) -> bytes:
    return f"event: message\ndata: {json.dumps(message)}\n\n".encode()


class _Server:
    """Answers the streamable-HTTP exchange the probe makes, recording every request."""

    def __init__(self, stats=None, status=200):
        self.requests = []
        self.stats = (
            stats
            if stats is not None
            else {"age_hours": 1.2, "stale": False, "embed_backend": "gemini"}
        )
        self.status = status

    def open(self, request, timeout=None):
        headers = {k.lower(): v for k, v in request.header_items()}
        body = json.loads(request.data) if request.data else None
        self.requests.append((request.get_method(), headers, body))
        if self.status != 200:
            raise urllib.error.HTTPError(request.full_url, self.status, "nope", {}, None)
        if request.get_method() == "DELETE":
            return _Response(200, {}, b"")
        if body["method"] == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {},
                "serverInfo": {"name": "t", "version": "1"},
            }
            return _Response(
                200,
                {"Content-Type": "text/event-stream", "mcp-session-id": "s-1"},
                _sse({"jsonrpc": "2.0", "id": body["id"], "result": result}),
            )
        if "id" not in body:
            return _Response(202, {}, b"")
        result = {
            "content": [{"type": "text", "text": json.dumps(self.stats)}],
            "structuredContent": self.stats,
            "isError": False,
        }
        return _Response(
            200,
            {"Content-Type": "text/event-stream"},
            _sse({"jsonrpc": "2.0", "id": body["id"], "result": result}),
        )


def test_mcp_probe_is_not_applicable_where_no_http_server_is_configured(hc, tmp_path):
    r = hc.check_mcp_stats(token_file=tmp_path / "absent", opener=_Server())

    assert r["status"] == "N/A"


def test_mcp_probe_ok_when_stats_answers_fresh(hc, tmp_path):
    server = _Server()

    r = hc.check_mcp_stats(token_file=_token(tmp_path), opener=server)

    assert r["status"] == "OK"
    assert r["embed_backend"] == "gemini"
    methods = [b["method"] if b else m for m, _h, b in server.requests]
    assert methods == ["initialize", "notifications/initialized", "tools/call", "DELETE"]
    assert all(h["authorization"] == f"Bearer {TOKEN}" for _m, h, _b in server.requests)
    assert all(h.get("mcp-session-id") == "s-1" for _m, h, _b in server.requests[1:])


def test_mcp_probe_fails_when_the_token_is_refused(hc, tmp_path):
    r = hc.check_mcp_stats(token_file=_token(tmp_path), opener=_Server(status=401))

    assert r["status"] == "FAIL"
    assert TOKEN not in json.dumps(r, default=str)


def test_mcp_probe_fails_when_nothing_listens(hc, tmp_path):
    class Refused:
        def open(self, request, timeout=None):
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    assert hc.check_mcp_stats(token_file=_token(tmp_path), opener=Refused())["status"] == "FAIL"


def test_mcp_probe_fails_on_a_token_file_others_can_read(hc, tmp_path):
    r = hc.check_mcp_stats(token_file=_token(tmp_path, mode=0o644), opener=_Server())

    assert r["status"] == "FAIL"
    assert "chmod 600" in r["note"]


def test_mcp_probe_is_stale_when_the_data_behind_it_is(hc, tmp_path):
    server = _Server(stats={"age_hours": 9.5, "stale": True, "embed_backend": "gemini"})

    assert hc.check_mcp_stats(token_file=_token(tmp_path), opener=server)["status"] == "STALE"


def test_mcp_probe_tolerates_the_overnight_gap(hc, tmp_path):
    """stats calls data stale after 3 h; the mail timer sleeps six hours every night."""
    server = _Server(stats={"age_hours": 5.5, "stale": True, "embed_backend": "gemini"})

    assert hc.check_mcp_stats(token_file=_token(tmp_path), opener=server)["status"] == "OK"


def test_hourly_mcp_probe_pings_its_own_check_and_never_prints_the_token(
    hc, tmp_path, monkeypatch, capsys
):
    pinged = []

    def run(cmd, **kwargs):
        pinged.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setenv("HC_PING_URL", "https://hc.example.invalid/ping/key")
    monkeypatch.setattr(hc.subprocess, "run", run)

    rc = hc.run_mcp_probe(ping=True, token_file=_token(tmp_path), opener=_Server(status=401))

    assert rc == 0
    assert pinged == ["https://hc.example.invalid/ping/key/sb-mcp-stats/fail?create=1"]
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    assert "FAIL" in out.out


def test_hourly_mcp_probe_does_not_ping_without_the_flag(hc, tmp_path, monkeypatch):
    pinged = []
    monkeypatch.setenv("HC_PING_URL", "https://hc.example.invalid/ping/key")
    monkeypatch.setattr(hc.subprocess, "run", lambda cmd, **kw: pinged.append(cmd))

    assert hc.run_mcp_probe(ping=False, token_file=_token(tmp_path), opener=_Server()) == 0
    assert pinged == []


def _free_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.allow_network
def test_mcp_probe_against_a_real_server_on_loopback(hc, tmp_path, monkeypatch):
    """The fakes above speak the protocol as this file understands it; the real server is the
    only judge of that. Loopback only, like test_mcp_http's end-to-end test."""
    from src.store.schema import create_database

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = create_database(str(data_dir / "brain.db"))
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', ?)",
        (datetime.now().isoformat(),),
    )
    conn.commit()
    conn.close()
    token_file = _token(tmp_path)
    port = _free_loopback_port()
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    env = {**os.environ, "BRAIN_MCP_TOKEN_FILE": str(token_file), "BRAIN_DATA_DIR": str(data_dir)}
    log = tmp_path / "server.log"
    with log.open("wb") as err:
        server = subprocess.Popen(
            [sys.executable, "-m", "src.mcp_server", "--http", f"127.0.0.1:{port}"],
            cwd=REPO,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=err,
        )
    try:
        deadline = time.monotonic() + 20
        while True:
            assert server.poll() is None, log.read_text()
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                assert time.monotonic() < deadline, "not listening after 20 s\n" + log.read_text()
                time.sleep(0.1)

        r = hc.check_mcp_stats(url=f"http://127.0.0.1:{port}/mcp", token_file=token_file)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
    assert r["status"] == "OK", r
    assert r["age_hours"] is not None
