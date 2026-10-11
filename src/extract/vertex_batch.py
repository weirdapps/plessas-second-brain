"""Attachment summaries as a Vertex AI batch prediction job, at half the online price (#165).

Phase 2 (`process-attachments --phase 2 --batch`) and `reextract --batch` can send their model
calls as one batch job instead of one request each: Vertex AI serves Claude to batch jobs at
half its online rate (src/llm_cost.py). Anthropic's Message Batches API is not on Vertex AI,
and the anthropic SDK's Vertex client refuses it, so this is Vertex's own batch prediction, as
Google documents it for Claude ("Batch predictions with Anthropic Claude models"): a JSONL file
in Cloud Storage, one line per request, `{"custom_id": ..., "request": <the body>}`, a job in a
region that serves the model (not the global endpoint), and its answers written back to the
bucket as JSONL keyed by custom_id, complete when every line is done or after 24 hours. The
job API is google-genai's `batches` on Vertex AI; the bucket is read and written through
Cloud Storage's JSON API with the host's Application Default Credentials (google-auth, which
anthropic[vertex] already requires). No new dependency.

Submit. The rows Phase 2 would take now (attachment_pipeline.phase2_ids), each planned as the
online path plans it (next_calls: one call over a text or a spreadsheet's digest, the parts of
a long text not answered yet, or its merge), each line the body the Vertex client would send
online for that call, byte for byte (request_body). The run's token budget holds each request
as it holds an online call, and a job takes at most MAX_REQUESTS. The file goes to
BRAIN_BATCH_BUCKET, the job to BRAIN_BATCH_LOCATION (the extraction region by default). The
job and each row's custom_ids are one sync_metadata row (JOB_KEY + the job's key), and each
row sent is held (attachment_pipeline.BATCH_HOLD_KEY), so the online pass leaves it to the job
for up to BATCH_HOLD_HOURS. No schema change.

Collect (`--batch-collect`). For each finished job, each reply the online path would store is
parsed by the online parser (parse_reply) and stored by the online loader (apply_call, which
saves a part where the online path looks for it, so a long document's merge goes in the next
job, or online). Everything else stays pending for the online path, untouched: a failed or
missing line, a refusal (the online path has the refusal fallback), a v2 reply cut short (the
online path asks again at twice the budget), a reply the parser cannot read, and a row changed
since it was sent. A row's hold is released in the transaction that stores it, and only the
collect that releases it stores it, so collecting twice stores nothing twice. Each answered
line goes in the usage log with its job's name, priced at batch rates. A reextract job sends one
row per document and records the copies of each (`copies`); its collect gives them the summary
it stores for that row (reextract.copy_summaries) and drops their vectors too.

Probe (`reextract --batch --probe`). Ten requests built from stored text, nothing held; it waits
for the job and prints each parsed reply and the cost, and stores nothing.

Every entry point refuses on a replica.
"""

import hashlib
import json
import math
import os
import secrets
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO, Any
from urllib.parse import quote

from src.config import DATA_ROOT, DEFAULT_DB, is_replica, replica_refusal
from src.extract import attachment_pipeline as ap
from src.extract import claude_extract
from src.extract.call_shape import Request, call_shape, prompt_hash
from src.extract.claude_extract import (
    ClaudeNotConfigured,
    claude_model,
    vertex_model_id,
    vertex_region,
)
from src.llm_cost import (
    ESTIMATE_MODEL,
    OUTPUT_TOKENS_PER_CALL,
    RATES,
    OverBudget,
    TokenBudget,
    configured_chars_per_token,
    cost_usd,
    rates_for,
)
from src.store.file_sweep import OCR_GARBLED_SQL

BUCKET_VAR = "BRAIN_BATCH_BUCKET"
LOCATION_VAR = "BRAIN_BATCH_LOCATION"
PROJECT_VARS = ("VERTEX_SDK_PROJECT", "ANTHROPIC_VERTEX_PROJECT_ID")

# What the anthropic SDK's Vertex client adds to every body (its DEFAULT_VERSION), and what
# Google's batch format asks each request for.
ANTHROPIC_VERSION = "vertex-2023-10-16"

# The body's keys in the order the SDK writes them (Messages.create), so a line holds the
# online body byte for byte. `model` is not one: the Vertex client moves it into the URL,
# and the job names it.
_BODY_ORDER = (
    "max_tokens",
    "messages",
    "cache_control",
    "container",
    "inference_geo",
    "metadata",
    "output_config",
    "service_tier",
    "stop_sequences",
    "stream",
    "system",
    "thinking",
    "tool_choice",
    "tools",
)

# One sync_metadata row per job: JSON with its Vertex name, origin, call shape, files, and the
# custom_ids of each content row it was sent for.
JOB_KEY = "attachment_batch_job:"

# The requests one job takes. A line holds a prompt of up to about 100 kB, so this keeps a
# job's file near 200 MB; a larger backlog goes in several jobs (Vertex runs 4 at a time in a
# project by default).
MAX_REQUESTS = 2_000
PROBE_REQUESTS = 10
POLL_SECONDS = 30
PROBE_WAIT_SECONDS = 3_600

# Where a job's file is written before it is uploaded. The file is deleted as it is closed: it
# holds the documents' text.
BATCH_DIR = DATA_ROOT / "state" / "batch"

# A job's states with nothing more to come. A paused job can resume, so PAUSED is not one.
ENDED = frozenset(
    {
        "JOB_STATE_SUCCEEDED",
        "JOB_STATE_FAILED",
        "JOB_STATE_CANCELLED",
        "JOB_STATE_EXPIRED",
        "JOB_STATE_PARTIALLY_SUCCEEDED",
    }
)

# The usage log's site for a batch job's answers.
SITE = f"{__name__}.collect"

_CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_STORAGE_API = "https://storage.googleapis.com"
_HTTP_TIMEOUT_S = 300
_CODES = {"single": "s", "digest": "d", "merge": "m"}


class BatchNotConfigured(ClaudeNotConfigured):
    """A setting batch mode needs is unset or unusable."""


class ReplicaRefused(RuntimeError):
    """A batch job was to be sent or collected on a replica (src.config.is_replica)."""


def _refuse_on_replica(what: str) -> None:
    if is_replica():
        raise ReplicaRefused(replica_refusal(what))


# --- settings ---------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    project: str
    location: str
    # The model as Vertex AI names it, '@' before a date.
    model: str
    # gs://bucket or gs://bucket/prefix, with no trailing slash.
    base: str


def _project() -> str:
    for name in PROJECT_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise BatchNotConfigured(
        "Batch mode runs on Vertex AI: set VERTEX_SDK_PROJECT (or ANTHROPIC_VERTEX_PROJECT_ID)"
        " to the project that serves the Claude model"
    )


def settings() -> Settings:
    """What a job is sent with, from the environment: BatchNotConfigured names what is missing."""
    raw = os.environ.get(BUCKET_VAR, "").strip()
    if not raw:
        raise BatchNotConfigured(
            f"{BUCKET_VAR} is unset: batch mode writes its requests to a Cloud Storage bucket"
            " in the job's region, gs://bucket or gs://bucket/prefix (docs/DEPLOY.md, section 11)"
        )
    base = raw.rstrip("/")
    if not base.startswith("gs://") or len(base) <= len("gs://"):
        raise BatchNotConfigured(
            f"{BUCKET_VAR} must name a Cloud Storage bucket as gs://bucket or"
            f" gs://bucket/prefix, not {raw!r}"
        )
    project = _project()
    location = os.environ.get(LOCATION_VAR, "").strip() or vertex_region()
    if location == "global":
        raise BatchNotConfigured(
            "Vertex AI runs no batch job for a partner model at the global endpoint: set"
            f" {LOCATION_VAR} to a region that serves the model"
        )
    return Settings(project, location, vertex_model_id(claude_model()), base)


def config_problem() -> str | None:
    """Why batch mode cannot run here, or None when it can: for a command to stop early."""
    try:
        settings()
    except ClaudeNotConfigured as e:
        return str(e)
    return None


# --- the request ------------------------------------------------------------------


def request_body(kwargs: dict) -> dict:
    """The body the Vertex client sends for claude_extract.complete(**kwargs): the SDK's keys
    in its order, then `anthropic_version`. ValueError for a key it would not send this way."""
    unknown = sorted(set(kwargs) - set(_BODY_ORDER))
    if unknown:
        raise ValueError(f"not a request parameter a batch line carries: {', '.join(unknown)}")
    body = {key: kwargs[key] for key in _BODY_ORDER if key in kwargs}
    body["anthropic_version"] = ANTHROPIC_VERSION
    return body


def jsonl_line(custom_id: str, kwargs: dict) -> bytes:
    """One line of a job's input: the custom_id and the request body, serialised as the SDK
    serialises a body (compact separators, UTF-8 rather than ASCII escapes)."""
    line = {"custom_id": custom_id, "request": request_body(kwargs)}
    text = json.dumps(line, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return text.encode() + b"\n"


def custom_id(ac_id: int, call: ap.Phase2Call) -> str:
    """The key of a call's line: its content row and its call, `ac<id>-s` for a single text,
    `-d` a digest, `-p<i>` the part with index i, `-m` the merge."""
    code = f"p{call.part}" if call.kind == "part" else _CODES[call.kind]
    return f"ac{ac_id}-{code}"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _chars(prompt: str | Request) -> int:
    return prompt.chars if isinstance(prompt, Request) else len(prompt)


# --- the cloud --------------------------------------------------------------------


@dataclass(frozen=True)
class JobStatus:
    name: str
    state: str
    error: str | None
    # The Cloud Storage directory the job wrote its answers to, once it says so.
    output_dir: str | None


def _split(uri: str) -> tuple[str, str]:
    """gs://bucket/name -> (bucket, name)."""
    bucket, _, name = uri.removeprefix("gs://").partition("/")
    return bucket, name


class Storage:
    """Cloud Storage through its JSON API, with the host's Application Default Credentials."""

    def __init__(self, session: Any = None):
        if session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(scopes=[_CLOUD_SCOPE])
            session = AuthorizedSession(credentials)
        self._session = session

    def upload(self, uri: str, stream: IO[bytes], size: int) -> None:
        """Write `size` bytes from `stream` to `uri`, and check that all of them arrived."""
        bucket, name = _split(uri)
        response = self._session.post(
            f"{_STORAGE_API}/upload/storage/v1/b/{bucket}/o",
            params={"uploadType": "media", "name": name},
            data=stream,
            headers={"Content-Type": "application/jsonl"},
            timeout=_HTTP_TIMEOUT_S,
        )
        response.raise_for_status()
        stored = int(response.json().get("size", -1))
        if stored != size:
            raise RuntimeError(f"{uri}: {stored} bytes of {size} arrived")

    def list(self, prefix_uri: str) -> list[str]:
        """The objects under a prefix, as gs:// URIs."""
        bucket, prefix = _split(prefix_uri.rstrip("/") + "/")
        names: list[str] = []
        token = None
        while True:
            params = {"prefix": prefix, "fields": "items(name),nextPageToken"}
            if token:
                params["pageToken"] = token
            response = self._session.get(
                f"{_STORAGE_API}/storage/v1/b/{bucket}/o", params=params, timeout=_HTTP_TIMEOUT_S
            )
            response.raise_for_status()
            body = response.json()
            names += [f"gs://{bucket}/{item['name']}" for item in body.get("items", [])]
            token = body.get("nextPageToken")
            if not token:
                return names

    def lines(self, uri: str) -> Iterator[bytes]:
        """An object's non-empty lines, streamed."""
        bucket, name = _split(uri)
        response = self._session.get(
            f"{_STORAGE_API}/storage/v1/b/{bucket}/o/{quote(name, safe='')}",
            params={"alt": "media"},
            stream=True,
            timeout=_HTTP_TIMEOUT_S,
        )
        try:
            response.raise_for_status()
            for line in response.iter_lines(chunk_size=1 << 16):
                if line:
                    yield line
        finally:
            response.close()


class Jobs:
    """Vertex AI batch prediction jobs, through google-genai."""

    def __init__(self, project: str, location: str, client: Any = None):
        if client is None:
            from google import genai
            from google.genai.types import HttpOptions

            # Project and location given, so a Gemini API key in the environment is not used.
            client = genai.Client(
                vertexai=True,
                project=project,
                location=location,
                http_options=HttpOptions(api_version="v1"),
            )
        self._client = client

    def create(self, *, model: str, src: str, dest: str, display_name: str) -> JobStatus:
        from google.genai.types import CreateBatchJobConfig

        config = CreateBatchJobConfig(dest=dest, display_name=display_name)
        return _job_status(self._client.batches.create(model=model, src=src, config=config))

    def get(self, name: str) -> JobStatus:
        return _job_status(self._client.batches.get(name=name))


def _job_status(job: Any) -> JobStatus:
    state = getattr(job.state, "value", job.state)
    error = getattr(job.error, "message", None) if job.error else None
    output = job.output_info.gcs_output_directory if job.output_info else None
    return JobStatus(job.name, str(state or "JOB_STATE_UNSPECIFIED"), error, output)


def cloud(project: str, location: str) -> tuple[Storage, Jobs]:
    """The bucket and the job API a job is sent to and read from."""
    return Storage(), Jobs(project, location)


# --- job records ------------------------------------------------------------------


def _write(conn, key: str, record: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)",
        (JOB_KEY + key, json.dumps(record, ensure_ascii=False)),
    )


def _read(conn, key: str) -> dict:
    (value,) = conn.execute(
        "SELECT value FROM sync_metadata WHERE key = ?", (JOB_KEY + key,)
    ).fetchone()
    return json.loads(value)


def open_jobs(conn) -> list[tuple[str, dict]]:
    """The jobs not collected yet, oldest first, as (key, record)."""
    jobs = []
    for full_key, value in conn.execute(
        "SELECT key, value FROM sync_metadata WHERE key GLOB ? ORDER BY key", (JOB_KEY + "*",)
    ):
        record = json.loads(value)
        if record.get("state") != "collected":
            jobs.append((full_key[len(JOB_KEY) :], record))
    return jobs


def _release(conn, key: str) -> None:
    """Every hold a job still has."""
    conn.execute(
        "DELETE FROM sync_metadata WHERE key GLOB ? AND value = ?", (ap.BATCH_HOLD_KEY + "*", key)
    )


# --- submit -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Planned:
    """A request written to a job's file, by what the job's record keeps of it."""

    ac_id: int
    att_id: int
    sha: str
    custom_id: str
    chars: int


@dataclass
class _Plan:
    planned: list[_Planned]
    # What stopped the requests: 'left' (a job's cap) or 'over_budget'; None when nothing did.
    stop: str | None = None
    # How many rows sent any request.
    used: int = 0
    # What the calls pin, for the usage log; every call of the attachment route pins the same.
    pins: dict | None = None


def _pins(kwargs: dict) -> dict:
    output_config = kwargs.get("output_config") or {}
    return {
        "max_tokens": kwargs.get("max_tokens"),
        "effort": output_config.get("effort"),
        "prompt_hash": prompt_hash(kwargs.get("system"), output_config.get("format")),
    }


def _plan(rows: Iterable[Any], budget: TokenBudget | None, cap: int, out: IO[bytes]) -> _Plan:
    """Write the requests for these rows to `out`, in order, until the cap or the token budget
    stops them, each held against the budget as an online call is. No row is read past the
    stop, and no prompt is kept: each goes to the file as it is built."""
    plan = _Plan([])
    for row in rows:
        sha = _sha(row.text)
        took = 0
        for call in ap.next_calls(row):
            if len(plan.planned) >= cap:
                plan.stop = "left"
            elif budget is not None:
                try:
                    budget.reserve(_chars(call.prompt))
                except OverBudget:
                    plan.stop = "over_budget"
            if plan.stop is not None:
                plan.used += 1 if took else 0
                return plan
            cid = custom_id(row.ac_id, call)
            kwargs = ap.call_kwargs(call.prompt)
            out.write(jsonl_line(cid, kwargs))
            plan.pins = plan.pins or _pins(kwargs)
            plan.planned.append(_Planned(row.ac_id, row.att_id, sha, cid, _chars(call.prompt)))
            took += 1
        plan.used += 1
    return plan


def _batch_file() -> IO[bytes]:
    """A file for a job's lines, deleted as it is closed: it holds the documents' text."""
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryFile(dir=BATCH_DIR)


def _estimate(planned: list[_Planned]) -> dict:
    """Tokens and cost of the requests, estimated as a dry run estimates them."""
    cpt = configured_chars_per_token()
    input_tokens = sum(math.ceil(p.chars / cpt) for p in planned)
    output_tokens = OUTPUT_TOKENS_PER_CALL * len(planned)
    rates = RATES[ESTIMATE_MODEL]
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "batch_cost_usd": cost_usd(
            rates, input_tokens=input_tokens, output_tokens=output_tokens, batch=True
        ),
        "online_cost_usd": cost_usd(rates, input_tokens=input_tokens, output_tokens=output_tokens),
    }


def _send(
    conn,
    s: Settings,
    plan: _Plan,
    f: IO[bytes],
    *,
    origin: str,
    probe: bool,
    copies: dict[int, list[int]] | None = None,
) -> dict:
    """Upload the file `plan` wrote, record the job (holding its rows, unless a probe), and
    create it. `copies` (row -> its copies) is recorded for the rows sent."""
    key = f"{datetime.now(UTC).strftime(ap.BATCH_KEY_TIME)}-{secrets.token_hex(3)}"
    src = f"{s.base}/{key}/input.jsonl"
    dest = f"{s.base}/{key}/output"
    storage, jobs = cloud(s.project, s.location)
    size = f.tell()
    f.seek(0)
    storage.upload(src, f, size)

    rows: dict[str, dict] = {}
    for p in plan.planned:
        entry = rows.setdefault(str(p.ac_id), {"att": p.att_id, "sha": p.sha, "ids": []})
        entry["ids"].append(p.custom_id)
    record: dict[str, Any] = {
        "state": "submitting",
        "name": None,
        "origin": origin,
        "probe": probe,
        "shape": call_shape(),
        "model": s.model,
        "location": s.location,
        "input": src,
        "output": dest,
        "submitted_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "requests": len(plan.planned),
        "call": plan.pins,
        "rows": rows,
        **(
            {"copies": {str(ac): others for ac, others in copies.items() if str(ac) in rows}}
            if copies
            else {}
        ),
    }
    # Recorded and held before the job exists, so the online pass never takes a row the job
    # is about to answer; taken back if Vertex refuses the job.
    _write(conn, key, record)
    if not probe:
        conn.executemany(
            "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)",
            [(f"{ap.BATCH_HOLD_KEY}{ac}", key) for ac in rows],
        )
    conn.commit()
    try:
        status = jobs.create(
            model=f"publishers/anthropic/models/{s.model}",
            src=src,
            dest=dest,
            display_name=f"brain-{origin}-{key}",
        )
    except BaseException:
        _release(conn, key)
        conn.execute("DELETE FROM sync_metadata WHERE key = ?", (JOB_KEY + key,))
        conn.commit()
        raise
    record.update(state="submitted", name=status.name, vertex_state=status.state)
    _write(conn, key, record)
    conn.commit()
    return {
        "key": key,
        "job": status.name,
        "state": status.state,
        "probe": probe,
        "input": src,
        "output": dest,
        "requests": len(plan.planned),
        "rows": len(rows),
        **_estimate(plan.planned),
    }


def _nothing(left: dict) -> dict:
    return {"requests": 0, "rows": 0, **left, **_estimate([])}


def submit(
    db_path=None,
    *,
    limit: int = 0,
    file_type: str | None = None,
    attachment_ids: list[int] | None = None,
    token_budget: TokenBudget | None = None,
    origin: str = "phase2",
    copies: dict[int, list[int]] | None = None,
) -> dict:
    """Send the rows Phase 2 would take now as one batch job: see the module docstring.

    `attachment_ids` keeps to those attachments (reextract passes the rows it read again, with
    origin 'reextract', whose collect also drops the vectors of what it stores). `copies`
    (content id -> the content ids of its copies) gives each copy, at collect, the summary
    stored for its row (reextract.copy_summaries). Returns the
    job's key and name, its files, what was sent, what was left (`over_budget` by the token
    budget, `left` by MAX_REQUESTS) and the estimated cost."""
    _refuse_on_replica("sending a batch job")
    s = settings()
    if attachment_ids is not None and not attachment_ids:
        return _nothing({"over_budget": 0, "left": 0})
    conn = ap._connect(str(db_path or DEFAULT_DB))
    try:
        ids = ap.phase2_ids(conn, file_type=file_type, attachment_ids=attachment_ids, limit=limit)
        rows = ap.stored_rows(conn, ids, ap.full_parts_ids(conn))
        with _batch_file() as f:
            plan = _plan(rows, token_budget, MAX_REQUESTS, f)
            left = {"over_budget": 0, "left": 0}
            if plan.stop is not None:
                left[plan.stop] = len(ids) - plan.used
            if not plan.planned:
                return _nothing(left)
            sent = _send(conn, s, plan, f, origin=origin, probe=False, copies=copies)
            return {**sent, **left}
    finally:
        conn.close()


def _probe_ids(conn, which: set[str], after_id: int | None) -> list[int]:
    """The rows a probe reads: those the reextract selectors choose, or else the newest rows
    that hold text."""
    if which:
        from src.extract.reextract import select_rows

        return [row[0] for row in select_rows(conn, which, None, after_id) if row[5]]
    return [
        ac_id
        for (ac_id,) in conn.execute(
            "SELECT ac.id FROM attachment_content ac WHERE ac.extraction_status = 'extracted'"
            " AND ac.extracted_text IS NOT NULL"
            f" AND COALESCE(ac.extraction_method, '') != 'vision' AND NOT {OCR_GARBLED_SQL}"
            " ORDER BY ac.id DESC LIMIT ?",
            (PROBE_REQUESTS,),
        )
    ]


def probe(db_path=None, which: Iterable[str] = (), after_id: int | None = None) -> dict:
    """Send a job of PROBE_REQUESTS requests built from the stored text of the rows the
    selectors choose (the newest rows that hold text with none), whatever their status. It
    holds no row and stores nothing: collect prints its replies."""
    _refuse_on_replica("a batch probe")
    s = settings()
    conn = ap._connect(str(db_path or DEFAULT_DB))
    try:
        ids = _probe_ids(conn, set(which), after_id)
        rows = ap.stored_rows(conn, ids, ap.full_parts_ids(conn))
        with _batch_file() as f:
            plan = _plan(rows, None, PROBE_REQUESTS, f)
            if not plan.planned:
                return _nothing({"over_budget": 0, "left": 0})
            return _send(conn, s, plan, f, origin="probe", probe=True)
    finally:
        conn.close()


def wait(
    db_path,
    key: str,
    *,
    wait_s: float = PROBE_WAIT_SECONDS,
    poll_s: float = POLL_SECONDS,
    sleep: Callable[[float], Any] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> str:
    """Poll a job until it ends or `wait_s` passes; its last state."""
    conn = ap._connect(str(db_path or DEFAULT_DB))
    try:
        record = _read(conn, key)
    finally:
        conn.close()
    _storage, jobs = cloud(_project(), record["location"])
    deadline = now() + wait_s
    while True:
        state = jobs.get(record["name"]).state
        if state in ENDED or now() >= deadline:
            return state
        sleep(poll_s)


# --- collect ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Reply:
    # The Claude message a line answered with, or why there is none.
    response: Any
    error: str | None


def _reply(line: dict) -> _Reply:
    from anthropic.types import Message
    from pydantic import ValidationError

    status = line.get("status")
    if isinstance(status, str) and status.strip():
        return _Reply(None, status.strip()[:300])
    response = line.get("response")
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return _Reply(None, "a response that is not JSON")
    if not isinstance(response, dict):
        return _Reply(None, "no response")
    if response.get("type") == "error":
        error = response.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        return _Reply(None, f"an error response: {message or 'no message'}"[:300])
    try:
        return _Reply(Message.model_validate(response), None)
    except ValidationError:
        keys = ", ".join(sorted(str(k) for k in response)[:8])
        return _Reply(None, f"a response that is not a message (keys: {keys})")


def _read_replies(storage: Storage, prefix: str) -> dict[str, _Reply]:
    """Every answered line under a job's output, by custom_id; an answer wins over an error."""
    replies: dict[str, _Reply] = {}
    for uri in storage.list(prefix):
        if not uri.endswith(".jsonl"):
            continue
        for raw in storage.lines(uri):
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(line, dict) or not isinstance(line.get("custom_id"), str):
                continue
            cid = line["custom_id"]
            reply = _reply(line)
            if cid not in replies or replies[cid].response is None:
                replies[cid] = reply
    return replies


class _Usage:
    """The tokens and the cost of a job's answers."""

    def __init__(self) -> None:
        self.tokens: Counter[str] = Counter()
        self.batch_cost = 0.0
        self.online_cost = 0.0
        self.unpriced = 0

    def add(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return

        def count(value: Any) -> int:
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        used = {
            "input_tokens": count(getattr(usage, "input_tokens", None)),
            "output_tokens": count(getattr(usage, "output_tokens", None)),
            "cache_read_tokens": count(getattr(usage, "cache_read_input_tokens", None)),
            "cache_write_tokens": count(getattr(usage, "cache_creation_input_tokens", None)),
        }
        ttl = getattr(usage, "cache_creation", None)
        one_hour = min(
            count(getattr(ttl, "ephemeral_1h_input_tokens", None)), used["cache_write_tokens"]
        )
        self.tokens.update(used)
        rates = rates_for(getattr(response, "model", None))
        if rates is None:
            self.unpriced += 1
            return
        for batch in (True, False):
            cost = cost_usd(
                rates,
                input_tokens=used["input_tokens"],
                output_tokens=used["output_tokens"],
                cache_read_tokens=used["cache_read_tokens"],
                cache_write_tokens=used["cache_write_tokens"] - one_hour,
                cache_write_1h_tokens=one_hour,
                batch=batch,
            )
            if batch:
                self.batch_cost += cost
            else:
                self.online_cost += cost

    def summary(self) -> dict:
        return {
            **{
                key: self.tokens[key]
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                )
            },
            "batch_cost_usd": self.batch_cost,
            "online_cost_usd": self.online_cost,
            "unpriced": self.unpriced,
        }


def _log(record: dict, cid: str, response: Any) -> None:
    call = record.get("call") or {}
    claude_extract._log_usage(
        response,
        SITE,
        max_tokens=call.get("max_tokens"),
        effort=call.get("effort"),
        prompt_hash=call.get("prompt_hash"),
        batch_job=record.get("name"),
        custom_id=cid,
    )


def _outcome(reply: _Reply | None, call: ap.Phase2Call | None, shape: str) -> Any:
    """(call, extraction) for a reply the online path would store; otherwise why not."""
    if reply is None:
        return "missing"
    if reply.response is None:
        return "failed"
    if call is None:
        return "changed"
    stop = getattr(reply.response, "stop_reason", None)
    if stop == "refusal":
        return "refused"
    if stop == "max_tokens" and shape == "v2":
        return "cut short"
    try:
        return call, ap.parse_reply(call.prompt, reply.response)
    except Exception:
        return "unreadable"


def _planned_calls(row: Any) -> dict[str, ap.Phase2Call]:
    return {custom_id(row.ac_id, call): call for call in ap.next_calls(row)} if row else {}


def _load(conn, key: str, record: dict, replies: dict[str, _Reply]) -> tuple[dict, list[int], dict]:
    """Store what a finished job answered: (counts by outcome, rows stored, usage)."""
    counts: Counter[str] = Counter()
    loaded: list[int] = []
    usage = _Usage()
    stats = dict.fromkeys(("processed", "extracted", "failed", "deferred", "over_budget"), 0)
    flagged = ap.full_parts_ids(conn)
    for ac_key, entry in record["rows"].items():
        ac_id = int(ac_key)
        # The gate: only the collect that releases a row's hold stores its answers.
        released = conn.execute(
            "DELETE FROM sync_metadata WHERE key = ? AND value = ?",
            (f"{ap.BATCH_HOLD_KEY}{ac_id}", key),
        ).rowcount
        if not released:
            conn.commit()
            counts["resolved before"] += 1
            continue
        for cid in entry["ids"]:
            reply = replies.get(cid)
            if reply is not None and reply.response is not None:
                _log(record, cid, reply.response)
                usage.add(reply.response)
        found = ap.phase2_rows(conn, attachment_ids=[entry["att"]], flagged=flagged)
        # Still Phase 2's work, with the text it was sent with; none when not (a reply then
        # finds no call, and counts as changed).
        row: Any = found[0] if found and _sha(found[0].text) == entry["sha"] else None
        planned = _planned_calls(row)
        for cid in entry["ids"]:
            outcome = _outcome(replies.get(cid), planned.get(cid), record["shape"])
            if isinstance(outcome, str):
                counts[outcome] += 1
                continue
            call, extraction = outcome
            ap.apply_call(conn, stats, row, call, extraction)
            if call.kind == "part":
                counts["parts saved"] += 1
            else:
                counts["loaded"] += 1
                loaded.append(ac_id)
        conn.commit()
    return dict(counts), loaded, usage.summary()


def _probe_results(conn, record: dict, replies: dict[str, _Reply]) -> tuple[list[dict], dict]:
    """A probe's replies, parsed as collect parses them, and stored nowhere."""
    results: list[dict] = []
    usage = _Usage()
    flagged = ap.full_parts_ids(conn)
    for ac_key, entry in record["rows"].items():
        found = list(ap.stored_rows(conn, [int(ac_key)], flagged))
        row = found[0] if found and _sha(found[0].text) == entry["sha"] else None
        planned = _planned_calls(row)
        for cid in entry["ids"]:
            reply = replies.get(cid)
            item: dict[str, Any] = {"custom_id": cid, "attachment_id": entry["att"]}
            if reply is not None and reply.response is not None:
                _log(record, cid, reply.response)
                usage.add(reply.response)
                tokens = getattr(reply.response, "usage", None)
                item.update(
                    stop_reason=reply.response.stop_reason,
                    input_tokens=getattr(tokens, "input_tokens", None),
                    output_tokens=getattr(tokens, "output_tokens", None),
                )
            outcome = _outcome(reply, planned.get(cid), record["shape"])
            if isinstance(outcome, str):
                item.update(outcome=outcome, error=reply.error if reply else None)
            else:
                _call, extraction = outcome
                item.update(
                    outcome="parsed",
                    language=extraction.get("language"),
                    summary=extraction.get("summary"),
                    **{
                        key: len(extraction.get(key) or [])
                        for key in ("topics", "decisions", "action_items", "key_facts")
                    },
                )
            results.append(item)
    return results, usage.summary()


def _close(conn, key: str, record: dict, state: str, error: str | None, counts: dict) -> None:
    """Mark a job collected, keeping a short record of it: its rows' mapping goes."""
    kept = (
        "name",
        "origin",
        "probe",
        "shape",
        "model",
        "location",
        "input",
        "output",
        "submitted_at",
        "requests",
    )
    closed = {name: record.get(name) for name in kept}
    closed.update(
        state="collected",
        vertex_state=state,
        error=error,
        counts=counts,
        collected_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    _release(conn, key)
    _write(conn, key, closed)
    conn.commit()


def _collect_job(conn, key: str, record: dict) -> dict:
    out: dict[str, Any] = {
        "key": key,
        "name": record.get("name"),
        "origin": record.get("origin"),
        "probe": bool(record.get("probe")),
        "requests": record.get("requests", 0),
        "state": record.get("vertex_state"),
        "done": False,
        "skipped": None,
        "error": None,
        "counts": {},
    }
    shape = record.get("shape")
    if shape != call_shape():
        out["skipped"] = (
            f"sent in call shape {shape}; collect it with BRAIN_EXTRACT_CALL_SHAPE={shape}"
        )
        return out
    if not record.get("name"):
        # Recorded, and Vertex never confirmed it (the command stopped in between). Once its
        # hold has lapsed, nothing can come of it.
        out["error"] = "Vertex AI never confirmed this job"
        if key < ap.batch_hold_cutoff():
            out["done"] = True
            _close(conn, key, record, "never confirmed", out["error"], {})
        return out
    storage, jobs = cloud(_project(), record["location"])
    status = jobs.get(record["name"])
    out["state"] = status.state
    if status.state not in ENDED:
        return out
    out["done"] = True
    out["error"] = status.error
    replies = _read_replies(storage, status.output_dir or record["output"])
    if record.get("probe"):
        out["results"], out["usage"] = _probe_results(conn, record, replies)
        out["counts"] = dict(Counter(item["outcome"] for item in out["results"]))
    else:
        out["counts"], loaded, out["usage"] = _load(conn, key, record, replies)
        copies = record.get("copies") or {}
        if loaded and copies:
            # The copies of a document reextract sent once take the summary just stored.
            from src.extract.reextract import copy_summaries

            copied = copy_summaries(
                conn, {ac: copies[str(ac)] for ac in loaded if str(ac) in copies}
            )
            if copied:
                out["counts"]["duplicates"] = len(copied)
                loaded = loaded + copied
        if loaded and record.get("origin") == "reextract":
            # As reextract does online: a row with a new summary is embedded again.
            from src.store.embeddings import remove_vectors

            remove_vectors([-ac_id for ac_id in loaded])
    _close(conn, key, record, status.state, status.error, out["counts"])
    return out


def collect(db_path=None, *, only: str | None = None) -> dict:
    """Collect every open job that has finished (`only`: that job alone): see the module
    docstring. A job still running is left as it is."""
    _refuse_on_replica("collecting a batch job")
    conn = ap._connect(str(db_path or DEFAULT_DB))
    try:
        return {
            "jobs": [
                _collect_job(conn, key, record)
                for key, record in open_jobs(conn)
                if only is None or key == only
            ]
        }
    finally:
        conn.close()


# --- the commands -----------------------------------------------------------------


def print_submit(summary: dict) -> None:
    """What a submit sent, where, and what it left."""
    if summary.get("requests"):
        print(f"  batch job     : {summary['job']}")
        print(f"  input         : {summary['input']}")
        print(f"  output        : {summary['output']}")
        print(f"  requests      : {summary['requests']:,} for {summary['rows']:,} attachments")
    else:
        print("  batch job     : nothing to send")
    if summary.get("over_budget"):
        print(f"  left pending  : {summary['over_budget']:,} (token budget reached)")
    if summary.get("left"):
        print(
            f"  left pending  : {summary['left']:,} (a job takes {MAX_REQUESTS:,} requests:"
            " `process-attachments --phase 2 --batch` sends the rest)"
        )
    if summary.get("requests"):
        print(
            f"  estimate      : {summary['input_tokens']:,} input and"
            f" {summary['output_tokens']:,} output tokens,"
            f" ${summary['batch_cost_usd']:,.2f} as a batch job"
            f" (${summary['online_cost_usd']:,.2f} online)"
        )
    if summary.get("requests") and not summary.get("probe"):
        print(
            "  collect with  : python -m src.cli process-attachments --batch-collect,"
            " once the job has ended (within 24 hours)"
        )


def _print_job(job: dict) -> None:
    head = f"batch job {job['name'] or job['key']} ({job['origin']}, {job['requests']:,} requests)"
    if job["skipped"]:
        print(f"{head}: not collected, {job['skipped']}")
        return
    if not job["done"]:
        extra = f" ({job['error']})" if job["error"] else ""
        print(f"{head}: {job['state']}{extra}, not finished; collect it again later")
        return
    print(f"{head}: {job['state']}" + (f" ({job['error']})" if job["error"] else ""))
    counts = job["counts"]
    if job["probe"]:
        for item in job.get("results", []):
            tokens = ""
            if item.get("input_tokens") is not None:
                tokens = f", {item['input_tokens']:,} in / {item['output_tokens']:,} out"
            print(
                f"  {item['custom_id']} (attachment {item['attachment_id']}):"
                f" {item['outcome']}, {item.get('stop_reason') or 'no reply'}{tokens}"
            )
            if item["outcome"] == "parsed":
                print(
                    f"      {item['language']}; {item['topics']} topics, {item['decisions']}"
                    f" decisions, {item['action_items']} action items, {item['key_facts']} key facts"
                )
                print(f"      {(item['summary'] or '')[:300]}")
            elif item.get("error"):
                print(f"      {item['error']}")
    else:
        print(f"  loaded        : {counts.get('loaded', 0):,}")
        if counts.get("parts saved"):
            print(f"  parts saved   : {counts['parts saved']:,} (their merges go in the next job)")
        if counts.get("duplicates"):
            print(f"  duplicates    : {counts['duplicates']:,} (took the summary of their copy)")
        done = ("loaded", "parts saved", "duplicates", "resolved before")
        left = {k: v for k, v in counts.items() if k not in done}
        if left:
            reasons = ", ".join(f"{k} {v:,}" for k, v in sorted(left.items()))
            print(f"  left pending  : {sum(left.values()):,} ({reasons})")
        if counts.get("resolved before"):
            print(f"  collected before: {counts['resolved before']:,} rows")
    usage = job.get("usage")
    if usage:
        print(
            f"  tokens        : {usage['input_tokens']:,} input, {usage['output_tokens']:,} output,"
            f" {usage['cache_read_tokens']:,} cache read, {usage['cache_write_tokens']:,}"
            " cache written"
        )
        print(
            f"  cost          : ${usage['batch_cost_usd']:,.4f} as a batch job"
            f" (${usage['online_cost_usd']:,.4f} online)"
        )


def cli_collect(db_path) -> int:
    """`--batch-collect`: 1 when a job was not collected for its call shape, or ended with an
    error and stored nothing."""
    try:
        result = collect(db_path)
    except ClaudeNotConfigured as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    if not result["jobs"]:
        print("No batch job is open.")
        return 0
    bad = False
    for job in result["jobs"]:
        _print_job(job)
        stored = job["counts"].get("loaded") or job["counts"].get("parts saved")
        bad |= bool(job["skipped"]) or (job["done"] and bool(job["error"]) and not stored)
    return 1 if bad else 0


def cli_process_attachments(args, db_path: str, budget: TokenBudget | None) -> int:
    """`process-attachments --phase 2 --batch` and `--batch-collect`."""
    if getattr(args, "batch_collect", False):
        return cli_collect(db_path)
    if getattr(args, "phase", None) != 2:
        print(
            "Error: --batch sends Phase 2's calls as a batch job: add --phase 2, and run"
            " Phase 1 on its own first",
            file=sys.stderr,
        )
        return 2
    problem = config_problem()
    if problem:
        print(f"Error: {problem}", file=sys.stderr)
        return 2
    print("Phase 2: a Vertex AI batch job...")
    summary = submit(
        db_path,
        limit=getattr(args, "limit", 0) or 0,
        file_type=getattr(args, "type", None),
        token_budget=budget,
    )
    print_submit(summary)
    return 0


def cli_reextract(args, which: set[str]) -> int:
    """`reextract --batch --probe` and `reextract --batch-collect`."""
    if getattr(args, "batch_collect", False):
        return cli_collect(args.db)
    try:
        sent = probe(args.db, which, getattr(args, "after_id", 0) or None)
    except ClaudeNotConfigured as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    print(f"Batch probe: {sent['requests']} requests, nothing held, nothing stored")
    print_submit(sent)
    if not sent["requests"]:
        return 1
    minutes = PROBE_WAIT_SECONDS // 60
    print(
        f"Waiting for the job, up to {minutes} minutes. It runs on if this stops:"
        " `python -m src.cli reextract --batch-collect` prints its replies later."
    )
    state = wait(args.db, sent["key"], wait_s=PROBE_WAIT_SECONDS, poll_s=POLL_SECONDS)
    if state not in ENDED:
        print(f"The job is {state} after {minutes} minutes; collect it later.")
        return 0
    result = collect(args.db, only=sent["key"])
    parsed = 0
    for job in result["jobs"]:
        _print_job(job)
        parsed += job["counts"].get("parsed", 0)
    return 0 if parsed else 1
