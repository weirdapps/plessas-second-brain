"""scripts/health_check.py: is the WhatsApp push alive, judged by its own stamp.

The push runs on a laptop, so a night asleep or a weekend away is its normal
state. Stale for a day is a WARN in the nightly report; only a week without a
push, or a push that reports failing, is STALE, which fails the freshness ping.
"""

import importlib.util
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "health_check.py"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def hc():
    spec = importlib.util.spec_from_file_location("health_check_whatsapp", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stamp(tmp_path, age: timedelta) -> Path:
    path = tmp_path / "whatsapp-sync.stamp"
    path.write_text((NOW - age).strftime("%Y-%m-%dT%H:%M:%S+00:00") + "\n")
    return path


def _check(hc, db, tmp_path, stamp=None, fail=None):
    return hc.check_whatsapp(
        db,
        snapshot=tmp_path / "absent-snapshot.db",
        stamp=stamp or tmp_path / "absent.stamp",
        fail_marker=fail or tmp_path / "absent.fail",
        now=NOW,
    )


def test_a_fresh_push_is_ok(hc, db, tmp_path):
    assert _check(hc, db, tmp_path, _stamp(tmp_path, timedelta(hours=1)))["status"] == "OK"


def test_a_day_without_a_push_is_a_warning_not_a_page(hc, db, tmp_path):
    out = _check(hc, db, tmp_path, _stamp(tmp_path, timedelta(hours=30)))
    assert out["status"] == "WARN"
    assert not out["stale"]


def test_a_week_without_a_push_is_stale(hc, db, tmp_path):
    assert _check(hc, db, tmp_path, _stamp(tmp_path, timedelta(days=8)))["status"] == "STALE"


def test_a_failing_push_is_stale_and_says_why(hc, db, tmp_path):
    fail = tmp_path / "whatsapp-sync.fail"
    fail.write_text((NOW - timedelta(minutes=5)).isoformat() + "\nsnapshot build failed (rc=66)\n")
    out = _check(hc, db, tmp_path, _stamp(tmp_path, timedelta(hours=2)), fail)
    assert out["status"] == "STALE"
    assert "rc=66" in out["note"]


def test_a_push_never_seen_is_a_warning(hc, db, tmp_path):
    out = _check(hc, db, tmp_path)
    assert out["status"] == "WARN"
    assert "never" in out["note"]


def test_it_reports_what_the_store_holds(hc, db, tmp_path):
    db.execute(
        "INSERT INTO whatsapp_chats (chat_jid, name, chat_kind, first_seen_at) "
        "VALUES ('c@g.us', 'Chat A', 'group', '2026-09-01T00:00:00Z')"
    )
    db.execute(
        "INSERT INTO whatsapp_messages (chat_id, chat_jid, message_id, sent_at, content) "
        "VALUES (1, 'c@g.us', 'm1', '2026-09-28T10:00:00Z', 'hello')"
    )
    out = _check(hc, db, tmp_path, _stamp(tmp_path, timedelta(hours=1)))
    assert out["total"] == 1
    assert out["latest"] == "2026-09-28T10:00:00Z"


def test_embedding_coverage_counts_whatsapp_sessions(hc, db):
    db.execute(
        "INSERT INTO whatsapp_chats (chat_jid, name, chat_kind, first_seen_at) "
        "VALUES ('c@g.us', 'Chat A', 'group', '2026-09-01T00:00:00Z')"
    )
    db.execute(
        "INSERT INTO whatsapp_threads (chat_id, anchor_message_id, started_at, ended_at, "
        "summary, extraction_status) VALUES (1, 'a', '2026-09-01T00:00:00Z', "
        "'2026-09-01T00:00:00Z', 's', 'extracted')"
    )
    assert hc._embedding_coverage(db, [])["whatsapp"] == (0, 1)


def test_the_nightly_report_runs_it(hc):
    assert "check_whatsapp(db)" in inspect.getsource(hc.main)
