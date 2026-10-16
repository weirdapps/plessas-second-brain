"""The extraction eval under call shape v2-text, and its check that no call of a shape
without structured outputs sends `output_config.format`.

The Vertex project the eval runs on refuses structured outputs: every v2 call of the
2026-10-11 run failed with HTTP 400 and nothing was graded. So `--call-shape v2-text`
must run, its replies be graded against schema v2, and a request that would send
`output_config.format` under it be caught before it is sent: by the dry run, which builds
every case's request and calls nothing, and by the run itself. The judge states its
verdict's shape in its prompt and sends no format either.
"""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.evals import extraction as ev
from src.evals import read_jsonl
from src.extract import call_shape, claude_extract, prompt, schema_v2

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
MODEL = "claude-sonnet-5-5"
SCRIPT = Path(__file__).parents[2] / "scripts" / "extraction_eval.py"


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


def gold_reply(case):
    """The gold of `case` as a schema v2 reply, wrapped in a Markdown fence as a model
    answering without structured outputs may write it."""
    gold = case["gold"]
    body = case["item"]["content"]
    evidence = body.split("\n")[0][:60]
    answer = {
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
    }
    return "```json\n" + json.dumps(answer, ensure_ascii=False) + "\n```"


class TextModel:
    """Answers every extraction with its case's gold, found by the subject in the user
    turn, and records each call."""

    def __init__(self, cases):
        self.cases = cases
        self.calls = []

    def __call__(self, *, max_tokens, messages, model=None, **kwargs):
        self.calls.append({"max_tokens": max_tokens, "messages": messages, **kwargs})
        turn = messages[0]["content"]
        case = next(c for c in self.cases if f"Subject: {c['item']['subject']}" in turn)
        return response(gold_reply(case))


@pytest.fixture
def files(tmp_path):
    return ev.Files(tmp_path / "evals" / "extraction")


@pytest.fixture
def synthetic():
    return ev.synthetic_cases(owner_name="Owner Person", owner_pattern="owner@example.com")


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setenv("CLAUDE_EXTRACT_MODEL", MODEL)

    def install(cases):
        fake = TextModel(cases)
        monkeypatch.setattr(claude_extract, "complete", fake)
        return fake

    return install


def test_a_v2_text_run_states_the_schema_sends_no_format_and_grades_against_schema_v2(
    files, model, synthetic
):
    fake = model(synthetic)

    record = ev.run(files, synthetic, reps=1, workers=1, call_shape="v2-text", set_sha="s", now=NOW)

    assert record["call_shape"] == "v2-text"
    assert record["rows"]["scored"] == len(synthetic) and record["rows"]["errors"] == 0
    assert record["format_calls"] == 0
    assert fake.calls and all("format" not in c["output_config"] for c in fake.calls)
    schema = json.dumps(schema_v2.EMAIL_SCHEMA_V2, ensure_ascii=False, separators=(",", ":"))
    assert all(any(schema in b["text"] for b in c["system"]) for c in fake.calls)
    assert record["checks"]["schema_valid"]["rate"] == 1.0
    rows = read_jsonl(files.runs / f"{record['run_id']}.jsonl")
    assert {row["call_shape"] for row in rows} == {"v2-text"}


def test_a_call_that_would_send_a_format_under_v2_text_is_stopped_before_it_is_sent(
    files, model, synthetic, monkeypatch
):
    fake = model(synthetic)
    real = call_shape.Request.kwargs

    def leaky(self, max_tokens):
        sent = real(self, max_tokens)
        sent["output_config"] = {**sent["output_config"], "format": {"type": "json_schema"}}
        return sent

    monkeypatch.setattr(call_shape.Request, "kwargs", leaky)

    record = ev.run(
        files, synthetic[:1], reps=1, workers=1, call_shape="v2-text", set_sha="s", now=NOW
    )

    assert fake.calls == []
    assert record["rows"]["errors"] == 1
    [error] = read_jsonl(files.errors)
    assert "output_config.format" in error["error"]


def test_a_v2_run_counts_its_format_calls(files, model, synthetic):
    model(synthetic)

    record = ev.run(files, synthetic[:2], reps=1, workers=1, call_shape="v2", set_sha="s", now=NOW)

    assert record["format_calls"] == 2


@pytest.mark.parametrize(("shape", "formats"), [("v1", 0), ("v2", 6), ("v2-text", 0)])
def test_the_request_check_builds_every_request_and_calls_nothing(model, synthetic, shape, formats):
    fake = model(synthetic)

    check = ev.request_check(synthetic, shape)

    assert check == {
        "call_shape": shape,
        "requests": len(synthetic),
        "format_requests": formats,
        "ok": formats == 0 or shape == "v2",
    }
    assert fake.calls == []


def test_the_prompt_hash_of_v2_text_is_the_one_its_calls_log(synthetic):
    request = call_shape.Request(
        route=call_shape.ROUTES["email"],
        system=tuple(prompt.email_system_v2()),
        user="u",
        structured=False,
    )
    sent = request.kwargs(8192)

    assert ev.prompt_sha("v2-text") == call_shape.prompt_hash(sent["system"], None)
    assert ev.prompt_sha("v2-text") != ev.prompt_sha("v2")


def test_a_v2_text_estimate_counts_the_stated_schema(synthetic):
    v2 = ev.estimate(synthetic, reps=1, judge=False, call_shape="v2")
    text = ev.estimate(synthetic, reps=1, judge=False, call_shape="v2-text")

    assert text["input_tokens"] > v2["input_tokens"]


def test_the_schema_check_holds_a_v2_text_reply_to_schema_v2(synthetic):
    case = synthetic[0]
    raw = gold_reply(case)
    extraction = json.loads(raw.removeprefix("```json\n").removesuffix("\n```"))

    assert ev.grade(case, extraction, raw, [], call_shape="v2-text")["schema_valid"] is True
    assert ev.grade(case, extraction, raw, [], call_shape="v1")["schema_valid"] is False


def test_the_judge_states_its_verdict_shape_and_sends_no_format():
    assert "format" not in ev.JUDGE_OUTPUT
    assert '"decision_pairs"' in ev.JUDGE_SYSTEM and '"action_pairs"' in ev.JUDGE_SYSTEM


def test_the_judge_reads_a_verdict_wrapped_in_prose(monkeypatch):
    monkeypatch.setattr(
        claude_extract,
        "complete",
        lambda **kwargs: response(
            'Here are the pairs:\n```json\n{"decision_pairs": [{"a": 1, "b": 1}],'
            ' "action_pairs": []}\n```'
        ),
    )

    verdict = ev.match_sets("email", ["d"], ["d"], [], [], expected_model=MODEL)

    assert verdict["decision_pairs"] == [{"a": 1, "b": 1}]


# --- the command line ------------------------------------------------------------------


@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location("extraction_eval_cli_v2_text", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def test_a_synthetic_dry_run_under_v2_text_checks_the_requests(cli, files, model, capsys):
    fake = model([])

    assert (
        cli(["--dry-run", "--synthetic", "--call-shape", "v2-text", "--dir", str(files.root)]) == 0
    )

    out = capsys.readouterr().out
    assert "6 requests in call shape v2-text, 0 with output_config.format" in out
    assert fake.calls == []


def test_a_dry_run_fails_when_a_v2_text_request_would_send_a_format(
    cli, files, model, monkeypatch, capsys
):
    model([])
    real = call_shape.Request.kwargs

    def leaky(self, max_tokens):
        sent = real(self, max_tokens)
        sent["output_config"] = {**sent["output_config"], "format": {"type": "json_schema"}}
        return sent

    monkeypatch.setattr(call_shape.Request, "kwargs", leaky)

    assert (
        cli(["--dry-run", "--synthetic", "--call-shape", "v2-text", "--dir", str(files.root)]) == 1
    )
    assert "output_config.format" in capsys.readouterr().out


def test_a_synthetic_run_under_v2_text(cli, files, model, monkeypatch):
    monkeypatch.setenv("BRAIN_USER_NAME", "Owner Person")
    monkeypatch.setenv("BRAIN_USER_EMAIL_PATTERN", "owner@example.com")
    model(ev.synthetic_cases(owner_name="Owner Person", owner_pattern="owner@example.com"))
    argv = ["--run", "--synthetic", "--yes", "--reps", "1", "--call-shape", "v2-text"]

    assert cli([*argv, "--dir", str(files.root)]) == 0

    [record] = read_jsonl(files.results)
    assert record["call_shape"] == "v2-text" and record["rows"]["errors"] == 0
