"""The nightly health check reports yesterday's model spend from the usage log.

Nothing read llm-usage-YYYY-MM.jsonl, so a $2,680 day (2026-10-04) raised no alarm (audit
2026-10-11). The check prices yesterday's calls by site, warns above a daily threshold, and says
STALE when the store shows model work that day but the log holds no call: a usage log that
stopped being written would otherwise read as a cheap day forever.
"""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.store.schema import create_database

HEALTH_CHECK_PATH = Path(__file__).resolve().parent.parent / "scripts" / "health_check.py"
NOW = datetime(2026, 10, 11, 20, 50, tzinfo=UTC)  # the nightly run; "yesterday" is 2026-10-10
SITE = "src.extract.attachment_pipeline._complete_and_parse"


@pytest.fixture
def hc():
    spec = importlib.util.spec_from_file_location("health_check", HEALTH_CHECK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


def _line(ts, site=SITE, model="claude-sonnet-5-5", i=1_000_000, o=100_000):
    return json.dumps(
        {
            "ts": ts,
            "site": site,
            "model": model,
            "stop_reason": "end_turn",
            "input_tokens": i,
            "output_tokens": o,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
    )


def _log(log_dir, *lines):
    (log_dir / "llm-usage-2026-10.jsonl").write_text("\n".join(lines) + "\n")


def test_yesterdays_calls_tokens_and_cost_by_site(hc, db, tmp_path):
    _log(
        tmp_path,
        _line("2026-10-09T12:00:00+00:00"),
        _line("2026-10-10T01:00:00+00:00"),
        _line("2026-10-10T02:00:00+00:00"),
        _line("2026-10-10T03:00:00+00:00", site="src.extract.claude_extract.extract_one", o=0),
        _line("2026-10-11T19:00:00+00:00"),
    )

    row = hc.check_llm_spend(db, log_dir=tmp_path, now=NOW, warn_usd=60)

    assert (row["name"], row["status"], row["day"], row["total"]) == (
        "LLM spend",
        "OK",
        "2026-10-10",
        3,
    )
    assert row["cost_usd"] == pytest.approx(2 * 3.30 + 2.20)
    assert row["sites"][SITE]["calls"] == 2
    assert row["age"] == timedelta(hours=1, minutes=50)  # since the newest logged call


def test_spend_above_the_threshold_warns(hc, db, tmp_path):
    _log(tmp_path, *[_line(f"2026-10-10T{h:02d}:00:00+00:00") for h in range(20)])  # $66

    row = hc.check_llm_spend(db, log_dir=tmp_path, now=NOW, warn_usd=60)

    assert row["status"] == "WARN"
    assert "60" in row["note"]


def test_the_threshold_defaults_to_60_and_comes_from_the_environment(hc, db, tmp_path, monkeypatch):
    _log(tmp_path, *[_line(f"2026-10-10T{h:02d}:00:00+00:00") for h in range(4)])  # $13.20

    monkeypatch.delenv("BRAIN_DAILY_SPEND_WARN_USD", raising=False)
    assert hc.check_llm_spend(db, log_dir=tmp_path, now=NOW)["status"] == "OK"
    monkeypatch.setenv("BRAIN_DAILY_SPEND_WARN_USD", "10")
    assert hc.check_llm_spend(db, log_dir=tmp_path, now=NOW)["status"] == "WARN"


def test_a_model_with_no_rate_warns(hc, db, tmp_path):
    _log(tmp_path, _line("2026-10-10T01:00:00+00:00", model="claude-opus-5-5"))

    row = hc.check_llm_spend(db, log_dir=tmp_path, now=NOW, warn_usd=60)

    assert row["status"] == "WARN" and row["unpriced_calls"] == 1


def _summarised_attachment(db, when):
    db.execute(
        "INSERT INTO attachments (message_id, filename, file_path, exported_at)"
        " VALUES (1, 'a.pdf', '/x/a.pdf', '2026-10-10')"
    )
    db.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_status,"
        " extracted_at, llm_status, llm_extracted_at, summary)"
        " VALUES (1, 'text', 'extracted', ?, 'extracted', ?, 'a summary')",
        (when, when),
    )
    db.commit()


def test_a_day_with_model_work_and_no_logged_call_is_stale(hc, db, tmp_path):
    _log(tmp_path, _line("2026-10-09T12:00:00+00:00"))
    _summarised_attachment(db, "2026-10-10T14:00:00.123456")

    row = hc.check_llm_spend(db, log_dir=tmp_path, now=NOW, warn_usd=60)

    assert row["status"] == "STALE" and row["total"] == 0
    assert "1 attachment" in row["note"]


def test_mail_summarised_that_day_counts_as_model_work(hc, db, tmp_path):
    db.execute(
        "INSERT INTO emails (message_id, date_received, subject, summary, mailbox_name)"
        " VALUES (5, '2026-10-10T09:00:00Z', 's', 'a summary', 'Inbox')"
    )
    db.commit()

    row = hc.check_llm_spend(db, log_dir=tmp_path, now=NOW, warn_usd=60)

    assert row["status"] == "STALE"


def test_a_quiet_day_with_no_log_is_ok(hc, db, tmp_path):
    db.execute(
        "INSERT INTO emails (message_id, date_received, subject, summary, mailbox_name)"
        " VALUES (5, '2026-10-10T09:00:00Z', 's', 'a digest', 'News')"
    )
    db.commit()

    row = hc.check_llm_spend(db, log_dir=tmp_path, now=NOW, warn_usd=60)

    assert (row["status"], row["total"]) == ("OK", 0)


def test_a_replica_is_not_applicable_whatever_log_it_holds(hc, db, tmp_path, monkeypatch):
    """The replica's copy shows the producer's work; a log of its own would read as STALE."""
    monkeypatch.setattr(hc, "is_replica", lambda: True)
    _summarised_attachment(db, "2026-10-10T14:00:00")
    _log(tmp_path, _line("2026-10-09T12:00:00+00:00"))

    row = hc.check_llm_spend(db, log_dir=tmp_path, now=NOW, warn_usd=60)

    assert row["status"] == "N/A"


def test_an_unreadable_log_is_a_warn_row_not_a_crash(hc, db, tmp_path):
    """main() runs the checks unguarded, so an exception here would take down the whole report."""
    (tmp_path / "llm-usage-2026-10.jsonl").mkdir()

    row = hc.check_llm_spend(db, log_dir=tmp_path, now=NOW, warn_usd=60)

    assert row["status"] == "WARN"
    assert "unreadable" in hc.llm_spend_detail(row)


def test_the_report_prints_the_row_and_the_sites(hc, db, tmp_path):
    _log(
        tmp_path,
        *[_line(f"2026-10-10T{h:02d}:00:00+00:00") for h in range(20)],
        _line("2026-10-10T21:00:00+00:00", site="src.extract.claude_extract.extract_one"),
    )
    row = hc.check_llm_spend(db, log_dir=tmp_path, now=NOW, warn_usd=60)

    text, issues = hc.build_report([row], {}, {}, {}, [])

    spend = next(line for line in text.splitlines() if line.strip().startswith("LLM spend"))
    assert "$69.30" in spend and "WARN" in spend
    assert "LLM spend: WARN" in issues
    sites = text[text.index("LLM SPEND BY SITE") :]
    first, second = (line for line in sites.splitlines()[2:4])
    assert "attachment_pipeline._complete_and_parse" in first and "$66.00" in first
    assert "claude_extract.extract_one" in second
    assert not {chr(0x2014), chr(0x2013)} & set(spend + sites)


def test_main_runs_the_check():
    assert "check_llm_spend(db)," in HEALTH_CHECK_PATH.read_text()
