"""Attachment summaries as a Vertex AI batch prediction job (issue 165).

A job sends the request each Phase 2 call would send online, byte for byte, as one JSONL
line keyed by custom_id in a Cloud Storage bucket. The rows it holds are left out of the
online pass until it is collected. Collecting parses each reply with the online parser and
stores it through the online loader, once; anything else stays pending for the online path.
Cloud Storage and the Vertex job API are stubbed: no test reaches the network.
"""

import argparse
import io
import json
import sys
from types import SimpleNamespace

import httpx2
import pytest
from anthropic import AnthropicVertex

from src import cli, llm_cost
from src.extract import attachment_pipeline as ap
from src.extract import claude_extract, untrusted, vertex_batch
from src.extract.attachment_prompt import split_text
from src.llm_cost import TokenBudget
from src.store.schema import create_database

SHORT = "Payments above 50,000 euros need two signatories. The board decided it in May. " * 4
# Over LONG_TEXT_CHARS, so it goes in parts.
LONG = "\n\n".join(["Paragraph about the regional plan. " + "word " * 400] * 70)[:130_000]
BUCKET = "gs://brain-batch-test/jobs"


def reply(summary="A summary of the policy.", **over):
    answer = {
        "language": "en",
        "summary": summary,
        "topics": ["payments policy"],
        "decisions": [
            {
                "evidence": "The board decided it in May.",
                "decision": "Two signatories above 50,000 euros",
                "decided_by": "the board",
            }
        ],
        "action_items": [],
        "key_facts": ["Payments above 50,000 euros need two signatories"],
    }
    answer.update(over)
    return json.dumps(answer)


def message(text, stop="end_turn", tokens=(1000, 100)):
    """A Claude message as Vertex returns it, as JSON."""
    return {
        "id": "msg_vrtx_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
    }


def ok(custom_id, text=None, **kw):
    return {"custom_id": custom_id, "request": {}, "response": message(text or reply(), **kw)}


def failed(custom_id, status="Bad Request: the request could not be served"):
    return {"custom_id": custom_id, "request": {}, "status": status}


class FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def upload(self, uri, stream, size):
        data = stream.read()
        assert len(data) == size
        self.objects[uri] = data

    def list(self, prefix):
        prefix = prefix.rstrip("/") + "/"
        return sorted(uri for uri in self.objects if uri.startswith(prefix))

    def lines(self, uri):
        yield from self.objects[uri].splitlines()


class FakeJobs:
    """The Vertex job API. `answer`, when set, turns a job's request lines into its output
    lines the first time the job is asked for."""

    def __init__(self, storage):
        self.storage = storage
        self.created: list[dict] = []
        self.status: dict[str, vertex_batch.JobStatus] = {}
        self.answer = None
        self.refuse = None

    def create(self, *, model, src, dest, display_name):
        if self.refuse:
            raise RuntimeError(self.refuse)
        name = f"projects/test-project/locations/eu/batchPredictionJobs/{len(self.created) + 1}"
        self.created.append(
            {"name": name, "model": model, "src": src, "dest": dest, "display_name": display_name}
        )
        self.status[name] = vertex_batch.JobStatus(name, "JOB_STATE_PENDING", None, None)
        return self.status[name]

    def get(self, name):
        if self.answer is not None and self.status[name].state == "JOB_STATE_PENDING":
            self.finish(name, self.answer(self.requests(name)))
        return self.status[name]

    def job(self, name=None):
        return (
            self.created[-1] if name is None else next(j for j in self.created if j["name"] == name)
        )

    def requests(self, name=None):
        data = self.storage.objects[self.job(name)["src"]]
        return [json.loads(line) for line in data.splitlines()]

    def finish(self, name, lines, state="JOB_STATE_SUCCEEDED"):
        out = f"{self.job(name)['dest']}/prediction-model-2026-10-11T12:00:00.000000Z"
        if lines:
            self.storage.objects[f"{out}/predictions.jsonl"] = b"".join(
                json.dumps(line, ensure_ascii=False).encode() + b"\n" for line in lines
            )
        error = None if state == "JOB_STATE_SUCCEEDED" else "the job did not finish"
        self.status[name] = vertex_batch.JobStatus(name, state, error, out)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_BATCH_BUCKET", BUCKET)
    monkeypatch.setenv("VERTEX_SDK_PROJECT", "test-project")
    monkeypatch.setenv("VERTEX_SDK_REGION", "eu")
    monkeypatch.setenv("CLAUDE_EXTRACT_MODEL", "claude-sonnet-5-5")
    for name in (
        "BRAIN_BATCH_LOCATION",
        "BRAIN_EXTRACT_CALL_SHAPE",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
        "VERTEX_MODEL_EXTRACT",
        "BRAIN_ATTACHMENT_TOKEN_BUDGET",
        "BRAIN_CHARS_PER_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def cloud(env, monkeypatch):
    storage = FakeStorage()
    jobs = FakeJobs(storage)
    seen: list = []

    def fake_cloud(project, location):
        seen.append((project, location))
        return storage, jobs

    monkeypatch.setattr(vertex_batch, "cloud", fake_cloud)
    return SimpleNamespace(storage=storage, jobs=jobs, seen=seen)


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (7, '2026-09-01', 'policy')"
    )
    conn.commit()
    yield path, conn
    conn.close()


def add(conn, text, *, method="direct_read", name="policy.txt", mime="text/plain", llm="pending"):
    """An attachment of the email and its content row; returns the content row's id."""
    att = conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at) VALUES (1, 7, ?, ?, 1, ?, '2026-09-01')",
        (name, mime, f"/x/{name}"),
    ).lastrowid
    ac = conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extracted_at, llm_status, summary)"
        " VALUES (?, ?, ?, 'extracted', '2026-09-01', ?, 'old summary')",
        (att, text, method, llm),
    ).lastrowid
    conn.commit()
    return ac


def status(conn, ac):
    return tuple(
        conn.execute(
            "SELECT llm_status, summary, llm_error FROM attachment_content WHERE id = ?", (ac,)
        ).fetchone()
    )


def holds(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM sync_metadata WHERE key LIKE ?", (f"{ap.BATCH_HOLD_KEY}%",)
    ).fetchone()[0]


def usage_lines(env):
    return [
        json.loads(line)
        for path in sorted(env.glob("llm-usage-*.jsonl"))
        for line in path.read_text().splitlines()
    ]


def answer_all(text=None):
    """Every request answered: a part, a single text, a digest and a merge alike."""
    return lambda requests: [ok(r["custom_id"], text) for r in requests]


def row_of(conn, ac):
    (row,) = ap.stored_rows(conn, [ac], set())
    return row


# --- the request ----------------------------------------------------------------


@pytest.mark.parametrize("shape", ["v1", "v2"])
def test_a_line_carries_the_online_request_body_byte_for_byte(shape, env, monkeypatch):
    """What the Vertex client would send for the call online, taken off the wire."""
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", shape)
    row = ap._Phase2Row(
        1, 2, SHORT + "«Γεια σας»", "policy.pdf", "application/pdf", 3, "Policy", "2026-05-12"
    )
    (call,) = ap.next_calls(row)
    kwargs = ap.call_kwargs(call.prompt)
    sent: list[bytes] = []

    def wire(request):
        sent.append(request.content)
        return httpx2.Response(200, json=message(reply()))

    client = AnthropicVertex(
        region="eu",
        project_id="test-project",
        access_token="unused",
        http_client=httpx2.Client(transport=httpx2.MockTransport(wire)),
        max_retries=0,
    )
    monkeypatch.setattr(
        claude_extract, "_get_client_and_model", lambda: (client, "claude-sonnet-5-5")
    )

    claude_extract.complete(**kwargs)

    assert vertex_batch.jsonl_line("ac1-s", kwargs) == (
        b'{"custom_id":"ac1-s","request":' + sent[0] + b"}\n"
    )


def test_a_request_with_a_parameter_the_body_does_not_know_is_refused():
    with pytest.raises(ValueError):
        vertex_batch.request_body({"max_tokens": 1, "temperature_typo": 1})


def _online_calls(row, monkeypatch):
    """The keyword arguments of each call the online path makes for `row`, in order."""
    calls: list[dict] = []

    def complete(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=reply())],
            stop_reason="end_turn",
            usage=None,
        )

    monkeypatch.setattr(claude_extract, "complete", complete)
    ac_id, _email, extraction, error, _verdict = ap._extract_one_attachment(row)
    assert error is None and extraction is not None
    return calls


@pytest.mark.parametrize("shape", ["v1", "v2"])
@pytest.mark.parametrize(
    "text, method",
    [
        (SHORT, "direct_read"),
        ("--- Sheet: Data ---\n" + "R | 1\n" * 12_000, "openpyxl"),
        (LONG, None),
    ],
    ids=["single", "digest", "parts"],
)
def test_the_batch_asks_what_the_online_path_asks(shape, text, method, env, monkeypatch):
    """Built through the same builders, so a part, a digest and a merge match online too."""
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", shape)
    monkeypatch.setattr(untrusted, "_new_tag", lambda: "untrusted_0123456789ab")
    row = ap._Phase2Row(
        11, 12, text, "doc.xlsx", "application/x", 3, "Policy", "2026-05-12", method
    )

    online = _online_calls(row, monkeypatch)
    first = ap.next_calls(row)

    assert [ap.call_kwargs(c.prompt) for c in first] == online[: len(first)]
    if len(online) > len(first):  # a long text: its parts, then the merge
        for call in first:
            ap.save_part(
                row.ac_id, call.part, call.key, ap.parse_reply(call.prompt, _reply_object())
            )
        (merge,) = ap.next_calls(row)
        assert merge.kind == "merge"
        assert [ap.call_kwargs(c.prompt) for c in first] + [ap.call_kwargs(merge.prompt)] == online


def _reply_object():
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=reply())], stop_reason="end_turn"
    )


# --- submit ---------------------------------------------------------------------


def test_submit_uploads_the_requests_and_records_the_job(cloud, store):
    path, conn = store
    first, second = add(conn, SHORT), add(conn, SHORT + " Another one.")

    summary = vertex_batch.submit(str(path))

    job = cloud.jobs.job()
    assert job["model"] == "publishers/anthropic/models/claude-sonnet-5-5"
    assert job["src"].startswith(BUCKET + "/") and job["src"].endswith("/input.jsonl")
    assert job["dest"] == job["src"].removesuffix("/input.jsonl") + "/output"
    assert cloud.seen == [("test-project", "eu")]
    lines = cloud.jobs.requests()
    assert [line["custom_id"] for line in lines] == [f"ac{first}-s", f"ac{second}-s"]
    assert {line["request"]["anthropic_version"] for line in lines} == {"vertex-2023-10-16"}
    assert "model" not in lines[0]["request"]
    assert (summary["job"], summary["requests"], summary["rows"]) == (job["name"], 2, 2)
    assert summary["input"] == job["src"] and summary["output"] == job["dest"]
    assert 0 < summary["batch_cost_usd"] < summary["online_cost_usd"]

    [(key, record)] = vertex_batch.open_jobs(conn)
    assert record["name"] == job["name"] and record["shape"] == "v1"
    assert record["rows"][str(first)]["ids"] == [f"ac{first}-s"]
    assert record["origin"] == "phase2" and not record["probe"]
    assert holds(conn) == 2


def test_a_held_row_is_left_out_of_the_online_pass_until_the_hold_lapses(cloud, store):
    path, conn = store
    held = add(conn, SHORT)

    vertex_batch.submit(str(path))

    assert ap.phase2_rows(conn) == []
    assert ap.estimate_phase2(str(path))["rows"] == 0
    # A job never collected must not keep its rows from the online path for good.
    conn.execute(
        "UPDATE sync_metadata SET value = '20200101T000000Z-aaaaaa' WHERE key = ?",
        (f"{ap.BATCH_HOLD_KEY}{held}",),
    )
    conn.commit()
    assert [row.ac_id for row in ap.phase2_rows(conn)] == [held]


def test_nothing_to_send_creates_no_job(cloud, store):
    path, conn = store
    add(conn, SHORT, llm="extracted")

    summary = vertex_batch.submit(str(path))

    assert summary["requests"] == 0 and cloud.jobs.created == [] and cloud.storage.objects == {}
    assert vertex_batch.open_jobs(conn) == []


def test_a_job_vertex_refuses_leaves_no_record_and_holds_nothing(cloud, store):
    path, conn = store
    add(conn, SHORT)
    cloud.jobs.refuse = "400 the location does not take batch jobs"

    with pytest.raises(RuntimeError, match="location"):
        vertex_batch.submit(str(path))

    assert vertex_batch.open_jobs(conn) == [] and holds(conn) == 0
    assert len(ap.phase2_rows(conn)) == 1


def test_the_token_budget_bounds_what_is_sent(cloud, store):
    """Held against the budget as the online path holds a call, before it is sent."""
    path, conn = store
    rows = [add(conn, SHORT + str(i)) for i in range(5)]
    one = TokenBudget(10**9).estimate(len(ap.next_calls(row_of(conn, rows[0]))[0].prompt))
    budget = TokenBudget(2 * one + one // 2)

    summary = vertex_batch.submit(str(path), token_budget=budget)

    assert summary["requests"] == 2 and summary["over_budget"] == 3
    assert [line["custom_id"] for line in cloud.jobs.requests()] == [f"ac{r}-s" for r in rows[:2]]
    assert [row.ac_id for row in ap.phase2_rows(conn)] == rows[2:]


def test_an_unusable_setting_stops_the_send_before_anything_is_sent(cloud, store, monkeypatch):
    """A setting the online path refuses to plan with stops the batch as well, with nothing
    uploaded, recorded or held."""
    path, conn = store
    add(conn, LONG)
    monkeypatch.setenv("BRAIN_ATTACHMENT_FULL_PARTS_UPTO", "eight")

    with pytest.raises(
        claude_extract.ClaudeNotConfigured, match="BRAIN_ATTACHMENT_FULL_PARTS_UPTO"
    ):
        vertex_batch.submit(str(path))

    assert cloud.storage.objects == {} and cloud.jobs.created == []
    assert vertex_batch.open_jobs(conn) == [] and holds(conn) == 0


def test_a_job_takes_at_most_its_cap(cloud, store, monkeypatch):
    path, conn = store
    rows = [add(conn, SHORT + str(i)) for i in range(4)]
    monkeypatch.setattr(vertex_batch, "MAX_REQUESTS", 3)

    summary = vertex_batch.submit(str(path))

    assert summary["requests"] == 3 and summary["left"] == 1
    assert [row.ac_id for row in ap.phase2_rows(conn)] == rows[3:]


# --- collect --------------------------------------------------------------------


def test_collect_loads_each_reply_once_and_leaves_the_rest_pending(cloud, store, env):
    path, conn = store
    good, bad, lost = add(conn, SHORT), add(conn, SHORT + " B"), add(conn, SHORT + " C")
    summary = vertex_batch.submit(str(path))
    cloud.jobs.finish(
        summary["job"], [ok(f"ac{good}-s", reply("Policy summary")), failed(f"ac{bad}-s")]
    )

    result = vertex_batch.collect(str(path))

    assert status(conn, good) == ("extracted", "Policy summary", None)
    assert status(conn, bad) == ("pending", "old summary", None)
    assert status(conn, lost) == ("pending", "old summary", None)
    decisions = conn.execute("SELECT decision, attachment_id FROM decisions").fetchall()
    assert [d[0] for d in decisions] == ["Two signatories above 50,000 euros"]
    assert conn.execute("SELECT COUNT(*) FROM key_facts").fetchone()[0] == 1
    assert holds(conn) == 0 and vertex_batch.open_jobs(conn) == []
    assert [row.ac_id for row in ap.phase2_rows(conn)] == [bad, lost]
    [job] = result["jobs"]
    assert job["counts"] == {"loaded": 1, "failed": 1, "missing": 1}
    logged = usage_lines(env)
    assert [(u["custom_id"], u["batch_job"]) for u in logged] == [(f"ac{good}-s", summary["job"])]

    again = vertex_batch.collect(str(path))

    assert again["jobs"] == []
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    assert len(usage_lines(env)) == 1


def test_a_reply_the_collect_was_interrupted_after_is_not_loaded_again(cloud, store, env):
    """The hold is the gate: a row whose hold is gone was resolved by an earlier collect."""
    path, conn = store
    first, second = add(conn, SHORT), add(conn, SHORT + " B")
    summary = vertex_batch.submit(str(path))
    cloud.jobs.finish(summary["job"], [ok(f"ac{first}-s"), ok(f"ac{second}-s")])
    [(key, _record)] = vertex_batch.open_jobs(conn)
    # As if a collect stopped after loading the first row, before marking the job done.
    conn.execute(
        "UPDATE attachment_content SET llm_status = 'extracted', summary = 'loaded' WHERE id = ?",
        (first,),
    )
    conn.execute("DELETE FROM sync_metadata WHERE key = ?", (f"{ap.BATCH_HOLD_KEY}{first}",))
    conn.commit()

    result = vertex_batch.collect(str(path))

    assert status(conn, first)[:2] == ("extracted", "loaded")
    assert status(conn, second)[0] == "extracted"
    assert result["jobs"][0]["counts"] == {"loaded": 1, "resolved before": 1}
    assert [u["custom_id"] for u in usage_lines(env)] == [f"ac{second}-s"]


@pytest.mark.parametrize(
    "shape, line",
    [
        ("v2", ok("ID", "", stop="refusal")),
        ("v2", ok("ID", reply()[:40], stop="max_tokens")),
        ("v1", ok("ID", "", stop="refusal")),
        ("v1", ok("ID", "I cannot summarise this.")),
        ("v1", {"custom_id": "ID", "request": {}, "response": {"type": "error", "error": {}}}),
        ("v1", {"custom_id": "ID", "request": {}, "response": {"unexpected": True}}),
    ],
    ids=["v2 refusal", "v2 cut short", "v1 refusal", "v1 no json", "error reply", "unknown shape"],
)
def test_a_reply_the_online_path_would_not_store_stays_pending(
    shape, line, cloud, store, env, monkeypatch
):
    """The online path asks again, with its refusal fallback and its larger second budget."""
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", shape)
    path, conn = store
    ac = add(conn, SHORT)
    summary = vertex_batch.submit(str(path))
    cloud.jobs.finish(summary["job"], [dict(line, custom_id=f"ac{ac}-s")])

    vertex_batch.collect(str(path))

    assert status(conn, ac) == ("pending", "old summary", None)
    assert [row.ac_id for row in ap.phase2_rows(conn)] == [ac]


def test_a_v1_reply_cut_short_is_salvaged_as_online(cloud, store):
    path, conn = store
    ac = add(conn, SHORT)
    summary = vertex_batch.submit(str(path))
    cut = reply("Cut short")[:-30]
    cloud.jobs.finish(summary["job"], [ok(f"ac{ac}-s", cut, stop="max_tokens")])

    vertex_batch.collect(str(path))

    assert status(conn, ac)[:2] == ("extracted", "Cut short")


def test_a_row_changed_since_it_was_sent_is_not_loaded(cloud, store, env):
    path, conn = store
    ac = add(conn, SHORT)
    summary = vertex_batch.submit(str(path))
    conn.execute("UPDATE attachment_content SET extracted_text = ? WHERE id = ?", (SHORT + "!", ac))
    conn.commit()
    cloud.jobs.finish(summary["job"], [ok(f"ac{ac}-s")])

    result = vertex_batch.collect(str(path))

    assert status(conn, ac) == ("pending", "old summary", None)
    assert result["jobs"][0]["counts"] == {"changed": 1}
    assert len(usage_lines(env)) == 1  # billed all the same


def test_parts_are_kept_for_the_merge_and_the_next_job_asks_for_it(cloud, store, monkeypatch):
    path, conn = store
    ac = add(conn, LONG)
    row = row_of(conn, ac)
    chosen = ap._choose_parts(split_text(LONG), False)

    first = vertex_batch.submit(str(path))
    assert [line["custom_id"] for line in cloud.jobs.requests()] == [f"ac{ac}-p{i}" for i in chosen]
    cloud.jobs.finish(first["job"], answer_all(reply("A part"))(cloud.jobs.requests()))
    vertex_batch.collect(str(path))

    assert status(conn, ac)[0] == "pending"
    assert [call.kind for call in ap.next_calls(row)] == ["merge"]
    second = vertex_batch.submit(str(path))
    assert [line["custom_id"] for line in cloud.jobs.requests()] == [f"ac{ac}-m"]
    cloud.jobs.finish(second["job"], [ok(f"ac{ac}-m", reply("The whole document"))])
    vertex_batch.collect(str(path))

    assert status(conn, ac)[:2] == ("extracted", "The whole document")
    assert not (ap.PARTS_DIR / f"{ac}.json").exists()


def test_parts_a_job_answered_are_reused_by_the_online_path(cloud, store, monkeypatch):
    path, conn = store
    ac = add(conn, LONG)
    summary = vertex_batch.submit(str(path))
    cloud.jobs.finish(summary["job"], answer_all()(cloud.jobs.requests()))
    vertex_batch.collect(str(path))

    online = _online_calls(row_of(conn, ac), monkeypatch)

    assert len(online) == 1  # the merge alone


def test_a_job_still_running_is_left_for_later(cloud, store):
    path, conn = store
    ac = add(conn, SHORT)
    vertex_batch.submit(str(path))

    result = vertex_batch.collect(str(path))

    assert result["jobs"][0]["state"] == "JOB_STATE_PENDING" and not result["jobs"][0]["done"]
    assert holds(conn) == 1 and len(vertex_batch.open_jobs(conn)) == 1
    assert status(conn, ac)[0] == "pending"


def test_a_job_that_failed_gives_its_rows_back(cloud, store):
    path, conn = store
    ac = add(conn, SHORT)
    summary = vertex_batch.submit(str(path))
    cloud.jobs.finish(summary["job"], [], state="JOB_STATE_FAILED")

    result = vertex_batch.collect(str(path))

    [job] = result["jobs"]
    assert job["done"] and job["error"] == "the job did not finish"
    assert job["counts"] == {"missing": 1}
    assert holds(conn) == 0 and [row.ac_id for row in ap.phase2_rows(conn)] == [ac]


@pytest.mark.parametrize("old", [True, False], ids=["hold lapsed", "hold live"])
def test_a_job_vertex_never_confirmed_gives_its_rows_back_once_its_hold_lapses(old, cloud, store):
    """The command stopped between recording the job and Vertex's answer."""
    path, conn = store
    ac = add(conn, SHORT)
    key = (
        "20200101T000000Z-abcdef"
        if old
        else vertex_batch.datetime.now(vertex_batch.UTC).strftime(ap.BATCH_KEY_TIME) + "-abcdef"
    )
    record = {
        "state": "submitting",
        "name": None,
        "origin": "phase2",
        "probe": False,
        "shape": "v1",
        "location": "eu",
        "requests": 1,
        "rows": {str(ac): {"att": 1, "sha": "x", "ids": [f"ac{ac}-s"]}},
    }
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES (?, ?)",
        (vertex_batch.JOB_KEY + key, json.dumps(record)),
    )
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES (?, ?)", (f"{ap.BATCH_HOLD_KEY}{ac}", key)
    )
    conn.commit()

    [job] = vertex_batch.collect(str(path))["jobs"]

    assert "never confirmed" in job["error"] and job["done"] is old
    assert holds(conn) == (0 if old else 1)
    assert len(vertex_batch.open_jobs(conn)) == (0 if old else 1)
    assert cloud.seen == []


def test_a_response_written_as_a_json_string_is_read(cloud, store):
    path, conn = store
    ac = add(conn, SHORT)
    summary = vertex_batch.submit(str(path))
    line = {"custom_id": f"ac{ac}-s", "response": json.dumps(message(reply("As a string")))}
    cloud.jobs.finish(summary["job"], [line, {"custom_id": f"ac{ac}-s", "response": "not json"}])

    vertex_batch.collect(str(path))

    assert status(conn, ac)[:2] == ("extracted", "As a string")


def test_collect_with_no_job_open_says_so(cloud, store, capsys):
    path, _conn = store

    assert vertex_batch.cli_collect(str(path)) == 0

    assert "No batch job is open" in capsys.readouterr().out


def test_a_job_sent_under_the_other_call_shape_is_not_collected(cloud, store, monkeypatch):
    path, conn = store
    ac = add(conn, SHORT)
    summary = vertex_batch.submit(str(path))
    cloud.jobs.finish(summary["job"], [ok(f"ac{ac}-s")])
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2")

    result = vertex_batch.collect(str(path))

    assert result["jobs"][0]["skipped"]
    assert status(conn, ac)[0] == "pending" and holds(conn) == 1


def test_reextract_jobs_drop_the_vectors_of_what_they_load(cloud, store, monkeypatch):
    path, conn = store
    loaded, kept = add(conn, SHORT), add(conn, SHORT + " B")
    removed: list = []
    monkeypatch.setattr(
        "src.store.embeddings.remove_vectors", lambda ids: removed.append(sorted(ids))
    )
    summary = vertex_batch.submit(str(path), origin="reextract")
    cloud.jobs.finish(summary["job"], [ok(f"ac{loaded}-s"), failed(f"ac{kept}-s")])

    vertex_batch.collect(str(path))

    assert removed == [[-loaded]]


# --- the usage log --------------------------------------------------------------


def test_batch_lines_are_priced_at_batch_rates(env):
    rates = llm_cost.RATES["claude-sonnet-5-5"]
    line = {
        "ts": "2026-10-10T10:00:00+00:00",
        "site": "src.extract.vertex_batch.collect",
        "model": "claude-sonnet-5-5",
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "cache_write_1h_tokens": 1_000_000,
    }
    (env / "llm-usage-2026-10.jsonl").write_text(
        json.dumps(line) + "\n" + json.dumps(dict(line, batch_job="projects/p/jobs/1")) + "\n"
    )
    from datetime import date

    day = llm_cost.usage_for_day(env, date(2026, 10, 10))

    online = rates.input + rates.output + rates.cache_read + rates.cache_write_1h
    batch = (
        rates.batch_input + rates.batch_output + rates.batch_cache_read + rates.batch_cache_write_1h
    )
    assert day["cost_usd"] == pytest.approx(online + batch)
    assert batch == pytest.approx(online / 2)


# --- the probe ------------------------------------------------------------------


def test_the_probe_sends_ten_requests_and_loads_nothing(cloud, store, env):
    path, conn = store
    rows = [add(conn, SHORT + str(i), llm="extracted") for i in range(12)]
    before = conn.execute(
        "SELECT id, llm_status, summary FROM attachment_content ORDER BY id"
    ).fetchall()

    summary = vertex_batch.probe(str(path))
    cloud.jobs.answer = answer_all(reply("Probe summary"))
    state = vertex_batch.wait(str(path), summary["key"], wait_s=60, poll_s=0, sleep=lambda s: None)
    result = vertex_batch.collect(str(path), only=summary["key"])

    assert summary["requests"] == 10 == len(cloud.jobs.requests())
    assert [line["custom_id"] for line in cloud.jobs.requests()] == [
        f"ac{r}-s" for r in rows[:1:-1]
    ]
    assert state == "JOB_STATE_SUCCEEDED"
    [job] = result["jobs"]
    assert job["probe"] and len(job["results"]) == 10
    assert {r["summary"] for r in job["results"]} == {"Probe summary"}
    assert job["usage"]["batch_cost_usd"] == pytest.approx(job["usage"]["online_cost_usd"] / 2)
    after = conn.execute(
        "SELECT id, llm_status, summary FROM attachment_content ORDER BY id"
    ).fetchall()
    assert after == before and holds(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    assert len(usage_lines(env)) == 10
    assert vertex_batch.open_jobs(conn) == []


def test_the_probe_takes_the_rows_the_selectors_choose(cloud, store):
    path, conn = store
    add(conn, SHORT)
    long_row = add(conn, "x" * 60_000, llm="extracted")

    summary = vertex_batch.probe(str(path), which={"long"})

    assert summary["rows"] == 1
    assert {line["custom_id"].split("-")[0] for line in cloud.jobs.requests()} == {f"ac{long_row}"}


def test_the_probe_wait_gives_up_after_its_time(cloud, store):
    path, conn = store
    add(conn, SHORT, llm="extracted")
    summary = vertex_batch.probe(str(path))
    clock = iter(range(0, 10_000, 30))

    state = vertex_batch.wait(
        str(path),
        summary["key"],
        wait_s=90,
        poll_s=30,
        sleep=lambda s: None,
        now=lambda: next(clock),
    )

    assert state == "JOB_STATE_PENDING"


# --- configuration and the replica ---------------------------------------------


def test_batch_mode_needs_a_bucket(env, monkeypatch):
    monkeypatch.setenv("BRAIN_BATCH_BUCKET", "")
    with pytest.raises(vertex_batch.BatchNotConfigured, match="BRAIN_BATCH_BUCKET"):
        vertex_batch.settings()
    monkeypatch.setenv("BRAIN_BATCH_BUCKET", "brain-batch-test")
    with pytest.raises(vertex_batch.BatchNotConfigured, match="gs://"):
        vertex_batch.settings()


def test_batch_mode_needs_a_vertex_project(env, monkeypatch):
    monkeypatch.setenv("VERTEX_SDK_PROJECT", "")
    with pytest.raises(vertex_batch.BatchNotConfigured, match="VERTEX_SDK_PROJECT"):
        vertex_batch.settings()


def test_the_location_is_the_extraction_region_unless_set(env, monkeypatch):
    assert vertex_batch.settings().location == "eu"
    monkeypatch.setenv("BRAIN_BATCH_LOCATION", "europe-west4")
    assert vertex_batch.settings().location == "europe-west4"
    monkeypatch.setenv("BRAIN_BATCH_LOCATION", "global")
    with pytest.raises(vertex_batch.BatchNotConfigured, match="global"):
        vertex_batch.settings()


def test_a_dated_model_is_named_as_vertex_names_it(env, monkeypatch):
    monkeypatch.setenv("CLAUDE_EXTRACT_MODEL", "claude-sonnet-4-5-20250929")
    assert vertex_batch.settings().model == "claude-sonnet-4-5@20250929"


@pytest.mark.parametrize("call", ["submit", "probe", "collect"])
def test_batch_mode_refuses_on_a_replica(call, cloud, store, monkeypatch):
    path, conn = store
    add(conn, SHORT)
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    with pytest.raises(vertex_batch.ReplicaRefused, match="replica"):
        getattr(vertex_batch, call)(str(path))

    assert cloud.jobs.created == [] and holds(conn) == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["process-attachments", "--phase", "2", "--batch"],
        ["process-attachments", "--batch-collect"],
        ["reextract", "--long", "--batch"],
        ["reextract", "--batch", "--probe"],
        ["reextract", "--batch-collect"],
    ],
)
def test_the_batch_commands_refuse_on_a_replica(argv, cloud, store, monkeypatch, capsys):
    path, conn = store
    add(conn, SHORT)
    monkeypatch.setenv("BRAIN_ROLE", "replica")
    monkeypatch.setattr(sys, "argv", ["brain", "--db", str(path), *argv])

    with pytest.raises(SystemExit) as stop:
        cli.main()

    assert stop.value.code == 2 and "replica" in capsys.readouterr().err
    assert cloud.jobs.created == [] and holds(conn) == 0


# --- the commands ---------------------------------------------------------------


def _pa_args(path, **over):
    args = argparse.Namespace(
        db=path,
        phase=2,
        type=None,
        limit=0,
        workers=1,
        deadline_s=None,
        token_budget=None,
        estimate=False,
        batch=True,
        batch_collect=False,
    )
    for key, value in over.items():
        setattr(args, key, value)
    return args


def test_process_attachments_batch_submits_phase_2(cloud, store, monkeypatch, capsys):
    path, conn = store
    add(conn, SHORT)
    monkeypatch.setattr(
        "src.extract.attachment_pipeline.run_phase2",
        lambda *a, **k: pytest.fail("Phase 2 must not run online"),
    )

    assert cli.cmd_process_attachments(_pa_args(path)) == 0

    out = capsys.readouterr().out
    assert cloud.jobs.job()["name"] in out and cloud.jobs.job()["src"] in out
    assert holds(conn) == 1


def test_process_attachments_batch_needs_phase_2(cloud, store, capsys):
    path, _conn = store

    assert cli.cmd_process_attachments(_pa_args(path, phase=1)) == 2

    assert "--phase 2" in capsys.readouterr().err and cloud.jobs.created == []


def test_a_missing_bucket_stops_the_command_before_any_work(cloud, store, monkeypatch, capsys):
    path, conn = store
    add(conn, SHORT)
    monkeypatch.setenv("BRAIN_BATCH_BUCKET", "")

    assert cli.cmd_process_attachments(_pa_args(path)) == 2

    assert "BRAIN_BATCH_BUCKET" in capsys.readouterr().err and holds(conn) == 0


def test_process_attachments_batch_collect_loads(cloud, store, capsys):
    path, conn = store
    ac = add(conn, SHORT)
    summary = vertex_batch.submit(str(path))
    cloud.jobs.finish(summary["job"], [ok(f"ac{ac}-s", reply("Collected"))])

    assert cli.cmd_process_attachments(_pa_args(path, batch=False, batch_collect=True)) == 0

    assert status(conn, ac)[:2] == ("extracted", "Collected")
    out = capsys.readouterr().out
    assert summary["job"] in out and "loaded" in out


def test_a_collect_that_finds_a_failed_job_exits_1(cloud, store):
    path, conn = store
    add(conn, SHORT)
    summary = vertex_batch.submit(str(path))
    cloud.jobs.finish(summary["job"], [], state="JOB_STATE_FAILED")

    assert cli.cmd_process_attachments(_pa_args(path, batch=False, batch_collect=True)) == 1


def _rx_args(path, root, **over):
    args = argparse.Namespace(
        db=str(path),
        capped=False,
        long=False,
        zip=False,
        unread=False,
        partial=False,
        stale=False,
        formats=False,
        ocr=False,
        garbled=False,
        limit=0,
        after_id=0,
        dry_run=False,
        workers=1,
        root=str(root),
        full_parts=None,
        token_budget=None,
        estimate=False,
        batch=False,
        probe=False,
        batch_collect=False,
    )
    for key, value in over.items():
        setattr(args, key, value)
    return args


def test_reextract_batch_reads_again_then_sends_the_summaries_as_a_job(
    cloud, store, tmp_path, monkeypatch, capsys
):
    path, conn = store
    ac = add(conn, "x" * 60_000, llm="extracted")
    monkeypatch.setattr(
        "src.extract.reextract.run_phase2",
        lambda *a, **k: pytest.fail("Phase 2 must not run online"),
    )

    assert cli.cmd_reextract(_rx_args(path, tmp_path, long=True, batch=True)) == 0

    assert status(conn, ac)[0] == "pending" and holds(conn) == 1
    [(_key, record)] = vertex_batch.open_jobs(conn)
    assert record["origin"] == "reextract"
    out = capsys.readouterr().out
    assert "resummarise" in out and cloud.jobs.job()["name"] in out


def test_reextract_probe_prints_the_results_and_the_cost(
    cloud, store, tmp_path, monkeypatch, capsys
):
    path, conn = store
    add(conn, SHORT, llm="extracted")
    cloud.jobs.answer = answer_all(reply("Probe summary"))
    monkeypatch.setattr(vertex_batch, "POLL_SECONDS", 0)

    assert cli.cmd_reextract(_rx_args(path, tmp_path, batch=True, probe=True)) == 0

    out = capsys.readouterr().out
    job = cloud.jobs.job()
    assert job["name"] in out and job["src"] in out and job["dest"] in out
    assert "Probe summary" in out and "$" in out
    assert status(conn, 1)[:2] == ("extracted", "old summary")


def test_the_batch_flags_reach_the_commands(monkeypatch):
    got: dict = {}
    monkeypatch.setattr(cli, "cmd_reextract", lambda args: got.update(vars(args)))
    monkeypatch.setattr(sys, "argv", ["brain", "reextract", "--long", "--batch", "--probe"])
    cli.main()
    assert (got["batch"], got["probe"], got["batch_collect"]) == (True, True, False)

    monkeypatch.setattr(cli, "cmd_process_attachments", lambda args: got.update(vars(args)))
    monkeypatch.setattr(sys, "argv", ["brain", "process-attachments", "--batch-collect"])
    cli.main()
    assert (got["batch"], got["batch_collect"]) == (False, True)


# --- the cloud adapters ---------------------------------------------------------


class FakeResponse:
    def __init__(self, body=None, lines=()):
        self.body = body or {}
        self._lines = list(lines)
        self.closed = False

    def raise_for_status(self):
        pass

    def json(self):
        return self.body

    def iter_lines(self, chunk_size=None):
        yield from self._lines

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list = []

    def _answer(self, method, url, **kw):
        if "data" in kw and hasattr(kw["data"], "read"):
            kw["data"] = kw["data"].read()
        self.calls.append((method, url, kw))
        return self.responses.pop(0)

    def post(self, url, **kw):
        return self._answer("POST", url, **kw)

    def get(self, url, **kw):
        return self._answer("GET", url, **kw)


def test_storage_uploads_by_name_and_checks_the_size():
    session = FakeSession(FakeResponse({"size": "5"}))

    vertex_batch.Storage(session).upload("gs://bkt/jobs/k/input.jsonl", io.BytesIO(b"lines"), 5)

    [(method, url, kw)] = session.calls
    assert (method, url) == ("POST", "https://storage.googleapis.com/upload/storage/v1/b/bkt/o")
    assert kw["params"] == {"uploadType": "media", "name": "jobs/k/input.jsonl"}
    assert kw["data"] == b"lines"


def test_storage_refuses_an_upload_that_did_not_arrive_whole():
    session = FakeSession(FakeResponse({"size": "0"}))

    with pytest.raises(RuntimeError, match="0 bytes of 5"):
        vertex_batch.Storage(session).upload("gs://bkt/in.jsonl", io.BytesIO(b"lines"), 5)


def test_storage_lists_every_page_under_a_prefix():
    session = FakeSession(
        FakeResponse({"items": [{"name": "jobs/k/output/a.jsonl"}], "nextPageToken": "t"}),
        FakeResponse({"items": [{"name": "jobs/k/output/b.jsonl"}]}),
    )

    names = vertex_batch.Storage(session).list("gs://bkt/jobs/k/output")

    assert names == ["gs://bkt/jobs/k/output/a.jsonl", "gs://bkt/jobs/k/output/b.jsonl"]
    assert session.calls[0][2]["params"]["prefix"] == "jobs/k/output/"
    assert session.calls[1][2]["params"]["pageToken"] == "t"


def test_storage_streams_an_objects_lines():
    response = FakeResponse(lines=[b'{"a": 1}', b"", b'{"b": 2}'])
    session = FakeSession(response)

    lines = list(vertex_batch.Storage(session).lines("gs://bkt/jobs/k/out put.jsonl"))

    assert lines == [b'{"a": 1}', b'{"b": 2}'] and response.closed
    [(_m, url, kw)] = session.calls
    assert url.endswith("/b/bkt/o/jobs%2Fk%2Fout%20put.jsonl") and kw["params"] == {"alt": "media"}


def test_jobs_create_and_read_a_vertex_batch_prediction_job():
    from google.genai import types

    created: list = []
    job = types.BatchJob(
        name="projects/p/locations/eu/batchPredictionJobs/9",
        state=types.JobState.JOB_STATE_SUCCEEDED,
        output_info=types.BatchJobOutputInfo(gcs_output_directory="gs://bkt/k/output/prediction-x"),
    )

    class Batches:
        def create(self, **kw):
            created.append(kw)
            return job

        def get(self, *, name):
            assert name == job.name
            return job

    jobs = vertex_batch.Jobs("p", "eu", client=SimpleNamespace(batches=Batches()))

    made = jobs.create(
        model="publishers/anthropic/models/m",
        src="gs://bkt/k/input.jsonl",
        dest="gs://bkt/k/output",
        display_name="brain-phase2-k",
    )
    read = jobs.get(job.name)

    [kw] = created
    assert (kw["model"], kw["src"]) == ("publishers/anthropic/models/m", "gs://bkt/k/input.jsonl")
    assert (kw["config"].dest, kw["config"].display_name) == ("gs://bkt/k/output", "brain-phase2-k")
    assert (
        made
        == read
        == vertex_batch.JobStatus(
            job.name, "JOB_STATE_SUCCEEDED", None, "gs://bkt/k/output/prediction-x"
        )
    )


def test_a_job_is_the_rest_call_google_documents(monkeypatch):
    """Taken off the wire: the request google-genai sends for Jobs.create and Jobs.get."""
    import httpx
    from google import genai
    from google.genai import types
    from google.oauth2.credentials import Credentials

    sent: list = []

    def wire(request):
        sent.append((request.method, str(request.url), request.content))
        return httpx.Response(
            200,
            json={
                "name": "projects/test-project/locations/eu/batchPredictionJobs/123",
                "state": "JOB_STATE_PENDING",
                "outputInfo": {"gcsOutputDirectory": "gs://bkt/k/output/prediction-model-x"},
            },
        )

    client = genai.Client(
        vertexai=True,
        project="test-project",
        location="eu",
        credentials=Credentials(token="unused"),
        http_options=types.HttpOptions(
            api_version="v1", httpx_client=httpx.Client(transport=httpx.MockTransport(wire))
        ),
    )
    jobs = vertex_batch.Jobs("test-project", "eu", client=client)

    made = jobs.create(
        model="publishers/anthropic/models/claude-sonnet-5-5",
        src="gs://bkt/k/input.jsonl",
        dest="gs://bkt/k/output",
        display_name="brain-phase2-k",
    )
    jobs.get(made.name)

    (method, url, body), (get_method, get_url, _) = sent
    base = "https://aiplatform.eu.rep.googleapis.com/v1/projects/test-project/locations/eu"
    assert (method, url) == ("POST", f"{base}/batchPredictionJobs")
    assert json.loads(body) == {
        "displayName": "brain-phase2-k",
        "model": "publishers/anthropic/models/claude-sonnet-5-5",
        "inputConfig": {
            "instancesFormat": "jsonl",
            "gcsSource": {"uris": ["gs://bkt/k/input.jsonl"]},
        },
        "outputConfig": {
            "predictionsFormat": "jsonl",
            "gcsDestination": {"outputUriPrefix": "gs://bkt/k/output"},
        },
    }
    assert (get_method, get_url) == ("GET", f"{base}/batchPredictionJobs/123")
    assert made == vertex_batch.JobStatus(
        "projects/test-project/locations/eu/batchPredictionJobs/123",
        "JOB_STATE_PENDING",
        None,
        "gs://bkt/k/output/prediction-model-x",
    )


def test_jobs_build_a_vertex_client_for_the_batch_location(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr("google.genai.Client", lambda **kw: seen.update(kw) or SimpleNamespace())

    vertex_batch.Jobs("test-project", "eu")

    assert (seen["vertexai"], seen["project"], seen["location"]) == (True, "test-project", "eu")
    assert seen["http_options"].api_version == "v1"


def test_the_cloud_is_reached_with_the_hosts_default_credentials(monkeypatch):
    asked: list = []
    sessions: list = []
    monkeypatch.setattr(
        "google.auth.default", lambda scopes=None: asked.append(scopes) or ("creds", None)
    )
    monkeypatch.setattr(
        "google.auth.transport.requests.AuthorizedSession",
        lambda credentials: sessions.append(credentials) or SimpleNamespace(),
    )
    monkeypatch.setattr("google.genai.Client", lambda **kw: SimpleNamespace())

    storage, jobs = vertex_batch.cloud("test-project", "eu")

    assert asked == [["https://www.googleapis.com/auth/cloud-platform"]] and sessions == ["creds"]
    assert isinstance(storage, vertex_batch.Storage) and isinstance(jobs, vertex_batch.Jobs)


# --- edges ----------------------------------------------------------------------


def test_an_empty_list_of_attachments_sends_nothing(cloud, store):
    path, conn = store
    add(conn, SHORT)

    assert vertex_batch.submit(str(path), attachment_ids=[])["requests"] == 0
    assert cloud.jobs.created == [] and holds(conn) == 0


def test_a_probe_with_nothing_to_read_sends_nothing(cloud, store, tmp_path, capsys):
    path, _conn = store

    assert cli.cmd_reextract(_rx_args(path, tmp_path, batch=True, probe=True)) == 1

    assert cloud.jobs.created == [] and "nothing to send" in capsys.readouterr().out


def test_a_probe_the_wait_runs_out_on_is_left_to_collect(
    cloud, store, tmp_path, monkeypatch, capsys
):
    path, conn = store
    add(conn, SHORT, llm="extracted")
    monkeypatch.setattr(vertex_batch, "PROBE_WAIT_SECONDS", 0)

    assert cli.cmd_reextract(_rx_args(path, tmp_path, batch=True, probe=True)) == 0

    assert "--batch-collect" in capsys.readouterr().out
    assert len(vertex_batch.open_jobs(conn)) == 1


def test_the_batch_commands_stop_on_a_missing_project(cloud, store, tmp_path, monkeypatch, capsys):
    path, _conn = store
    monkeypatch.setenv("VERTEX_SDK_PROJECT", "")

    assert cli.cmd_reextract(_rx_args(path, tmp_path, batch=True, probe=True)) == 2
    assert cli.cmd_reextract(_rx_args(path, tmp_path, long=True, batch=True)) == 2
    assert "VERTEX_SDK_PROJECT" in capsys.readouterr().err


def test_lines_that_are_not_replies_are_skipped(cloud, store):
    path, conn = store
    ac = add(conn, SHORT)
    summary = vertex_batch.submit(str(path))
    cloud.jobs.finish(summary["job"], [ok(f"ac{ac}-s", reply("Kept"))])
    out = cloud.jobs.status[summary["job"]].output_dir
    cloud.storage.objects[f"{out}/predictions.jsonl"] += b'not json\n[1, 2]\n{"no": "id"}\n'
    cloud.storage.objects[f"{out}/errors.txt"] = b"not read"

    vertex_batch.collect(str(path))

    assert status(conn, ac)[:2] == ("extracted", "Kept")


def test_a_reply_from_a_model_without_a_rate_is_counted_unpriced(cloud, store):
    path, conn = store
    ac = add(conn, SHORT)
    summary = vertex_batch.submit(str(path))
    line = ok(f"ac{ac}-s")
    line["response"]["model"] = "claude-unlisted"
    cloud.jobs.finish(summary["job"], [line])

    [job] = vertex_batch.collect(str(path))["jobs"]

    assert job["usage"]["unpriced"] == 1 and job["usage"]["batch_cost_usd"] == 0


def test_every_kind_of_outcome_prints(capsys):
    base = {"key": "k", "name": "projects/p/jobs/1", "origin": "phase2", "requests": 3}
    vertex_batch._print_job({**base, "skipped": "sent in call shape v2", "done": False})
    vertex_batch._print_job(
        {**base, "skipped": None, "done": False, "state": "JOB_STATE_RUNNING", "error": None}
    )
    vertex_batch._print_job(
        {
            **base,
            "skipped": None,
            "done": True,
            "state": "JOB_STATE_SUCCEEDED",
            "error": None,
            "probe": False,
            "counts": {"loaded": 1, "parts saved": 2, "failed": 1, "resolved before": 1},
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "batch_cost_usd": 0.5,
                "online_cost_usd": 1.0,
            },
        }
    )
    vertex_batch._print_job(
        {
            **base,
            "skipped": None,
            "done": True,
            "state": "JOB_STATE_SUCCEEDED",
            "error": None,
            "probe": True,
            "counts": {"failed": 1},
            "results": [
                {
                    "custom_id": "ac1-s",
                    "attachment_id": 2,
                    "outcome": "failed",
                    "error": "no response",
                }
            ],
        }
    )
    vertex_batch.print_submit({"requests": 0, "over_budget": 4, "left": 0})
    vertex_batch.print_submit({"requests": 0, "over_budget": 0, "left": 7})

    out = capsys.readouterr().out
    assert "not collected" in out and "JOB_STATE_RUNNING" in out
    assert "parts saved" in out and "collected before" in out and "left pending  : 1" in out
    assert "$0.5000" in out and "no response" in out
    assert "nothing to send" in out and "token budget" in out and "sends the rest" in out
