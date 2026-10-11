"""The extraction eval under either call shape, and on the cases it builds itself.

`--call-shape v1|v2` runs the same cases through extract_one under either shape, so the
owner can compare them before turning v2 on; the record says which shape it measured,
and the schema check grades each reply against the schema it was asked for. The owner's
labelled set lives outside the repository and may be absent on a host, so
`--synthetic` runs six cases the eval builds itself, with their gold.

The model is a fake installed as claude_extract.complete, where the SDK call is.
"""

import importlib.util
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.evals import extraction as ev
from src.evals import read_jsonl
from src.extract import call_shape, claude_extract, schema_v2
from tests.patching import unset_env

NOW = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
MODEL = "claude-sonnet-5-5"
SCRIPT = Path(__file__).parents[2] / "scripts" / "extraction_eval.py"
_TAG = re.compile(r"<(untrusted_[0-9a-f]+)>\n(.*?)\n</\1>", re.DOTALL)


def response(text, *, stop="end_turn", tokens=(1000, 200)):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        model=MODEL,
        usage=SimpleNamespace(
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def v2_reply(case, quote=True):
    """The gold of `case` written as a v2 reply, quoting the email for its evidence."""
    gold = case["gold"]
    body = case["item"]["content"]
    evidence = body.split("\n")[0][:60] if quote else "words the email never wrote"
    return json.dumps(
        {
            "item_kind": gold["item_kind"],
            "language": gold["language"],
            "summary": "s",
            "topics": [],
            "sentiment": "informational",
            "urgency": "low",
            "decisions": [
                {
                    "evidence": evidence,
                    "decision": d["text"],
                    "decided_by": "x",
                    "owner_role": d["owner_role"],
                    "status": d["status"],
                }
                for d in gold["decisions"]
            ],
            "action_items": [
                {
                    "evidence": evidence,
                    "task": a["text"],
                    "owner": "x",
                    "direction": a["direction"],
                    "deadline": a["deadline"],
                }
                for a in gold["actions"]
            ],
            "commitments": [],
            "key_facts": [],
            "people": [],
        },
        ensure_ascii=False,
    )


class FakeModel:
    """Answers each extraction by its email's subject: v2 calls with the case's gold as
    a v2 reply, v1 calls with an empty v1 reply."""

    def __init__(self, cases, first=None):
        self.by_subject = {c["item"]["subject"]: c for c in cases}
        self.first = list(first or [])
        self.calls = []

    def __call__(self, *, max_tokens, messages, model=None, **kwargs):
        self.calls.append({"max_tokens": max_tokens, "messages": messages, **kwargs})
        if self.first:
            return self.first.pop(0)
        inside = _TAG.search(messages[0]["content"]).group(2)
        subject = re.search(r"Subject: (.*)", inside).group(1)
        case = self.by_subject[subject]
        if "output_config" in kwargs and "format" in kwargs["output_config"]:
            return response(v2_reply(case))
        return response('{"summary": "s"}')


@pytest.fixture
def files(tmp_path):
    return ev.Files(tmp_path / "evals" / "extraction")


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setenv("CLAUDE_EXTRACT_MODEL", MODEL)
    unset_env(monkeypatch, "BRAIN_EXTRACT_CALL_SHAPE")

    def install(cases, first=None):
        fake = FakeModel(cases, first)
        monkeypatch.setattr(claude_extract, "complete", fake)
        return fake

    return install


@pytest.fixture
def synthetic():
    return ev.synthetic_cases(owner_name="Owner Person", owner_pattern="owner@example.com")


# --- the synthetic set -----------------------------------------------------------------


def test_the_synthetic_set_is_valid_labelled_and_passes_its_own_checks(synthetic):
    assert ev.validate_cases(synthetic) == []
    assert all(ev.is_labelled(c) for c in synthetic)
    every_short_stratum = {s for s, _, _ in ev.STRATA} - {"greek_long_thread"}
    assert {c["stratum"] for c in synthetic} == every_short_stratum | {"injection"}
    test = ev.self_test(synthetic, ["Owner Person"])
    assert test["oracle_failures"] == []
    assert test["oracle"] == 1.0


def test_the_synthetic_set_writes_to_the_configured_owner(synthetic):
    """The prompt's identity line names the configured owner; the emails must too."""
    owner = {"name": "Owner Person", "address": "owner@example.com"}
    for case in synthetic:
        item = case["item"]
        people = [item["sender"], *item["to_recipients"], *item["cc_recipients"]]
        assert owner in people, case["id"]


def test_the_synthetic_set_has_a_placeholder_owner_when_none_is_configured():
    cases = ev.synthetic_cases(owner_name="", owner_pattern="")

    assert ev.validate_cases(cases) == []
    assert any(
        r.get("address") == "owner@example.com" for c in cases for r in c["item"]["to_recipients"]
    )


def test_the_vocabularies_are_schema_v2s():
    assert ev.ITEM_KINDS is schema_v2.ITEM_KINDS
    assert ev.OWNER_ROLES is schema_v2.OWNER_ROLES
    assert ev.DIRECTIONS is schema_v2.DIRECTIONS


# --- a run under either shape ----------------------------------------------------------


def test_a_v2_run_sends_v2_requests_and_grades_v2_replies(files, model, synthetic):
    fake = model(synthetic)

    record = ev.run(files, synthetic, reps=1, workers=2, call_shape="v2", set_sha="s", now=NOW)

    assert len(fake.calls) == len(synthetic)
    for sent in fake.calls:
        assert sent["output_config"]["format"]["schema"] == schema_v2.EMAIL_SCHEMA_V2
        assert sent["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert record["call_shape"] == "v2"
    assert record["prompt_sha"] == ev.prompt_sha("v2") != ev.prompt_sha("v1")
    assert record["rows"]["scored"] == len(synthetic)
    for name in ("schema_valid", "kind", "language", "owner_roles", "evidence_verbatim"):
        check = record["checks"][name]
        assert check["applicable"] and check["rate"] == 1.0, name
    rows = read_jsonl(files.runs / f"{record['run_id']}.jsonl")
    assert {r["call_shape"] for r in rows} == {"v2"}


def test_a_v1_run_sends_the_v1_request(files, model, synthetic):
    fake = model(synthetic)

    record = ev.run(files, synthetic, reps=1, workers=1, call_shape="v1", set_sha="s", now=NOW)

    assert all("system" not in c and "output_config" not in c for c in fake.calls)
    assert record["call_shape"] == "v1"
    assert record["prompt_sha"] == ev.prompt_sha("v1")


def test_without_a_switch_the_run_takes_the_environments_shape(
    files, model, synthetic, monkeypatch
):
    model(synthetic)
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2")

    record = ev.run(files, synthetic[:1], reps=1, workers=1, set_sha="s", now=NOW)

    assert record["call_shape"] == "v2"


def test_the_switch_holds_for_the_run_only(files, model, synthetic):
    model(synthetic)

    ev.run(files, synthetic[:1], reps=1, workers=1, call_shape="v2", set_sha="s", now=NOW)

    assert "BRAIN_EXTRACT_CALL_SHAPE" not in os.environ
    assert call_shape.call_shape() == "v1"


def test_a_retried_attempt_is_counted_and_priced_whole(files, model, synthetic):
    """v2 asks once more after a max_tokens stop: both calls are billed."""
    cut = response('{"summary": "cut', stop="max_tokens", tokens=(1000, 8192))
    model(synthetic, first=[cut])

    record = ev.run(files, synthetic[:1], reps=1, workers=1, call_shape="v2", set_sha="s", now=NOW)

    [row] = read_jsonl(files.runs / f"{record['run_id']}.jsonl")
    assert row["calls"] == 2
    assert row["usage"]["output_tokens"] == 8192 + 200
    assert record["cost_usd"] == pytest.approx((2000 * 2.20 + 8392 * 11.00) / 1e6)


def test_the_schema_check_follows_the_shape_the_reply_was_asked_in(synthetic):
    case = synthetic[0]
    raw = v2_reply(case)
    extraction = json.loads(raw)

    assert ev.grade(case, extraction, raw, [], call_shape="v2")["schema_valid"] is True
    assert ev.grade(case, extraction, raw, [], call_shape="v1")["schema_valid"] is False


def test_an_evidence_quote_the_email_never_wrote_fails(synthetic):
    case = next(c for c in synthetic if c["gold"]["actions"])
    raw = v2_reply(case, quote=False)

    checks = ev.grade(case, json.loads(raw), raw, [], call_shape="v2")

    assert checks["evidence_verbatim"] is False


def test_regrade_grades_each_row_in_its_own_shape(files, model, synthetic):
    model(synthetic)
    ev.run(files, synthetic, reps=1, workers=1, call_shape="v2", set_sha="s", now=NOW)

    regraded = ev.regrade(files, synthetic, ["Owner Person"])

    assert regraded["checks"]["schema_valid"]["rate"] == 1.0


def test_a_v2_estimate_counts_the_system_prompt(synthetic):
    v1 = ev.estimate(synthetic, reps=1, judge=False, call_shape="v1")
    v2 = ev.estimate(synthetic, reps=1, judge=False, call_shape="v2")

    assert v2["calls"] == v1["calls"] == len(synthetic)
    assert v2["input_tokens"] > v1["input_tokens"]


# --- the command line ------------------------------------------------------------------


@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location("extraction_eval_cli_shape", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def test_a_synthetic_dry_run_needs_no_set(cli, files, model, capsys):
    fake = model([])

    assert cli(["--dry-run", "--synthetic", "--dir", str(files.root)]) == 0
    assert fake.calls == []
    assert not files.cases.exists() and not files.results.exists()
    assert "6 cases, 6 labelled" in capsys.readouterr().out


@pytest.mark.parametrize("shape", ["v1", "v2"])
def test_a_synthetic_run_under_either_shape(cli, files, model, monkeypatch, shape):
    monkeypatch.setenv("BRAIN_USER_NAME", "Owner Person")
    monkeypatch.setenv("BRAIN_USER_EMAIL_PATTERN", "owner@example.com")
    model(ev.synthetic_cases(owner_name="Owner Person", owner_pattern="owner@example.com"))
    argv = ["--run", "--synthetic", "--yes", "--reps", "1", "--call-shape", shape]

    assert cli([*argv, "--dir", str(files.root)]) == 0

    [record] = read_jsonl(files.results)
    assert record["call_shape"] == shape and record["cases"] == 6


@pytest.mark.parametrize("mode_", ["--build", "--calibrate"])
def test_synthetic_is_for_dry_runs_and_runs_only(cli, files, mode_):
    assert cli([mode_, "--synthetic", "--dir", str(files.root)]) == 2


def test_an_unknown_shape_in_the_environment_stops_the_eval(cli, files, model, monkeypatch, capsys):
    model([])
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v9")

    assert cli(["--dry-run", "--synthetic", "--dir", str(files.root)]) == 2
    assert "BRAIN_EXTRACT_CALL_SHAPE" in capsys.readouterr().err
