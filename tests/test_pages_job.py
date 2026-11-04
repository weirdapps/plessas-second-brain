"""`brain pages`: the synthesis of the dirty pages (issue 169, src/pipeline/pages.py).

One model call a page through claude_extract.complete() (topic_pages' `ask`), at most 300
words, every line citing items the evidence held; a line that cites anything else is
dropped. New pages first, then dirty ones, within a budget of calls; a full build writes a
new generation and swaps it in only when it is complete.
"""

import json
from types import SimpleNamespace

import pytest

from src.pipeline import pages as job
from src.store import pages
from src.store.schema import create_database
from tests import factories as f

OWNER = ("Owner Example", "owner@example.com")


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    from src import config

    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", OWNER[1])


@pytest.fixture
def store(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    for i in range(5):
        f.email(
            conn,
            subject="Budget 2027" if i == 0 else "Re: Budget 2027",
            sender=f.ALICE if i % 2 == 0 else OWNER,
            to=(OWNER,) if i % 2 == 0 else (f.ALICE,),
            topics=["okapi budget"],
            conversation_id="cid-budget",
            date_received=f.ago(days=3, hours=-i),
            source_class="mail",
            summary=f"Alice proposes budget version {i}",
        )
    pages.refresh(conn)
    conn.commit()
    yield conn
    conn.close()


def _reply(lines):
    return job.Reply(json.dumps({"lines": lines}), "claude-sonnet-5-5", 100, 50, 0.001, "end_turn")


def _fake(monkeypatch, make):
    calls = []

    def ask(system, user, **kw):
        calls.append(user)
        return make(user)

    monkeypatch.setattr(job, "ask", ask)
    return calls


def _first_ref(user):
    import re

    return re.search(r"\[(email:\d+)\]", user).group(1)


def test_a_synthesis_keeps_cited_lines_dates_them_and_drops_the_rest(store, monkeypatch):
    def make(user):
        ref = _first_ref(user)
        return _reply(
            [
                {"text": "Alice is negotiating the 2027 budget with the owner.", "refs": [ref]},
                {"text": "Invented line", "refs": ["email:999999"]},
                {"text": "No refs at all", "refs": []},
            ]
        )

    _fake(monkeypatch, make)
    stats = job.build(store, kinds=["thread"], llm_calls=5)

    assert stats["built"] == 1 and stats["lines_dropped"] == 2
    page = pages.page_for_thread(store, "cid-budget")
    (line,) = page["synthesis"]
    assert line["text"].startswith("Alice is negotiating")
    assert line["dates"][line["refs"][0]]  # [kind:id, date]
    assert "## Synthesis" in page["markdown"]
    assert page["pending"] is None, "the dirty flag is cleared"


def test_the_synthesis_is_held_to_300_words(store, monkeypatch):
    def make(user):
        ref = _first_ref(user)
        return _reply([{"text": " ".join(["word"] * 120), "refs": [ref]} for _ in range(4)])

    _fake(monkeypatch, make)
    job.build(store, kinds=["thread"], llm_calls=5)
    page = pages.page_for_thread(store, "cid-budget")
    assert sum(len(x["text"].split()) for x in page["synthesis"]) <= pages.SYNTHESIS_WORDS


def test_the_budget_of_calls_is_kept_and_new_pages_go_first(store, monkeypatch):
    calls = _fake(monkeypatch, lambda user: _reply([{"text": "x", "refs": [_first_ref(user)]}]))
    stats = job.build(store, llm_calls=1)
    assert len(calls) == 1 and stats["built"] == 1
    assert stats["left"] >= 1


def test_estimate_calls_no_model_and_writes_nothing(store, monkeypatch):
    calls = _fake(monkeypatch, lambda user: pytest.fail("no call on an estimate"))
    stats = job.build(store, estimate=True, llm_calls=40)
    assert not calls and stats["to_build"] >= 2 and stats["cost_usd"] > 0
    assert store.execute("SELECT COUNT(synthesis) FROM pages").fetchone()[0] == 0


def test_a_failed_call_keeps_the_page_dirty(store, monkeypatch):
    def boom(system, user, **kw):
        raise RuntimeError("vertex down")

    monkeypatch.setattr(job, "ask", boom)
    stats = job.build(store, kinds=["thread"], llm_calls=5)
    assert stats["failed"] == 1
    assert pages.page_for_thread(store, "cid-budget")["pending"]


def test_a_dropped_ref_never_reaches_the_model(store, monkeypatch):
    newest = store.execute(
        "SELECT MAX(id) FROM emails WHERE conversation_id = 'cid-budget'"
    ).fetchone()[0]
    pages.add_override(store, "thread", "cid-budget", "drop_ref", ref=f"email:{newest}")
    calls = _fake(monkeypatch, lambda user: _reply([{"text": "x", "refs": [_first_ref(user)]}]))
    job.build(store, kinds=["thread"], llm_calls=5)
    assert f"email:{newest}]" not in calls[0]


def test_a_full_build_swaps_in_a_kind_only_when_all_its_pages_are_written(store, monkeypatch):
    _fake(monkeypatch, lambda user: _reply([{"text": "x", "refs": [_first_ref(user)]}]))
    kinds = {k for (k,) in store.execute("SELECT DISTINCT kind FROM pages")}
    total = store.execute("SELECT COUNT(*) FROM pages").fetchone()[0]

    first = job.build(store, full=True, llm_calls=1)

    # A kind with no page swaps at once; of the others, only the one written.
    assert first["built"] == 1 and len(set(first["swapped"]) & kinds) <= 1
    for kind in kinds - set(first["swapped"]):
        assert pages.live_generation(store, kind) == 0, "not live before it is whole"
    rest = job.build(store, full=True, llm_calls=total)
    assert set(first["swapped"]) | set(rest["swapped"]) >= kinds
    assert store.execute("SELECT COUNT(*) FROM pages WHERE synthesis IS NULL").fetchone()[0] == 0
    assert store.execute("SELECT COUNT(*) FROM page_dirty").fetchone()[0] == 0


def test_the_cli_refuses_on_a_replica(tmp_path, monkeypatch, capsys):
    from src import config

    monkeypatch.setattr(config, "is_replica", lambda: True)
    args = SimpleNamespace(
        db=tmp_path / "x.db",
        estimate=False,
        status=False,
        refresh=False,
        full=False,
        kind=None,
        llm_calls=40,
        deadline_s=None,
        override=None,
        ref=None,
        text=None,
        overrides=False,
        remove_override=None,
    )
    assert job.cmd(args) == 2
    assert "replica" in capsys.readouterr().err.lower()
