"""src/evals/summary.py: the latest eval results, read back for the health check."""

import pytest

from src.evals import append_jsonl
from src.evals.summary import latest_eval_summary


def extraction_run(date, pass_rate, *, set_sha="s1", selection="all", **extra):
    return {
        "eval": "extraction",
        "ts": f"{date}T03:00:00+00:00",
        "date": date,
        "run_id": date.replace("-", ""),
        "set_sha": set_sha,
        "selection": selection,
        "model": "claude-sonnet-5-5",
        "prompt_sha": "p1",
        "effort": "default",
        "pass_rate": pass_rate,
        "rows": {"scored": 80, "truncated": 0, "refused": 0, "errors": 0},
        "cost_usd": 2.5,
        **extra,
    }


def qa_run(date, recall10, *, set_sha="q1", semantic=None):
    modes = {
        mode: {"questions": 30, "recall@5": r - 0.1, "recall@10": r, "mrr": 0.5, "ndcg@10": 0.5}
        for mode, r in recall10.items()
    }
    return {
        "eval": "qa",
        "ts": f"{date}T04:00:00+00:00",
        "date": date,
        "set_sha": set_sha,
        "modes": modes,
        "semantic_status": semantic or {"semantic": {"ok": 30}, "recall": {"ok": 30}},
    }


def known_item_run(date, hit10, *, set_sha="k1"):
    return {
        "eval": "known-item",
        "ts": f"{date}T05:00:00+00:00",
        "date": date,
        "set_sha": set_sha,
        "thread": {"queries": 20, "hit@1": 0.6, "hit@5": 0.7, "hit@10": hit10, "mrr": 0.65},
    }


@pytest.fixture
def home(tmp_path):
    return tmp_path


def write(home, kind, *records):
    for record in records:
        append_jsonl(home / "evals" / kind / "results.jsonl", record)


def test_nothing_recorded_yet_is_neither_ok_nor_a_warning(home):
    summary = latest_eval_summary(home)

    assert summary["status"] == "NONE"
    assert summary["warnings"] == []
    assert summary["extraction"]["latest"] is None


def test_a_drop_of_more_than_five_points_in_extraction_is_a_warning(home):
    write(
        home, "extraction", extraction_run("2026-10-04", 0.825), extraction_run("2026-10-11", 0.75)
    )

    summary = latest_eval_summary(home)

    assert summary["status"] == "WARN"
    assert summary["extraction"]["change_points"] == pytest.approx(-7.5)
    assert len(summary["warnings"]) == 1
    assert "extraction" in summary["warnings"][0]
    assert "2026-10-04" in summary["warnings"][0]


def test_a_smaller_drop_or_a_gain_is_ok(home):
    write(
        home,
        "extraction",
        extraction_run("2026-10-01", 0.70),
        extraction_run("2026-10-04", 0.80),
        extraction_run("2026-10-11", 0.76),
    )

    summary = latest_eval_summary(home)

    assert summary["status"] == "OK"
    assert summary["extraction"]["change_points"] == pytest.approx(-4.0)


def test_the_threshold_is_a_parameter(home):
    write(
        home, "extraction", extraction_run("2026-10-04", 0.80), extraction_run("2026-10-11", 0.77)
    )

    assert latest_eval_summary(home, drop_points=2.0)["status"] == "WARN"


def test_a_run_is_compared_with_the_last_run_on_the_same_set_and_cases(home):
    """A pilot on three cases, or a run after the owner edited the set, measures
    something else: it is never compared, and it does not hide the run before it."""
    write(
        home,
        "extraction",
        extraction_run("2026-10-01", 0.90),
        extraction_run("2026-10-04", 0.30, selection="three-cases"),
        extraction_run("2026-10-08", 0.20, set_sha="edited"),
        extraction_run("2026-10-11", 0.80),
    )

    summary = latest_eval_summary(home)

    assert summary["extraction"]["previous"]["date"] == "2026-10-01"
    assert summary["extraction"]["change_points"] == pytest.approx(-10.0)
    assert summary["status"] == "WARN"


def test_a_run_with_nothing_comparable_before_it_has_no_change(home):
    write(
        home,
        "extraction",
        extraction_run("2026-10-04", 0.9),
        extraction_run("2026-10-11", 0.1, set_sha="new"),
    )

    summary = latest_eval_summary(home)

    assert summary["extraction"]["previous"] is None
    assert summary["extraction"]["change_points"] is None
    assert summary["status"] == "OK"


def test_calibration_records_are_not_runs(home):
    write(
        home,
        "extraction",
        extraction_run("2026-10-04", 0.80),
        {"eval": "extraction-calibration", "date": "2026-10-05", "agreement": 0.95},
    )

    assert latest_eval_summary(home)["extraction"]["latest"]["date"] == "2026-10-04"


def test_a_qa_drop_in_any_mode_is_named(home):
    write(
        home,
        "retrieval",
        qa_run("2026-10-04", {"keyword": 0.5, "semantic": 0.6, "hybrid": 0.6, "recall": 0.7}),
        qa_run("2026-10-11", {"keyword": 0.5, "semantic": 0.6, "hybrid": 0.52, "recall": 0.7}),
    )

    summary = latest_eval_summary(home)

    assert summary["qa"]["change_points"]["hybrid"] == pytest.approx(-8.0)
    assert summary["qa"]["change_points"]["keyword"] == pytest.approx(0.0)
    assert len(summary["warnings"]) == 1
    assert "hybrid" in summary["warnings"][0]


def test_an_embedding_outage_in_the_latest_qa_run_is_a_warning_on_its_own(home):
    """The 2026-10-06 outage left semantic search on keyword stand-ins for days,
    and no number moved enough for anyone to look."""
    write(
        home,
        "retrieval",
        qa_run(
            "2026-10-11",
            {"keyword": 0.5, "recall": 0.7},
            semantic={
                "semantic": {"keyword_seeded: ClientError": 30},
                "recall": {"ok": 2, "unavailable: ClientError": 28},
            },
        ),
    )

    summary = latest_eval_summary(home)

    assert summary["status"] == "WARN"
    assert summary["qa"]["latest"]["semantic_not_ok"] == 58
    assert "58 of 60" in summary["warnings"][0]


def test_a_known_item_drop_is_a_warning(home):
    write(home, "retrieval", known_item_run("2026-10-04", 0.85), known_item_run("2026-10-11", 0.70))

    summary = latest_eval_summary(home)

    assert summary["known_item"]["change_points"] == pytest.approx(-15.0)
    assert "known-item" in summary["warnings"][0]


def test_a_torn_line_does_not_hide_the_runs_around_it(home):
    path = home / "evals" / "extraction" / "results.jsonl"
    write(home, "extraction", extraction_run("2026-10-04", 0.8))
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"eval": "extraction", "date": "2026-10-0\n')
    write(home, "extraction", extraction_run("2026-10-11", 0.7))

    summary = latest_eval_summary(home)

    assert summary["extraction"]["latest"]["date"] == "2026-10-11"
    assert summary["extraction"]["previous"]["date"] == "2026-10-04"


def test_the_summary_names_the_call_shape_each_run_measured(home):
    """A v2 run after a v1 run on the same set is the comparison the switch waits for."""
    write(
        home,
        "extraction",
        extraction_run("2026-10-04", 0.8),  # before call shapes were recorded
        extraction_run("2026-10-10", 0.8, call_shape="v1"),
        extraction_run("2026-10-11", 0.85, call_shape="v2"),
    )

    summary = latest_eval_summary(home)["extraction"]

    assert (summary["latest"]["call_shape"], summary["previous"]["call_shape"]) == ("v2", "v1")
    assert summary["change_points"] == pytest.approx(5.0)


def test_the_default_home_is_the_eval_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_DATA_DIR", str(tmp_path))
    write(tmp_path, "extraction", extraction_run("2026-10-11", 0.8))

    assert latest_eval_summary()["extraction"]["latest"]["pass_rate"] == 0.8


def test_the_summary_says_how_old_the_latest_run_is(home):
    from datetime import UTC, datetime

    write(home, "extraction", extraction_run("2026-10-04", 0.8))

    summary = latest_eval_summary(home, now=datetime(2026, 10, 11, 3, 0, tzinfo=UTC))

    assert summary["extraction"]["latest"]["age_days"] == pytest.approx(7.0)
