"""One rate table, a run's token budget, and the usage log read back as spend.

Nothing read llm-usage-YYYY-MM.jsonl, and nothing capped a run: on 2026-10-04 one day of
attachment summaries cost about $2,680 and no alarm fired (audit 2026-10-11). The rates are
Vertex AI's `eu` multi-region for the one model the deployment's allowlist accepts, and a cost is
derived from the tokens when it is read, never stored.
"""

import json
import threading
from datetime import UTC, date, datetime

import pytest

from src import llm_cost
from src.llm_cost import (
    DEFAULT_CHARS_PER_TOKEN,
    RATES,
    OverBudget,
    TokenBudget,
    configured_chars_per_token,
    cost_usd,
    newest_usage_at,
    rates_for,
    usage_for_day,
)


class _Usage:
    def __init__(self, i, o, read=0, write=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = read
        self.cache_creation_input_tokens = write


def test_the_rate_table_is_sonnet_5_5_on_vertex_eu():
    r = RATES["claude-sonnet-5-5"]
    assert (r.input, r.output, r.batch_input, r.batch_output, r.cache_read) == (
        2.20,
        11.00,
        1.10,
        5.50,
        0.11,
    )


def test_cost_is_tokens_times_rate_per_million():
    r = RATES["claude-sonnet-5-5"]
    assert cost_usd(r, input_tokens=1_000_000, output_tokens=1_000_000) == pytest.approx(13.20)
    assert cost_usd(r, input_tokens=1_000_000, output_tokens=1_000_000, batch=True) == (
        pytest.approx(6.60)
    )
    assert cost_usd(r, cache_read_tokens=1_000_000) == pytest.approx(0.11)


@pytest.mark.parametrize(
    "model",
    ["claude-sonnet-5-5", "claude-sonnet-5-5@20261001", "claude-sonnet-5-5[1m]"],
)
def test_a_model_is_priced_whatever_suffix_it_carries(model):
    assert rates_for(model) is RATES["claude-sonnet-5-5"]


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5", "", None])
def test_a_model_without_a_rate_is_unpriced(model):
    assert rates_for(model) is None


def test_characters_per_token_default_and_override(monkeypatch):
    monkeypatch.delenv("BRAIN_CHARS_PER_TOKEN", raising=False)
    assert configured_chars_per_token() == DEFAULT_CHARS_PER_TOKEN == 1.6
    monkeypatch.setenv("BRAIN_CHARS_PER_TOKEN", "2.5")
    assert configured_chars_per_token() == 2.5


@pytest.mark.parametrize("bad", ["abc", "0", "-1", "nan", "inf"])
def test_a_bad_characters_per_token_falls_back_to_the_default(monkeypatch, bad):
    monkeypatch.setenv("BRAIN_CHARS_PER_TOKEN", bad)
    assert configured_chars_per_token() == DEFAULT_CHARS_PER_TOKEN


# --- TokenBudget --------------------------------------------------------------


def test_a_call_is_estimated_from_its_prompt_and_an_answer():
    budget = TokenBudget(100_000, chars_per_token=1.6, output_tokens=1_500)
    assert budget.estimate(1_600) == 1_000 + 1_500


def test_a_finished_call_is_charged_what_its_response_reports():
    budget = TokenBudget(100_000, chars_per_token=1.6, output_tokens=1_500)

    held = budget.reserve(1_600)
    budget.settle(held, _Usage(900, 300, read=50, write=25))

    assert (budget.spent, budget.calls) == (1_275, 1)


def test_a_call_whose_response_reports_nothing_is_charged_its_estimate():
    budget = TokenBudget(100_000, chars_per_token=1.6, output_tokens=1_500)

    budget.settle(budget.reserve(1_600), None)

    assert budget.spent == 2_500


def test_a_call_that_raised_is_charged_nothing():
    budget = TokenBudget(5_000, chars_per_token=1.6, output_tokens=1_500)

    budget.release(budget.reserve(1_600))

    assert budget.spent == 0
    budget.reserve(1_600)  # its room is free again


def test_the_call_that_would_pass_the_limit_is_refused_and_the_run_stops():
    budget = TokenBudget(6_000, chars_per_token=1.6, output_tokens=1_500)
    budget.settle(budget.reserve(1_600), None)  # 2,500 spent

    with pytest.raises(OverBudget):
        budget.reserve(8_000)  # 5,000 + 1,500 more would pass 6,000

    assert budget.exhausted
    with pytest.raises(OverBudget):
        budget.reserve(10)  # a smaller one later in the queue does not slip in


def test_calls_in_flight_count_against_the_limit():
    budget = TokenBudget(4_000, chars_per_token=1.6, output_tokens=1_500)

    budget.reserve(1_600)  # 2,500, not yet answered

    with pytest.raises(OverBudget):
        budget.reserve(1_600)


def test_reservations_from_many_threads_never_pass_the_limit():
    budget = TokenBudget(25_000, chars_per_token=1.0, output_tokens=0)
    granted = []

    def worker():
        for _ in range(50):
            try:
                granted.append(budget.reserve(1_000))
            except OverBudget:
                return

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(granted) == 25_000


# --- the usage log, read back -------------------------------------------------


def _line(ts, site, model="claude-sonnet-5-5", i=1_000_000, o=100_000, read=0, write=0):
    return json.dumps(
        {
            "ts": ts,
            "site": site,
            "model": model,
            "stop_reason": "end_turn",
            "input_tokens": i,
            "output_tokens": o,
            "cache_creation_input_tokens": write,
            "cache_read_input_tokens": read,
        }
    )


def _log(tmp_path, month, lines):
    (tmp_path / f"llm-usage-{month}.jsonl").write_text("\n".join(lines) + "\n")


def test_one_day_of_the_log_by_site(tmp_path):
    _log(
        tmp_path,
        "2026-10",
        [
            _line(
                "2026-10-09T23:59:59+00:00", "src.extract.attachment_pipeline._complete_and_parse"
            ),
            _line(
                "2026-10-10T01:00:00+00:00", "src.extract.attachment_pipeline._complete_and_parse"
            ),
            _line(
                "2026-10-10T02:00:00+00:00", "src.extract.attachment_pipeline._complete_and_parse"
            ),
            _line("2026-10-10T03:00:00+00:00", "src.extract.claude_extract.extract_one", o=0),
            "{not json",
            _line("2026-10-11T00:00:01+00:00", "src.extract.claude_extract.extract_one"),
        ],
    )

    day = usage_for_day(tmp_path, date(2026, 10, 10))

    assert day is not None
    assert day["calls"] == 3
    att = day["sites"]["src.extract.attachment_pipeline._complete_and_parse"]
    assert (att["calls"], att["input_tokens"], att["output_tokens"]) == (2, 2_000_000, 200_000)
    assert att["cost_usd"] == pytest.approx(2 * (2.20 + 1.10))
    assert day["cost_usd"] == pytest.approx(2 * 3.30 + 2.20)
    assert day["unpriced_calls"] == 0


def test_a_model_without_a_rate_is_counted_and_left_unpriced(tmp_path):
    _log(tmp_path, "2026-10", [_line("2026-10-10T01:00:00+00:00", "s", model="claude-opus-5-5")])

    day = usage_for_day(tmp_path, date(2026, 10, 10))

    assert day is not None
    assert (day["calls"], day["unpriced_calls"], day["cost_usd"]) == (1, 1, 0)


def test_the_first_of_the_month_reads_the_previous_months_file(tmp_path):
    _log(tmp_path, "2026-09", [_line("2026-09-30T12:00:00+00:00", "s")])

    day = usage_for_day(tmp_path, date(2026, 9, 30))

    assert day is not None and day["calls"] == 1


def test_no_log_for_that_month_is_none(tmp_path):
    assert usage_for_day(tmp_path, date(2026, 10, 10)) is None


def test_the_newest_logged_call(tmp_path):
    _log(
        tmp_path,
        "2026-10",
        [_line("2026-10-10T01:00:00+00:00", "s"), _line("2026-10-10T05:30:00+00:00", "s")],
    )

    newest = newest_usage_at(tmp_path, datetime(2026, 10, 11, 9, tzinfo=UTC))

    assert newest == datetime(2026, 10, 10, 5, 30, tzinfo=UTC)


def test_the_newest_logged_call_early_in_a_month_is_last_months(tmp_path):
    _log(tmp_path, "2026-09", [_line("2026-09-30T22:00:00+00:00", "s")])

    newest = newest_usage_at(tmp_path, datetime(2026, 10, 1, 1, tzinfo=UTC))

    assert newest == datetime(2026, 9, 30, 22, tzinfo=UTC)


def test_no_log_at_all_has_no_newest_call(tmp_path):
    assert newest_usage_at(tmp_path, datetime(2026, 10, 1, tzinfo=UTC)) is None


def test_the_log_writer_and_this_reader_agree_on_the_file(tmp_path, monkeypatch):
    """claude_extract writes the file this module reads; the two names must not drift."""
    from src.extract import claude_extract

    class _Response:
        model = "claude-sonnet-5-5"
        stop_reason = "end_turn"
        usage = _Usage(10, 5)

    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", tmp_path)
    claude_extract._log_usage(_Response(), "site")

    today = datetime.now(UTC).date()
    day = usage_for_day(tmp_path, today)
    assert day is not None and day["sites"]["site"]["input_tokens"] == 10
    assert llm_cost.usage_log_path(tmp_path, today).exists()
