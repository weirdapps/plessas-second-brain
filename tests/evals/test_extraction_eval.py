"""The extraction eval: build, grading, the run through extract_one, the judge, calibration.

The model is always the fake below, installed as claude_extract.complete: the eval must
call the production entry point, so the fake sits exactly where the SDK call does.
"""

import importlib.util
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.evals import extraction as ev
from src.evals import read_jsonl
from src.extract import claude_extract
from src.store.schema import create_database
from tests.payment_data import card, iban

NOW = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
MODEL = "claude-sonnet-5-5"
SCRIPT = Path(__file__).parents[2] / "scripts" / "extraction_eval.py"


# --- the fake model --------------------------------------------------------------


def response(text, *, stop="end_turn", model=MODEL, tokens=(1000, 200)):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        model=model,
        usage=SimpleNamespace(
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def reply(*, decisions=(), actions=(), language="english", **extra):
    """An extraction in the production schema, as the model writes it."""
    return json.dumps(
        {
            "summary": "s",
            "topics": [],
            "decisions": [{"decision": d, "decided_by": by} for d, by in decisions],
            "action_items": [{"task": t, "owner": o, "deadline": dl} for t, o, dl in actions],
            "commitments": [],
            "people_roles": {},
            "sentiment": "informational",
            "urgency": "low",
            "language": language,
            "key_facts": [],
            "references": [],
            **extra,
        },
        ensure_ascii=False,
    )


class FakeModel:
    """Answers an extraction by the email's subject, and a judge call with `verdict`."""

    def __init__(self, replies, verdict=None, model=MODEL):
        self.replies = replies
        self.verdict = verdict
        self.model = model
        self.calls = []

    def __call__(self, *, max_tokens, messages, model=None, **kwargs):
        self.calls.append({"max_tokens": max_tokens, "messages": messages, **kwargs})
        prompt = messages[0]["content"]
        if "system" in kwargs:
            out = self.verdict(prompt)
            return out if isinstance(out, SimpleNamespace) else response(json.dumps(out))
        subject = re.search(r"Subject: (.*)", prompt).group(1)
        answer = self.replies[subject]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, SimpleNamespace):
            return answer
        return response(answer, model=self.model)

    def judge_calls(self):
        return [c for c in self.calls if "system" in c]


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setenv("CLAUDE_EXTRACT_MODEL", MODEL)

    def install(replies, verdict=None, served=MODEL):
        fake = FakeModel(replies, verdict, served)
        monkeypatch.setattr(claude_extract, "complete", fake)
        return fake

    return install


# --- cases -------------------------------------------------------------------------


def gold(kind="work_thread", language="en", decisions=(), actions=(), must_not=()):
    return {
        "item_kind": kind,
        "language": language,
        "decisions": [{"text": t, "owner_role": r, "status": s} for t, r, s in decisions],
        "actions": [{"text": t, "direction": d, "deadline": dl} for t, d, dl in actions],
        "must_not": list(must_not),
    }


def email_case(cid, content="Body.", *, gold_=None, stratum="inbound_ask"):
    return {
        "id": cid,
        "kind": "email",
        "stratum": stratum,
        "source": {"email_id": f"source-{cid}"},
        "gold": gold_ if gold_ is not None else ev.empty_gold(),
        "item": {
            "message_id": cid,
            "date_received": "2026-10-01T08:00:00Z",
            "sender": {"name": "Colleague", "address": "colleague@example.com"},
            "to_recipients": [{"name": "Owner Person", "address": "owner@example.com"}],
            "cc_recipients": [],
            "subject": f"subject {cid}",
            "content": content,
            "mailbox_name": "Archive",
        },
    }


@pytest.fixture
def files(tmp_path):
    return ev.Files(tmp_path / "evals" / "extraction")


ALIASES = ["Owner Person", "Person"]


# --- building the set --------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    greek_thread = (
        "Καλημέρα σας, ακολουθεί η ενημέρωση για το έργο. " * 140
        + "\n\nΑπό: Συνάδελφος\nΣτάλθηκε: Δευτέρα\nΘέμα: Συνάντηση\n\nΠαλαιότερο μήνυμα."
    )
    emails = [
        (
            1,
            "Sent Items",
            "owner@example.com",
            "Budget follow-up",
            "english",
            "I will send it.",
            "2026-09-01",
        ),
        (
            2,
            "SentItems",
            "owner@example.com",
            "Plan",
            "english",
            "Please prepare the plan.",
            "2026-09-02",
        ),
        (
            3,
            "Archive",
            "colleague@example.com",
            "Need your approval",
            "english",
            "Approve by Monday?",
            "2026-09-03",
        ),
        (
            4,
            "Archive",
            "partner@example.org",
            "FW: Regulation update",
            "english",
            "FYI.",
            "2026-09-04",
        ),
        (
            5,
            "Archive",
            "no-reply@service.example",
            "Weekly digest",
            "english",
            "Digest.",
            "2026-09-05",
        ),
        (
            6,
            "Archive",
            "offers@shop.example",
            "Offers",
            "english",
            "Deals. Unsubscribe here.",
            "2026-09-06",
        ),
        (
            7,
            "Archive",
            "colleague@example.com",
            "Θέμα συνάντησης",
            "greek",
            greek_thread,
            "2026-09-07",
        ),
        (8, "News", "news@example.com", "Markets", "english", "News text.", "2026-09-08"),
        (9, "External", "", "[Document] report", "english", "Doc text.", "2026-09-09"),
        (10, "Archive", "colleague@example.com", "Old ask", "english", "Do it.", "2025-01-01"),
        (
            11,
            "Archive",
            "colleague@example.com",
            "Lunch",
            "english",
            "See you at lunch.",
            "2026-09-10",
        ),
        (
            12,
            "Archive",
            "colleague@example.com",
            "Re: Approve",
            "english",
            "Please do.",
            "2026-09-11",
        ),
    ]
    conn.executemany(
        "INSERT INTO emails (id, message_id, mailbox_name, sender_address, sender_name, subject,"
        " language, content, date_received) VALUES (?, ?, ?, ?, 'Sender', ?, ?, ?, ?)",
        [
            (i, i, mb, addr, subj, lang, body, f"{day}T09:00:00Z")
            for i, mb, addr, subj, lang, body, day in emails
        ],
    )
    conn.executemany(
        "INSERT INTO people (id, name, email) VALUES (?, ?, ?)",
        [(1, "Owner Person", "owner@example.com"), (2, "Colleague", "colleague@example.com")],
    )
    conn.executemany(
        "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, ?)",
        [(3, 1, "recipient"), (12, 2, "recipient"), (12, 1, "cc"), (11, 1, "recipient")],
    )
    conn.execute("INSERT INTO commitments (email_id, commitment) VALUES (1, 'send it')")
    conn.executemany(
        "INSERT INTO action_items (email_id, task) VALUES (?, 'a task')", [(2,), (3,), (10,), (12,)]
    )
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, file_path, exported_at)"
        " VALUES (4, 4, 'reg.pdf', '/x/reg.pdf', '2026-09-04T09:00:00Z')"
    )
    conn.commit()
    conn.close()
    return path


def build(store, **kw):
    from src.evals import connect_read_only

    conn = connect_read_only(store)
    try:
        return ev.build_cases(
            conn, days=180, seed=7, owner_pattern="owner@example.com", now=NOW, **kw
        )
    finally:
        conn.close()


def test_build_draws_each_stratum_from_the_store_and_adds_the_crafted_items(store):
    cases, _notes = build(store)

    by_stratum = {}
    for case in cases:
        by_stratum.setdefault(case["stratum"], set()).add(case["source"].get("email_id"))
    assert by_stratum["sent_commitment"] == {1, 2}
    assert by_stratum["inbound_ask"] == {3}
    assert by_stratum["fyi_forward"] == {4, 12}
    assert by_stratum["bulk_notification"] == {5, 6}
    assert by_stratum["greek_long_thread"] == {7}
    assert len(by_stratum["injection"]) == 1  # both crafted, neither from the store
    assert sum(1 for c in cases if c["stratum"] == "injection") == 2


def test_built_cases_carry_the_item_as_extraction_reads_it_and_empty_gold(store):
    cases, _ = build(store)
    ask = next(c for c in cases if c["stratum"] == "inbound_ask")

    assert ask["id"] == "ask-01"
    assert ask["kind"] == "email"
    assert ask["gold"] == ev.empty_gold()
    assert ask["item"]["subject"] == "Need your approval"
    assert ask["item"]["sender"]["address"] == "colleague@example.com"
    assert ask["item"]["to_recipients"] == [
        {"name": "Owner Person", "address": "owner@example.com"}
    ]
    assert list(ask) == ["id", "kind", "stratum", "source", "gold", "item"]


def test_build_is_the_same_for_the_same_seed(store):
    assert build(store)[0] == build(store)[0]


def test_build_says_which_strata_came_up_short(store):
    _cases, notes = build(store)

    assert any(note.startswith("inbound_ask: 1 of 10") for note in notes)


def test_the_crafted_items_are_labelled_and_valid():
    crafted = ev.crafted_cases()

    assert [c["id"] for c in crafted] == ["inject-01", "inject-02"]
    assert all(ev.is_labelled(c) and c["gold"]["must_not"] for c in crafted)
    assert ev.validate_cases(crafted) == []


# --- validating the set -------------------------------------------------------------


def test_validation_names_each_problem():
    good = email_case("a", gold_=gold(actions=[("x", "owner_owes", "2026-10-02")]))
    bad_enum = email_case("b", gold_=gold(kind="memo"))
    bad_date = email_case("c", gold_=gold(actions=[("x", "owner_owes", "Friday")]))
    no_text = email_case("d", gold_=gold(decisions=[("", "owner_decided", "made")]))
    dup = email_case("a")
    teams = {**email_case("e"), "kind": "teams"}
    half = email_case("f", gold_={**ev.empty_gold(), "decisions": [{"text": "x"}]})

    problems = ev.validate_cases([good, bad_enum, bad_date, no_text, dup, teams, half])

    assert not any(p.startswith("a:") for p in problems if "duplicate" not in p)
    for cid in "bcdef":
        assert any(p.startswith(f"{cid}:") for p in problems), cid
    assert any("duplicate" in p for p in problems)
    # Problems are printed; the labels they are about stay in the file.
    assert not any("memo" in p or "Friday" in p for p in problems)


# --- the programmatic checks --------------------------------------------------------


def checks(case, text, aliases=ALIASES):
    from src.extract.parser import parse_extraction

    return ev.grade(case, parse_extraction(text), text, aliases)


def test_schema_validity_is_strict_json_in_the_production_schema():
    case = email_case("a", gold_=gold())

    assert checks(case, reply())["schema_valid"] is True
    assert checks(case, "Here it is:\n" + reply())["schema_valid"] is False
    assert checks(case, json.dumps({"summary": "s"}))["schema_valid"] is False


def test_kind_is_graded_only_once_the_extraction_emits_one():
    case = email_case("a", gold_=gold(kind="fyi_forward"))

    assert checks(case, reply())["kind"] is None
    assert checks(case, reply(item_kind="fyi_forward"))["kind"] is True
    assert checks(case, reply(item_kind="work_thread"))["kind"] is False


def test_language_names_are_read_as_codes():
    case = email_case("a", gold_=gold(language="el"))

    assert checks(case, reply(language="greek"))["language"] is True
    assert checks(case, reply(language="english"))["language"] is False
    assert checks(email_case("u"), reply())["language"] is None  # unlabelled


def test_owner_roles_count_the_owners_decisions_and_debts_by_name():
    case = email_case(
        "a",
        gold_=gold(
            decisions=[("approve", "owner_decided", "made"), ("hire", "third_party", "made")],
            actions=[("send", "owner_owes", None), ("review", "owed_to_owner", None)],
        ),
    )
    right = reply(
        decisions=[("approve", "κ. ΠΕΡΣΟΝ"), ("hire", "HR")],
        actions=[("send", "owner person", None), ("review", "Colleague", None)],
    )
    wrong = reply(decisions=[("approve", "HR")], actions=[("send", "owner person", None)])

    assert checks(case, right, aliases=["Owner Person", "Πέρσον"])["owner_roles"] is True
    assert checks(case, wrong)["owner_roles"] is False
    assert checks(case, right, aliases=[])["owner_roles"] is None


def test_owner_roles_compare_the_fields_once_the_extraction_emits_them():
    case = email_case("a", gold_=gold(decisions=[("approve", "owner_decided", "made")]))
    explicit = json.loads(reply())
    explicit["decisions"] = [
        {"decision": "approve", "decided_by": "", "owner_role": "owner_decided"}
    ]
    other = json.loads(reply())
    other["decisions"] = [{"decision": "approve", "decided_by": "", "owner_role": "third_party"}]

    assert checks(case, json.dumps(explicit), aliases=[])["owner_roles"] is True
    assert checks(case, json.dumps(other), aliases=[])["owner_roles"] is False


def test_bulk_and_notifications_must_yield_no_decisions_or_actions():
    bulk = email_case("a", gold_=gold(kind="bulk"))

    assert checks(bulk, reply())["bulk_empty"] is True
    assert checks(bulk, reply(decisions=[("buy now", "Shop")]))["bulk_empty"] is False
    assert checks(email_case("b", gold_=gold()), reply())["bulk_empty"] is None


def test_deadlines_must_match_exactly():
    case = email_case("a", gold_=gold(actions=[("send", "owner_owes", "2026-10-16")]))

    assert checks(case, reply(actions=[("send", "x", "2026-10-16")]))["deadlines"] is True
    assert checks(case, reply(actions=[("send", "x", None)]))["deadlines"] is False
    assert checks(case, reply(actions=[("send", "x", "Friday")]))["deadlines"] is False
    none = email_case("b", gold_=gold(actions=[("send", "owner_owes", None)]))
    assert checks(none, reply(actions=[("send", "x", "2026-10-16")]))["deadlines"] is False


def test_evidence_must_be_quoted_from_the_source():
    case = email_case("a", "Please   send the\nrevised budget by Friday.", gold_=gold())
    quoted = json.loads(reply())
    quoted["decisions"] = [
        {"decision": "d", "decided_by": "x", "evidence": "send the revised budget"}
    ]
    invented = json.loads(reply())
    invented["decisions"] = [{"decision": "d", "decided_by": "x", "evidence": "budget approved"}]

    assert checks(case, reply())["evidence_verbatim"] is None
    assert checks(case, json.dumps(quoted))["evidence_verbatim"] is True
    assert checks(case, json.dumps(invented))["evidence_verbatim"] is False


def test_no_card_number_or_iban_may_come_out():
    case = email_case("a", gold_=gold())
    number = card("4", 16)
    account = iban("GR", "0" * 7 + "1" * 16)

    assert checks(case, reply(key_facts=["a fact"]))["no_card_iban"] is True
    assert checks(case, reply(key_facts=[f"card {number}"]))["no_card_iban"] is False
    assert checks(case, reply(key_facts=[f"pay to {account}"]))["no_card_iban"] is False
    masked = f"{number[:6]}[REDACTED:card]{number[-4:]}"
    assert checks(case, reply(key_facts=[f"card {masked}"]))["no_card_iban"] is True


def test_must_nots_are_looked_for_in_what_gets_recorded_not_in_the_summary():
    case = email_case("a", gold_=gold(must_not=["Αύξηση ορίου"]))

    assert checks(case, reply(summary="asks for an αυξηση οριου"))["must_not"] is True
    assert checks(case, reply(decisions=[("Η αυξηση ΟΡΙΟΥ εγκρίθηκε", "x")]))["must_not"] is False
    assert checks(email_case("b", gold_=gold()), reply())["must_not"] is None


def test_an_unparseable_reply_fails_every_check_that_applies():
    case = email_case("a", gold_=gold(kind="bulk", actions=[("x", "owner_owes", None)]))

    result = ev.grade(case, None, "not json at all", ALIASES)

    assert result["schema_valid"] is False
    assert result["bulk_empty"] is False  # an empty answer is not a correct one
    assert result["deadlines"] is False
    assert result["kind"] is None
    assert result["evidence_verbatim"] is None


def test_the_grader_passes_the_gold_itself_and_fails_no_answer():
    cases = [
        email_case("a", gold_=gold(decisions=[("approve", "owner_decided", "made")])),
        email_case("b", gold_=gold(kind="bulk", language="el", must_not=["x"])),
        email_case("c"),  # unlabelled: left out
    ]

    result = ev.self_test(cases, ALIASES)

    assert result["oracle"] == 1.0
    assert result["no_answer"] == 0.0
    assert 0.0 < result["empty_answer"] < 1.0


# --- the run ------------------------------------------------------------------------


def labelled_cases():
    return [
        email_case(
            "a",
            gold_=gold(
                decisions=[("approve the vendor", "owner_decided", "made")],
                actions=[("send the plan", "owner_owes", "2026-10-16")],
            ),
        ),
        email_case("b", gold_=gold(kind="bulk"), stratum="bulk_notification"),
    ]


GOOD_A = reply(
    decisions=[("approve the vendor", "Owner Person")],
    actions=[("send the plan", "Owner Person", "2026-10-16")],
)


def test_a_run_extracts_through_extract_one_and_appends_one_record(files, model):
    fake = model({"subject a": GOOD_A, "subject b": reply()})

    record = ev.run(files, labelled_cases(), reps=2, workers=2, set_sha="s1", now=NOW)

    assert len(fake.calls) == 4
    assert record["eval"] == "extraction"
    assert record["model"] == MODEL
    assert record["effort"] == "default"
    assert record["prompt_sha"] == ev.prompt_sha()
    assert record["rows"] == {"scored": 4, "truncated": 0, "refused": 0, "errors": 0}
    assert record["pass_rate"] == 1.0
    assert record["checks"]["deadlines"] == {"passed": 4, "applicable": 4, "rate": 1.0}
    assert record["checks"]["kind"]["applicable"] == 0
    assert record["cost_usd"] == pytest.approx(4 * (1000 * 2.20 + 200 * 11.00) / 1e6)
    assert record["noise_points"] == pytest.approx(50.0)
    assert read_jsonl(files.results) == [record]
    rows = read_jsonl(files.runs / f"{record['run_id']}.jsonl")
    assert sorted((r["case_id"], r["rep"]) for r in rows) == [
        ("a", 0),
        ("a", 1),
        ("b", 0),
        ("b", 1),
    ]
    assert all(r["checks"] and r["raw_text"] for r in rows)


def test_the_run_goes_through_the_real_complete_and_logs_its_own_sites(
    files, monkeypatch, tmp_path
):
    """Only the client is fake here: the retry policy, the refusal fallback and the usage
    log are production's, and the eval's spend shows in that log apart from the sync's."""

    class Messages:
        def create(self, **kw):
            prompt = kw["messages"][0]["content"]
            if "system" in kw:
                return response(json.dumps(exact_judge(prompt)))
            assert re.search(r"Subject: (.*)", prompt).group(1) == "subject a"
            return response(GOOD_A)

    monkeypatch.setattr(
        claude_extract,
        "_get_client_and_model",
        lambda: (SimpleNamespace(messages=Messages()), MODEL),
    )
    monkeypatch.setenv("CLAUDE_EXTRACT_MODEL", MODEL)
    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", tmp_path)

    record = ev.run(
        files, labelled_cases()[:1], reps=1, workers=1, judge=True, set_sha="s", now=NOW
    )

    assert (record["rows"]["scored"], record["judge"]["judged"]) == (1, 1)
    logged = [json.loads(line) for p in tmp_path.glob("llm-usage-*.jsonl") for line in p.open()]
    assert sorted(entry["site"] for entry in logged) == [
        "src.evals.extraction._eval_extraction_call",
        "src.evals.extraction._judge_call",
    ]


def test_the_extraction_prompt_is_the_production_one(files, model):
    fake = model({"subject a": GOOD_A, "subject b": reply()})

    ev.run(files, labelled_cases(), reps=1, workers=1, set_sha="s1", now=NOW)

    from src.extract.prompt import build_extraction_prompt

    def untagged(text):  # the fence tag is drawn per prompt
        return re.sub(r"untrusted_[0-9a-f]+", "TAG", text)

    sent = {c["messages"][0]["content"] for c in fake.calls}
    expected = {build_extraction_prompt(c["item"]) for c in labelled_cases()}
    assert {untagged(p) for p in sent} == {untagged(p) for p in expected}
    assert {c["max_tokens"] for c in fake.calls} == {claude_extract.MAX_OUTPUT_TOKENS}


def test_an_effort_override_reaches_the_call_and_the_record(files, model):
    fake = model({"subject a": GOOD_A, "subject b": reply()})

    record = ev.run(files, labelled_cases(), reps=1, workers=1, effort="low", set_sha="s", now=NOW)

    assert {c["output_config"]["effort"] for c in fake.calls} == {"low"}
    assert record["effort"] == "low"


def test_truncated_and_refused_replies_are_counted_not_scored(files, model):
    model(
        {
            "subject a": response('{"summary": "cut', stop="max_tokens"),
            "subject b": response("", stop="refusal"),
        }
    )

    record = ev.run(files, labelled_cases(), reps=1, workers=1, set_sha="s", now=NOW)

    assert record["rows"] == {"scored": 0, "truncated": 1, "refused": 1, "errors": 0}
    assert record["pass_rate"] is None


def test_a_call_that_never_answered_goes_to_errors_not_to_the_scores(files, model):
    model({"subject a": RuntimeError("quota"), "subject b": reply()})

    record = ev.run(files, labelled_cases(), reps=1, workers=1, set_sha="s", now=NOW)

    assert record["rows"]["errors"] == 1
    assert record["rows"]["scored"] == 1
    errors = read_jsonl(files.errors)
    assert [(e["case_id"], e["stage"], e["class"]) for e in errors] == [
        ("a", "extract", "harness_error")
    ]
    assert errors[0]["run_id"] == record["run_id"]


def test_a_reply_served_by_another_model_is_an_error(files, model):
    model({"subject a": GOOD_A, "subject b": reply()}, served="claude-opus-4-6")

    record = ev.run(files, labelled_cases(), reps=1, workers=1, set_sha="s", now=NOW)

    assert record["rows"]["errors"] == 2
    assert {e["class"] for e in read_jsonl(files.errors)} == {"model_mismatch"}


def test_an_unparseable_reply_is_a_scored_failure(files, model):
    model({"subject a": "I cannot produce JSON today.", "subject b": reply()})

    record = ev.run(files, labelled_cases(), reps=1, workers=1, set_sha="s", now=NOW)

    assert record["rows"]["scored"] == 2
    assert record["checks"]["schema_valid"] == {"passed": 1, "applicable": 2, "rate": 0.5}
    assert record["case_pass_rate"] == 0.5


def test_a_run_compares_only_with_the_same_cases(files, model):
    model({"subject a": GOOD_A, "subject b": reply()})

    full = ev.run(files, labelled_cases(), reps=1, workers=1, set_sha="s", now=NOW)
    one = ev.run(files, labelled_cases()[:1], reps=1, workers=1, set_sha="s", now=NOW)

    assert full["selection"] != one["selection"]
    assert full["run_id"] != one["run_id"]
    assert ev.regrade(files, labelled_cases(), ALIASES)["run_id"] == one["run_id"]


# --- the judge ----------------------------------------------------------------------


def exact_judge(prompt):
    """Pairs items whose text is identical: the judge a faithful matcher would be."""
    sets = {}
    for name in (
        "DECISIONS, SET A",
        "DECISIONS, SET B",
        "ACTION ITEMS, SET A",
        "ACTION ITEMS, SET B",
    ):
        block = prompt.split(name + "\n", 1)[1].split("\n\n", 1)[0]
        sets[name] = [line.split(". ", 1)[1] for line in block.splitlines() if ". " in line]

    def pairs(a, b):
        return [
            {"a": i, "b": sets[b].index(t) + 1} for i, t in enumerate(sets[a], 1) if t in sets[b]
        ]

    return {
        "decision_pairs": pairs("DECISIONS, SET A", "DECISIONS, SET B"),
        "action_pairs": pairs("ACTION ITEMS, SET A", "ACTION ITEMS, SET B"),
    }


def test_the_judge_scores_decision_and_action_sets(files, model):
    partial = reply(
        decisions=[("approve the vendor", "x"), ("something invented", "y")],
        actions=[],
    )
    fake = model({"subject a": partial, "subject b": reply()}, verdict=exact_judge)

    record = ev.run(files, labelled_cases(), reps=1, workers=1, judge=True, set_sha="s", now=NOW)

    judge_calls = fake.judge_calls()
    assert len(judge_calls) == 1  # case b: both sides empty, nothing to ask
    call = judge_calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == "high"
    assert "untrusted_" in call["messages"][0]["content"]
    rows = {r["case_id"]: r for r in read_jsonl(files.runs / f"{record['run_id']}.jsonl")}
    decisions = rows["a"]["judge"]["decisions"]
    assert (decisions["precision"], decisions["recall"]) == (0.5, 1.0)
    assert rows["a"]["judge"]["actions"]["recall"] == 0.0
    assert rows["b"]["judge"]["decisions"]["f1"] == 1.0
    assert record["judge"]["judged"] == 2
    assert record["judge"]["decisions_f1"] == pytest.approx((2 / 3 + 1.0) / 2)
    assert record["judge_cost_usd"] > 0


def test_a_verdict_that_breaks_the_contract_is_a_judge_error(files, model):
    model(
        {"subject a": GOOD_A, "subject b": reply()},
        verdict=lambda prompt: {"decision_pairs": [{"a": 1, "b": 9}], "action_pairs": []},
    )

    record = ev.run(
        files, labelled_cases()[:1], reps=1, workers=1, judge=True, set_sha="s", now=NOW
    )

    assert [(e["stage"], e["class"]) for e in read_jsonl(files.errors)] == [
        ("judge", "invalid_verdict")
    ]
    assert record["judge"]["judged"] == 0


# --- calibration --------------------------------------------------------------------


def calibration_cases():
    return [
        email_case(
            "a",
            gold_=gold(
                decisions=[("approve the vendor", "owner_decided", "made")],
                actions=[
                    ("send the plan", "owner_owes", None),
                    ("book a room", "third_party", None),
                ],
            ),
        ),
        email_case("b", gold_=gold(decisions=[("hire two analysts", "third_party", "made")])),
    ]


def test_a_faithful_judge_agrees_with_every_probe(files, model):
    model({}, verdict=exact_judge)

    record = ev.calibrate(files, calibration_cases(), now=NOW)

    assert record["probes"]["total"] > 0
    assert record["agreement"] == 1.0
    assert record["calibrated"] is True
    assert read_jsonl(files.results)[-1]["eval"] == "extraction-calibration"


def test_a_judge_that_pairs_everything_is_not_calibrated(files, model):
    def generous(prompt):
        verdict = exact_judge(prompt)
        verdict["decision_pairs"] = [{"a": 1, "b": 1}]
        return verdict

    model({}, verdict=generous)

    record = ev.calibrate(files, calibration_cases(), now=NOW)

    assert record["agreement"] < 0.9
    assert record["calibrated"] is False


def test_the_owner_template_comes_from_the_latest_run_and_owner_counts_are_checked(files, model):
    fake = model({"subject a": GOOD_A, "subject b": reply()}, verdict=exact_judge)
    ev.run(files, labelled_cases(), reps=1, workers=1, set_sha="s", now=NOW)

    assert ev.write_calibration_template(files, labelled_cases()) == 2
    lines = read_jsonl(files.calibration)
    assert {line["case_id"] for line in lines} == {"a", "b"}
    assert all(line["decisions_matched"] is None for line in lines)
    for line in lines:
        line["decisions_matched"] = len(line["extracted_decisions"])
        line["actions_matched"] = len(line["extracted_actions"])
    lines[0]["actions_matched"] += 1  # the owner disagrees with the judge on one set
    from src.evals import write_jsonl

    write_jsonl(files.calibration, lines)
    before = len(fake.judge_calls())

    record = ev.calibrate(files, labelled_cases(), now=NOW)

    assert record["owner"]["total"] == 2
    assert record["owner"]["agreed"] == 1
    assert len(fake.judge_calls()) > before


# --- the command line ---------------------------------------------------------------


@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location("extraction_eval_cli", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def write_set(files, cases):
    from src.evals import write_jsonl

    write_jsonl(files.cases, cases)


def mode(path):
    import stat

    return stat.S_IMODE(path.stat().st_mode)


def test_build_writes_the_set_privately_and_never_overwrites_it(
    cli, files, store, monkeypatch, capsys
):
    monkeypatch.setenv("BRAIN_USER_EMAIL_PATTERN", "owner@example.com")
    argv = ["--build", "--dir", str(files.root), "--db", str(store)]

    assert cli(argv) == 0
    assert mode(files.root) == 0o700
    for written in (files.cases, files.cases_md, files.owner):
        assert mode(written) == 0o600, written
    first = files.cases.read_bytes()
    assert cli(argv) == 2
    assert files.cases.read_bytes() == first
    assert cli([*argv, "--force"]) == 0
    out = capsys.readouterr()
    for subject in ("Need your approval", "Regulation update", "Θέμα συνάντησης", "Budget"):
        assert subject not in out.out + out.err


def test_what_a_run_prints_holds_no_item_label_or_extraction(cli, files, model, capsys):
    model({"subject a": GOOD_A, "subject b": reply()}, verdict=exact_judge)
    write_set(files, labelled_cases())

    assert cli(["--run", "--judge", "--yes", "--reps", "1", "--dir", str(files.root)]) == 0
    assert cli(["--dry-run", "--dir", str(files.root)]) == 0
    assert cli(["--calibrate", "--yes", "--dir", str(files.root)]) == 0

    out = capsys.readouterr()
    for text in ("subject a", "Body.", "approve the vendor", "send the plan", "2026-10-16"):
        assert text not in out.out + out.err


def test_every_mode_refuses_a_directory_inside_the_repository(cli, store):
    from src.evals import REPO_ROOT

    inside = REPO_ROOT / "evals-should-not-exist"
    for argv in (
        ["--build", "--db", str(store)],
        ["--dry-run"],
        ["--run", "--yes"],
        ["--calibrate", "--yes"],
    ):
        assert cli([*argv, "--dir", str(inside)]) == 2
    assert not inside.exists()


def test_an_error_that_quotes_the_item_is_kept_without_the_quote(files, model):
    content = "The quarterly zebrafinch figures are attached for review."
    model({"subject a": RuntimeError(f"upstream rejected: {content}")})

    ev.run(files, [email_case("a", content)], reps=1, workers=1, set_sha="s", now=NOW)

    text = files.errors.read_text(encoding="utf-8")
    assert "zebrafinch" not in text
    assert "RuntimeError" in text
    assert mode(files.errors) == 0o600


def test_dry_run_calls_no_model_and_records_nothing(cli, files, model):
    fake = model({})
    write_set(files, labelled_cases())

    assert cli(["--dry-run", "--dir", str(files.root), "--judge"]) == 0
    assert fake.calls == []
    assert not files.results.exists()


def test_dry_run_fails_on_an_invalid_set(cli, files, model):
    model({})
    write_set(files, [email_case("a", gold_=gold(kind="memo"))])

    assert cli(["--dry-run", "--dir", str(files.root)]) == 1


def test_dry_run_regrades_the_latest_run_against_the_current_gold(files, model):
    model({"subject a": GOOD_A, "subject b": reply()})
    cases = labelled_cases()
    ev.run(files, cases, reps=1, workers=1, set_sha="s", now=NOW)
    cases[0]["gold"]["actions"][0]["deadline"] = "2026-10-17"

    regraded = ev.regrade(files, cases, ALIASES)

    assert regraded["checks"]["deadlines"]["rate"] == 0.5


def test_a_run_needs_yes(cli, files, model, capsys):
    fake = model({"subject a": GOOD_A, "subject b": reply()}, verdict=exact_judge)
    write_set(files, labelled_cases())

    assert cli(["--run", "--judge", "--dir", str(files.root)]) == 2
    assert fake.calls == []
    assert "$" in capsys.readouterr().out
    assert cli(["--run", "--judge", "--yes", "--reps", "1", "--dir", str(files.root)]) == 0
    assert len(read_jsonl(files.results)) == 1
    assert read_jsonl(files.results)[0]["judge"]["judged"] == 2


def test_a_run_can_be_limited_to_named_cases(cli, files, model):
    fake = model({"subject a": GOOD_A})
    write_set(files, labelled_cases())

    assert cli(["--run", "--yes", "--reps", "1", "--case", "a", "--dir", str(files.root)]) == 0
    assert len(fake.calls) == 1
    assert cli(["--run", "--yes", "--case", "zz", "--dir", str(files.root)]) == 2


def test_a_missing_set_is_refused(cli, files):
    assert cli(["--dry-run", "--dir", str(files.root)]) == 2


def test_a_gold_that_fails_its_own_check_fails_the_dry_run(cli, files, model):
    model({})
    contradiction = gold(
        decisions=[("raise the limit", "owner_decided", "made")], must_not=["raise the limit"]
    )
    write_set(files, [email_case("a", gold_=contradiction)])

    assert cli(["--dry-run", "--dir", str(files.root)]) == 1


def test_calibrate_writes_the_template_and_spends_only_with_yes(cli, files, model):
    fake = model({"subject a": GOOD_A, "subject b": reply()}, verdict=exact_judge)
    write_set(files, labelled_cases())
    ev.run(files, labelled_cases(), reps=1, workers=1, set_sha="s", now=NOW)
    assert cli(["--dry-run", "--dir", str(files.root)]) == 0  # regrades that run
    calls = len(fake.calls)

    assert cli(["--calibrate", "--dir", str(files.root)]) == 2
    assert files.calibration.exists()
    assert len(fake.calls) == calls
    assert cli(["--calibrate", "--yes", "--dir", str(files.root)]) == 0
    assert read_jsonl(files.results)[-1]["calibrated"] is True


def test_calibrate_with_nothing_to_judge_is_refused(cli, files, model):
    model({})
    write_set(files, [email_case("a", gold_=gold(kind="bulk"))])

    assert cli(["--calibrate", "--yes", "--dir", str(files.root)]) == 2
