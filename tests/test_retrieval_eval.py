"""scripts/retrieval_eval.py: the known-item guard for keyword search and the qa set."""

import importlib.util
import json
import stat
from pathlib import Path

import numpy as np
import pytest

from src.evals import REPO_ROOT, read_jsonl
from src.store.schema import create_database

SCRIPT = Path(__file__).parent.parent / "scripts" / "retrieval_eval.py"


@pytest.fixture
def ev():
    spec = importlib.util.spec_from_file_location("retrieval_eval", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    rows = [
        (1, "Quarterly zebrafinch budget review", "Inbox"),
        (2, "Hello", "Inbox"),  # one search word: not usable
        (3, "Zebrafinch market wrap", "News"),  # news: left out
        (4, "", "Inbox"),
    ]
    conn.executemany(
        "INSERT INTO emails (id, message_id, date_received, subject, mailbox_name, summary) "
        "VALUES (?, ?, '2026-09-01T00:00:00Z', ?, ?, 'nothing about it')",
        [(i, i, subject, mailbox) for i, subject, mailbox in rows],
    )
    conn.commit()
    conn.close()
    return path


def test_the_set_takes_the_longest_subject_words_of_usable_mail_only(ev, db):
    conn = ev._open(db)

    items = ev.build_set(conn, size=5, seed=1)

    assert items == [{"email_id": 1, "query": "zebrafinch quarterly"}]


def test_the_score_counts_ranks(ev):
    result = ev.score([1, 3, None, 11])

    assert result["queries"] == 4
    assert (result["hit@1"], result["hit@5"], result["hit@10"]) == (0.25, 0.5, 0.5)
    assert result["mrr"] == pytest.approx((1 + 1 / 3 + 1 / 11) / 4)


def test_build_then_run_finds_the_email_by_its_subject(ev, db, tmp_path, capsys):
    """The subject is indexed from v22, so this is also the index's own check."""
    eval_set = tmp_path / "set.json"
    results = tmp_path / "evals" / "retrieval" / "results.jsonl"

    assert ev.main(["build", "--db", str(db), "--file", str(eval_set)]) == 0
    assert (
        ev.main(["run", "--db", str(db), "--file", str(eval_set), "--results", str(results)]) == 0
    )

    out = capsys.readouterr().out
    result = json.loads(out[out.index("{") : out.rindex("}") + 1])
    assert result["email"]["hit@1"] == 1.0
    assert result["thread"]["hit@1"] == 1.0
    assert json.loads(eval_set.read_text())[0]["email_id"] == 1
    assert stat.S_IMODE(eval_set.stat().st_mode) == 0o600


def test_each_known_item_run_is_recorded_and_prints_no_subject_words(ev, db, tmp_path, capsys):
    eval_set = tmp_path / "set.json"
    results = tmp_path / "evals" / "retrieval" / "results.jsonl"
    eval_set.write_text(json.dumps([{"email_id": 1, "query": "zebrafinch nowhere"}]))

    assert (
        ev.main(["run", "--db", str(db), "--file", str(eval_set), "--results", str(results)]) == 0
    )

    (record,) = read_jsonl(results)
    assert record["eval"] == "known-item"
    assert (record["thread"]["hit@1"], record["misses"]) == (0.0, 1)  # the news item ranks first
    assert record["set_sha"] and record["date"]
    assert stat.S_IMODE(results.stat().st_mode) == 0o600
    assert "zebrafinch" not in capsys.readouterr().out


def test_known_items_are_only_subjects_no_other_thread_shares(ev, tmp_path):
    """Two words that several threads' subjects hold make a tie-break, not a test: in the
    audit of 2026-10-11 such items came first 4 times in 61, unshared ones 65 in 66."""
    path = tmp_path / "t.db"
    conn = create_database(str(path))
    conn.executemany(
        "INSERT INTO emails (id, message_id, date_received, subject, mailbox_name, summary, "
        "conversation_id) VALUES (?, ?, '2026-09-01T00:00:00Z', ?, ?, 'nothing', ?)",
        [
            (1, 1, "Quarterly zebrafinch budget review", "Inbox", "A"),
            (2, 2, "Weekly status report", "Inbox", "B"),
            (3, 3, "Weekly status report", "Inbox", "C"),
            (4, 4, "RE: Weekly status report", "Inbox", "B"),
            (5, 5, "Kiwi orchard visit", "Inbox", "D"),
            (6, 6, "Orchard visit for kiwi growers", "News", None),  # news competes in run
        ],
    )
    conn.commit()
    conn.close()

    items = ev.build_set(ev._open(path), size=10, seed=1)

    assert [i["email_id"] for i in items] == [1]


def test_the_database_is_opened_read_only(ev, db):
    import sqlite3

    conn = ev._open(db)

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM emails")


def test_another_email_of_the_thread_counts_for_the_thread_only(ev, tmp_path):
    """Replies share their subject, so a sibling often ranks first; counted as a
    miss, that was noise about tie-breaking, not a retrieval failure."""
    path = tmp_path / "t.db"
    conn = create_database(str(path))
    conn.executemany(
        "INSERT INTO emails (id, message_id, date_received, subject, mailbox_name, summary, "
        "conversation_id) VALUES (?, ?, '2026-09-01T00:00:00Z', ?, 'Inbox', 'nothing', 'T1')",
        [(1, 1, "RE: Quarterly zebrafinch budget"), (2, 2, "Quarterly zebrafinch budget")],
    )
    conn.commit()
    conn.close()

    result, misses = ev.run_set(ev._open(path), [{"email_id": 1, "query": "zebrafinch quarterly"}])

    assert result["email"]["hit@1"] == 0.0
    assert result["thread"]["hit@1"] == 1.0
    assert misses == []


def test_a_thread_is_the_one_search_and_email_thread_know(ev, tmp_path):
    """News keeps a day per pipeline as its conversation_id and blank subjects
    share one hash: neither makes their emails one thread, so neither is a hit."""
    from src.store.schema import subject_to_conversation_id

    path = tmp_path / "t.db"
    conn = create_database(str(path))
    blank = subject_to_conversation_id("")
    conn.executemany(
        "INSERT INTO emails (id, message_id, date_received, subject, mailbox_name, summary, "
        "conversation_id) VALUES (?, ?, '2026-09-01T00:00:00Z', ?, ?, 'nothing', ?)",
        [
            (1, 1, "", "Inbox", blank),
            (2, 2, "", "Inbox", blank),
            (3, 3, "ECB holds", "News", "news:digest:2026-09-01"),
            (4, 4, "ECB warns", "News", "news:digest:2026-09-01"),
            (5, 5, "Plan", "Inbox", "T"),
            (6, 6, "RE: Plan", "Inbox", "T"),
        ],
    )
    conn.commit()
    conn.close()
    ro = ev._open(path)

    assert ev.thread_of(ro, 1) == {1}
    assert ev.thread_of(ro, 3) == {3}
    assert ev.thread_of(ro, 5) == {5, 6}


# --- the qa set ------------------------------------------------------------------------


@pytest.fixture
def qa_store(tmp_path):
    """Mail, a calendar event and two logged sessions in which the brain was asked things."""
    path = tmp_path / "qa.db"
    conn = create_database(str(path))
    conn.executemany(
        "INSERT INTO emails (id, message_id, date_received, subject, mailbox_name, summary,"
        " content, conversation_id) VALUES (?, ?, ?, ?, 'Archive', ?, ?, ?)",
        [
            (
                1,
                1,
                "2026-10-01T09:00:00Z",
                "Budget 2027 plan",
                "plan",
                "The plan is attached.",
                "T1",
            ),
            (2, 2, "2026-10-02T09:00:00Z", "Re: Budget 2027 plan", "ok", "Approved.", "T1"),
            (3, 3, "2026-10-03T09:00:00Z", "Lunch menu", "food", "Pasta today.", "T2"),
            (4, 4, "2026-10-04T09:00:00Z", "Kiwi orchard visit", "trip", "We go Friday.", None),
        ],
    )
    conn.execute(
        "INSERT INTO calendar_events (id, outlook_event_id, subject, start_at, end_at, ingested_at)"
        " VALUES (1, 'e1', 'Zebrafinch review', '2026-10-05T09:00:00Z', '2026-10-05T10:00:00Z',"
        " '2026-10-01T00:00:00Z')"
    )
    conn.executemany(
        "INSERT INTO conversations (id, session_id, started_at, workspace, created_at)"
        " VALUES (?, ?, '2026-10-01T10:00:00Z', ?, '2026-10-01T10:00:00Z')",
        [(1, "s-work", "/home/u/work"), (2, "s-brain", "/home/u/src/second-brain")],
    )
    turns = [
        (1, 0, "user", "what did we decide about the budget plan?"),
        (1, 1, "assistant", "[Tool: mcp__second-brain__recall]\nWe approved it."),
        (1, 2, "user", "<task-notification>done</task-notification>"),
        (1, 3, "assistant", "[Tool: mcp__second-brain__search_emails]"),
        (1, 4, "user", "and what about the lunch menu today?"),
        (1, 5, "assistant", "[Tool: Bash ls]"),
        (1, 6, "user", "What did we decide about the budget   plan?"),
        (
            1,
            7,
            "assistant",
            "[Tool: mcp__second-brain__search_emails]\n[Tool: mcp__second-brain__recall]",
        ),
        (1, 8, "user", "ok go on"),
        (1, 9, "assistant", "[Tool: mcp__second-brain__stats]"),
        (2, 0, "user", "test the recall tool on budget questions"),
        (2, 1, "assistant", "[Tool: mcp__second-brain__recall]"),
    ]
    conn.executemany(
        "INSERT INTO conversation_turns (conversation_id, turn_index, timestamp, speaker, content)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (c, i, f"2026-10-0{c}T10:{i:02d}:00Z", speaker, content)
            for c, i, speaker, content in turns
        ],
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def index(tmp_path):
    """Four email vectors: the budget thread on one axis, lunch and the kiwi trip on others."""
    path = tmp_path / "embeddings.npz"
    vectors = np.array(
        [[1, 0, 0, 0], [0.8, 0.2, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]], dtype=np.float32
    )
    np.savez(path, ids=np.array([1, 2, 3, 4]), vectors=vectors)
    return path


def embed(texts):
    """The query's axis, as the vectors above have it."""
    out = []
    for text in texts:
        if "budget" in text:
            out.append([1.0, 0, 0, 0])
        elif "kiwi" in text:
            out.append([0, 0, 1.0, 0])
        else:
            out.append([0, 0, 0, 1.0])
    return np.array(out, dtype=np.float32)


def question(qid, query, **gold):
    return {"id": qid, "question": f"question {qid}", "query": query, "modes": [], "gold": gold}


def test_build_qa_mines_the_questions_put_to_the_brain(ev, qa_store):
    items = ev.mine_questions(ev._open(qa_store), size=10)

    assert [i["question"] for i in items] == ["What did we decide about the budget plan?"]
    (item,) = items
    assert item["id"] == "q001"
    assert item["tools"] == ["recall", "search_emails"]
    assert item["source"] == {"session_id": "s-work", "turn_index": 6}
    assert item["gold"] == {kind: [] for kind in ev.GOLD_KINDS}
    assert item["query"] == "" and item["answer"] == ""


def test_sessions_in_the_brain_repository_itself_are_left_out_unless_asked(ev, qa_store):
    items = ev.mine_questions(ev._open(qa_store), size=10, all_workspaces=True)

    assert [i["source"]["session_id"] for i in items] == ["s-brain", "s-work"]


def test_the_metrics_follow_the_ranks_of_the_gold(ev):
    both = ev.qa_metrics([1, 3])
    one = ev.qa_metrics([None, 7])

    assert (both["recall@5"], both["recall@10"], both["rr"]) == (1.0, 1.0, 1.0)
    assert both["ndcg@10"] == pytest.approx((1 + 1 / np.log2(4)) / (1 + 1 / np.log2(3)))
    assert (one["recall@5"], one["recall@10"], one["rr"]) == (0.0, 0.5, pytest.approx(1 / 7))
    assert one["ndcg@10"] == pytest.approx((1 / np.log2(8)) / (1 + 1 / np.log2(3)))
    assert ev.qa_metrics([None])["ndcg@10"] == 0.0


def test_run_qa_grades_each_mode_on_the_kinds_it_can_return(ev, qa_store, index):
    items = [
        question("q1", "budget plan", email=[1]),  # the thread, whichever email shows it
        question("q2", "zebrafinch review", event=[1]),  # only recall returns events
        question("q3", "kiwi orchard", email=[4]),  # an email with no thread
        question("q4", "lunch"),  # not labelled yet
    ]

    result = ev.run_qa(ev._open(qa_store), items, embed_fn=embed, index_path=index)

    assert (result["questions"], result["labelled"]) == (4, 3)
    modes = result["modes"]
    assert {m: modes[m]["questions"] for m in modes} == {
        "keyword": 2,
        "semantic": 2,
        "hybrid": 2,
        "recall": 3,
        "keyword_recency": 2,
        "hybrid_recency": 2,
        "recall_recency": 3,
    }
    for mode in modes:
        assert modes[mode]["recall@10"] == 1.0, mode
        assert modes[mode]["mrr"] == 1.0, mode
    assert result["semantic_status"] == {
        "semantic": {"ok": 3},
        "recall": {"ok": 3},
        "recall_recency": {"ok": 3},
    }
    first = result["per_question"][0]
    assert first["id"] == "q1" and first["units"] == ["thread:T1"]
    # Every gold item was found by its own row: none only through a row's editions.
    assert not any(result["found_in_editions"].values())


def test_a_question_can_be_held_to_some_modes(ev, qa_store, index):
    items = [{**question("q1", "budget plan", email=[1]), "modes": ["keyword"]}]

    result = ev.run_qa(ev._open(qa_store), items, embed_fn=embed, index_path=index)

    assert list(result["modes"]) == ["keyword"]


def test_an_embedding_outage_shows_in_the_status_of_every_call(ev, qa_store, index):
    def down(texts):
        raise RuntimeError("model refused")

    result = ev.run_qa(
        ev._open(qa_store),
        [question("q1", "budget plan", email=[1])],
        embed_fn=down,
        index_path=index,
    )

    status = result["semantic_status"]
    assert status["semantic"] == {"keyword_seeded: RuntimeError": 1}
    assert status["recall"] == {"keyword_seeded: RuntimeError": 1}


def test_a_gold_email_missing_from_the_store_is_counted(ev, qa_store, index):
    result = ev.run_qa(
        ev._open(qa_store),
        [question("q1", "budget plan", email=[999])],
        embed_fn=embed,
        index_path=index,
    )

    assert result["unknown_gold"] == 1
    assert result["modes"]["keyword"]["recall@10"] == 0.0


def test_validate_qa_names_problems_without_their_values(ev):
    items = [
        question("q1", "a", email=[1]),
        question("q1", "b"),
        {**question("q2", "c"), "modes": ["fuzzy"]},
        {**question("q3", "d"), "gold": {"email": "12"}},
        {**question("q4", "e"), "gold": {"tickets": [1]}},
        {"id": "q5", "question": ""},
        question("q6", "f", email=["12"], thread=["AAQkADk="], conversation=["s-1", 3]),
    ]

    problems = ev.validate_qa(items)

    assert [p for p in problems if p.startswith("q6:")] == [
        "q6: gold.email is not a list of numeric ids"
    ]
    for qid in ("q2", "q3", "q4", "q5"):
        assert any(p.startswith(f"{qid}:") for p in problems), qid
    assert any("duplicate" in p for p in problems)
    assert not any("fuzzy" in p or "tickets" in p for p in problems)


def qa_argv(qa_store, tmp_path, *extra):
    return [
        *extra,
        "--db",
        str(qa_store),
        "--qa-file",
        str(tmp_path / "evals" / "retrieval" / "qa.jsonl"),
        "--results",
        str(tmp_path / "evals" / "retrieval" / "results.jsonl"),
    ]


def test_build_qa_writes_a_private_set_and_keeps_the_owners_labels(ev, qa_store, tmp_path, capsys):
    qa_file = tmp_path / "evals" / "retrieval" / "qa.jsonl"

    assert ev.main(qa_argv(qa_store, tmp_path, "--build-qa")) == 0
    assert stat.S_IMODE(qa_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(qa_file.parent.stat().st_mode) == 0o700
    assert ev.main(qa_argv(qa_store, tmp_path, "--build-qa")) == 2
    assert ev.main(qa_argv(qa_store, tmp_path, "--build-qa", "--force", "--all-workspaces")) == 0
    assert len(read_jsonl(qa_file)) == 2
    assert "budget" not in capsys.readouterr().out


def test_run_qa_records_a_dated_run_and_prints_no_question(
    ev, qa_store, tmp_path, capsys, monkeypatch
):
    from src.evals import write_jsonl
    from src.store import embeddings

    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", tmp_path / "no-index.npz")
    qa_file = tmp_path / "evals" / "retrieval" / "qa.jsonl"
    results = tmp_path / "evals" / "retrieval" / "results.jsonl"
    write_jsonl(qa_file, [question("q1", "budget plan", email=[1])])

    assert ev.main(qa_argv(qa_store, tmp_path, "--run-qa")) == 0

    (record,) = read_jsonl(results)
    assert record["eval"] == "qa" and record["date"] and record["set_sha"]
    assert record["modes"]["keyword"]["recall@10"] == 1.0
    # No vector index here: semantic search is down, and the record says so.
    assert set(record["semantic_status"]["semantic"]) == {"unavailable: FileNotFoundError"}
    out = capsys.readouterr().out
    assert "budget" not in out and "question q1" not in out


def test_run_qa_needs_a_labelled_set(ev, qa_store, tmp_path):
    from src.evals import write_jsonl

    assert ev.main(qa_argv(qa_store, tmp_path, "--run-qa")) == 2  # no set yet
    write_jsonl(tmp_path / "evals" / "retrieval" / "qa.jsonl", [question("q1", "budget plan")])
    assert ev.main(qa_argv(qa_store, tmp_path, "--run-qa")) == 2  # nothing labelled
    write_jsonl(tmp_path / "evals" / "retrieval" / "qa.jsonl", [{"id": "q1"}])
    assert ev.main(qa_argv(qa_store, tmp_path, "--run-qa")) == 1  # invalid


def test_nothing_is_written_inside_the_repository(ev, qa_store):
    inside = REPO_ROOT / "evals-should-not-exist"

    assert ev.main(["--build-qa", "--db", str(qa_store), "--qa-file", str(inside / "q.jsonl")]) == 2
    assert ev.main(["run", "--db", str(qa_store), "--results", str(inside / "r.jsonl")]) == 2
    assert not inside.exists()


def test_one_mode_at_a_time(ev):
    with pytest.raises(SystemExit):
        ev.main(["build", "--run-qa"])
    with pytest.raises(SystemExit):
        ev.main([])


# --- before and after the ranking of #PR: recency, editions, names, every bucket -------------


@pytest.fixture
def ranked_store(tmp_path):
    """A name written in Greek, monthly editions of one workbook, and a copy of a deck."""
    from tests import factories

    path = tmp_path / "ranked.db"
    conn = create_database(str(path))
    factories.person(conn, "Νικόλαος Οκαπίδης", "nokapidis@example.com")
    greek = factories.email(
        conn, subject="Θέμα", summary="Ο Οκαπίδης ενέκρινε.", content="Σώμα.", conversation_id="G"
    )
    editions = []
    for month in range(3, 9):
        sent = f"2026-{month + 1:02d}-20T09:00:00Z"
        email_id = factories.email(
            conn, subject=f"Zebra report {month}", date_received=sent, conversation_id=f"Z{month}"
        )
        editions.append(
            factories.attachment(
                conn,
                email_id=email_id,
                filename=f"Zebra Workbook 28.{month:02d}.2026.xlsx",
                text="the zebra conversion ratio",
            )
        )
    deck = factories.attachment(conn, filename="gnu deck.pptx", text="the gnu strategy")
    copy = factories.attachment(conn, filename="gnu deck final.pptx", text="unrelated words")
    conn.close()
    return path, {"greek": greek, "editions": editions, "deck": deck, "copy": copy}


def test_the_plain_modes_rank_as_before_and_the_recency_modes_as_now(ev, ranked_store, index):
    """A Latin name finds the Greek mail in keyword_recency only: the plain modes run with
    BRAIN_QUERY_RANKING=relevance and BRAIN_QUERY_NAMES=0, as the code before them."""
    path, made = ranked_store

    result = ev.run_qa(
        ev._open(path),
        [question("q1", "Okapidis", email=[made["greek"]])],
        embed_fn=embed,
        index_path=index,
    )

    modes = result["modes"]
    assert modes["keyword"]["recall@10"] == 0.0
    assert modes["keyword_recency"]["recall@10"] == 1.0
    assert modes["recall_recency"]["recall@10"] == 1.0


def test_an_edition_a_row_stands_for_counts_as_found_and_is_counted(ev, ranked_store, index):
    path, made = ranked_store
    oldest = made["editions"][0]

    result = ev.run_qa(
        ev._open(path),
        [question("q1", "zebra conversion ratio", attachment=[oldest])],
        embed_fn=embed,
        index_path=index,
    )

    assert result["modes"]["recall_recency"]["recall@10"] == 1.0
    assert result["found_in_editions"]["recall_recency"] == 1
    assert result["found_in_editions"].get("recall", 0) == 0


def test_alt_ids_are_graded_apart_as_equivalent_gold(ev, ranked_store, index):
    """The copy is found, the canonical deck is not: 0 strictly, 1 with alt."""
    path, made = ranked_store
    item = {**question("q1", "unrelated words", attachment=[made["deck"]])}
    item["alt"] = {"attachment": [made["copy"]]}

    result = ev.run_qa(ev._open(path), [item], embed_fn=embed, index_path=index)

    assert result["modes"]["recall"]["recall@10"] == 0.0
    assert result["modes_with_alt"]["recall"]["recall@10"] == 1.0
    assert result["modes_with_alt"]["recall_recency"]["recall@10"] == 1.0


def test_unanswerable_questions_are_checked_for_abstention(ev, ranked_store, index):
    path, _made = ranked_store
    items = [
        {**question("e1", "quokka invoices"), "expect_empty": True},  # nothing holds it
        {**question("e2", "zebra conversion ratio"), "expect_empty": True},  # rows claim it
    ]

    result = ev.run_qa(ev._open(path), items, embed_fn=embed, index_path=index)

    abstention = result["abstention"]
    assert abstention["keyword"] == {"cases": 2, "abstained": 1}
    assert abstention["recall_recency"] == {"cases": 2, "abstained": 1}
    assert "semantic" not in abstention


def test_results_are_given_by_shape_too(ev, qa_store, index):
    items = [
        {**question("q1", "budget plan", email=[1]), "shape": "status"},
        {**question("q3", "kiwi orchard", email=[4]), "shape": "numbers"},
    ]

    result = ev.run_qa(ev._open(qa_store), items, embed_fn=embed, index_path=index)

    assert result["by_shape"]["status"]["keyword"] == {"questions": 1, "recall@10": 1.0}
    assert set(result["by_shape"]) == {"status", "numbers"}


def test_each_question_is_embedded_once_for_every_mode(ev, qa_store, index):
    calls = []

    def counting(texts):
        calls.append(tuple(texts))
        return embed(texts)

    ev.run_qa(
        ev._open(qa_store),
        [question("q1", "budget plan", email=[1])],
        embed_fn=counting,
        index_path=index,
    )

    assert calls == [("budget plan",)]


def test_validate_qa_takes_alt_shape_and_expect_empty(ev):
    good = {**question("q1", "a", email=[1]), "alt": {"attachment": [2]}, "shape": "numbers"}
    bad = {**question("q2", "b", email=[1]), "alt": {"tickets": [2]}, "expect_empty": "yes"}

    problems = ev.validate_qa([good, bad])

    assert not [p for p in problems if p.startswith("q1:")]
    assert any(p.startswith("q2: alt") for p in problems)
    assert any(p.startswith("q2: expect_empty") for p in problems)
