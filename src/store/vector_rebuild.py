"""`brain maint vectors-rebuild`: the whole index again, in this code's space, then one swap.

The index moves to a new space only whole: an index never mixes two (embeddings.py). So the
rebuild embeds every summary (emails, attachments, conversations, Teams threads, WhatsApp
sessions) and every chunk of the long documents (chunks.py) into a side directory beside
the index, embeddings.rebuild/, in parts, and swaps the finished index in at once
(embeddings.install_index), keeping the old one as embeddings.npz.pre-rebuild.

It resumes: every part and every batch job sent is recorded in embeddings.rebuild/state.json,
so a run stopped by its token budget, a signal or an error carries on where it was. It uses
the Gemini API's batch mode (half the price, a 24-hour target turnaround, usually far less)
unless told --online or the embedding backend is Vertex AI, which has no embedding batch
jobs; online it sends 100 texts a call with the ingest path's pauses and backoff.

While it runs it holds embeddings.rebuild_lock_path(), and every writer that adds vectors
(sync's embedding pass, teams-sync, whatsapp-sync) adds none: the rebuild embeds what they
would have. Before the swap it catches up, embedding what the store gained since it began
and dropping what it lost, and after it a Teams thread or WhatsApp session it embedded is
stamped as embedded when its text was read. Nothing else needs stopping: the timers go on
ingesting, and the MCP server goes on answering from the old index, then reloads the new one
on its next query (the cache follows the file's mtime).
"""

import json
import shutil
import sqlite3
import sys
import time
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from src.config import embed_backend
from src.store import embeddings as emb
from src.store.chunks import CHUNK_CHARS, ChunkPlan, attachment_head, split_text
from src.store.stamps import instant

# Measured 2026-10-16 with count_tokens on the reference replica: 3.56 characters a token for
# template-2 summary texts (60 texts) and 2.66 for chunk texts (40 texts, Greek and figures).
# The estimate rounds both down, so it errs high.
CHARS_PER_TOKEN_SUMMARY = 3.5
CHARS_PER_TOKEN_CHUNK = 2.6
# Dollars per million input tokens, (online, batch), from Google's pricing page.
PRICES = {"gemini-embedding-2": (0.20, 0.10), "gemini-embedding-001": (0.15, 0.075)}
# Online: what generate_embeddings sends, 100 texts a call with a 1.5 s pause between calls.
ONLINE_TEXTS_PER_MINUTE = 2_400
# Texts per part online, and per batch job: a job's output is about 9 KB a vector as JSON,
# so 10,000 texts download as some 90 MB.
ONLINE_PART_TEXTS = 5_000
BATCH_JOB_TEXTS = 10_000
# A batch job also closes at this many (estimated) tokens. The Gemini API caps the tokens a
# key may have queued in embedding batch jobs at once (500,000 at tier 1, 5 million at tier
# 2, 10 million at tier 3, its rate-limits page, 2026-10-16), and refuses with a 429 a job
# that would pass it. A job of 10,000 chunks, some 17 million tokens, would never be taken;
# jobs this size go out as the queue drains (the rolling window in rebuild()).
BATCH_JOB_TOKENS = 1_000_000
# The smallest a job is halved to when the queue takes none at all; and how many rounds in a
# row the queue may refuse with none of this build's jobs in it before the rebuild gives up
# (another job of the key's may hold the queue, so it waits a while first).
BATCH_JOB_MIN_TOKENS = 50_000
IDLE_REFUSALS = 10
POLL_SECONDS = 60
# A batch job that fails this many times sends its texts online instead.
BATCH_ATTEMPTS = 2

_DONE_STATES = ("JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED")
_FAILED_STATES = ("JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED")


class RebuildRefused(RuntimeError):
    """The side directory holds another space's build, or the store cannot be rebuilt."""


class BatchQueueFull(RuntimeError):
    """The service refused a batch job with a 429: the key's queue of batch tokens is full."""


def _quota(exc: Exception) -> bool:
    return getattr(exc, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(exc)


def side_dir() -> Path:
    return emb.EMBEDDINGS_FILE.with_name("embeddings.rebuild")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _kind(vid: int) -> str:
    if vid > 0:
        return "email"
    if vid <= emb.CHUNK_ID_OFFSET:
        return "chunk"
    if vid <= emb.WHATSAPP_THREAD_ID_OFFSET:
        return "whatsapp"
    if vid <= emb.TEAMS_THREAD_ID_OFFSET:
        return "teams"
    if vid <= emb.CONVERSATION_ID_OFFSET:
        return "conversation"
    return "attachment"


def _tokens(vid: int, text: str) -> int:
    ratio = CHARS_PER_TOKEN_CHUNK if vid <= emb.CHUNK_ID_OFFSET else CHARS_PER_TOKEN_SUMMARY
    return int(len(text) / ratio) + 1


@dataclass
class Wanted:
    """What an index in the space should hold: every summary's (id, text), and the chunk
    plan, whose texts are made an attachment at a time (chunk_stream)."""

    summaries: list[tuple[int, str]]
    plan: ChunkPlan | None

    def ids(self) -> set[int]:
        out = {vid for vid, _text in self.summaries}
        if self.plan is not None:
            out |= emb.chunk_ids(self.plan)
        return out

    def stream(self, conn: sqlite3.Connection, skip: set[int]) -> Iterator[tuple[int, str]]:
        """(id, text) of everything not in `skip`: the summaries, then the chunks."""
        for vid, text in self.summaries:
            if vid not in skip:
                yield vid, text
        if self.plan is None:
            return
        plan = self.plan
        wanted = emb.chunk_ids(plan) - skip
        yield from emb.chunk_items(conn, plan, sorted(plan.spans, reverse=True), wanted)


def _wanted(conn: sqlite3.Connection, space: dict) -> Wanted:
    """The items of `space`, chunks cut first where the store has none (writes
    attachment_chunks)."""
    plan = emb._plan_chunks(conn, None) if emb.chunks_wanted(space) else None
    return Wanted(emb.summary_items(conn, space), plan)


# --- the side directory ---------------------------------------------------------------------


def _state_path() -> Path:
    return side_dir() / "state.json"


def _load_state() -> dict | None:
    path = _state_path()
    return json.loads(path.read_text()) if path.exists() else None


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(path)


def _write_part(state: dict, ids: list[int], vectors: np.ndarray, read_at: str) -> None:
    name = f"part-{len(state['parts']) + 1:05d}.npz"
    np.savez(
        side_dir() / name,
        ids=np.asarray(ids, dtype=np.int64),
        vectors=np.asarray(vectors).astype(emb.STORE_DTYPE),
        read_at=np.array(read_at),
    )
    state["parts"].append(name)
    _save_state(state)


def _part_ids(state: dict) -> set[int]:
    out: set[int] = set()
    for name in state["parts"]:
        with np.load(side_dir() / name, allow_pickle=False) as data:
            out |= {int(v) for v in data["ids"]}
    return out


def _job_ids(job: dict) -> list[int]:
    return np.load(side_dir() / job["ids"], allow_pickle=False).tolist()


# --- the two ways of embedding ----------------------------------------------------------------


def _embed_online(client, space: dict, pairs: list[tuple[int, str]]) -> np.ndarray:
    return emb.generate_embeddings([t for _v, t in pairs], client, space=space)


def _submit(
    client, state: dict, space: dict, pairs: list[tuple[int, str]], attempts: int = 1
) -> None:
    """Send `pairs` as one batch job: a JSONL file of requests keyed by vector id. Raises
    BatchQueueFull, having deleted the file it uploaded, when the service's queue is full."""
    n = state.get("next_job", len(state["jobs"]) + 1)
    request_file = side_dir() / f"job-{n:05d}.jsonl"
    with request_file.open("w") as f:
        for vid, text in pairs:
            request: dict[str, Any] = {"content": {"parts": [{"text": text}]}}
            if int(space["dim"]) != emb.NATIVE_DIM:
                request["output_dimensionality"] = int(space["dim"])
            f.write(json.dumps({"key": str(vid), "request": request}, ensure_ascii=False) + "\n")
    ids_name = f"job-{n:05d}.ids.npy"
    np.save(side_dir() / ids_name, np.asarray([v for v, _t in pairs], dtype=np.int64))
    uploaded = client.files.upload(
        file=str(request_file), config={"mime_type": "jsonl", "display_name": request_file.stem}
    )
    request_file.unlink()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # the SDK calls embedding batch jobs experimental
            job = client.batches.create_embeddings(
                model=space["model"],
                src={"file_name": uploaded.name},
                config={"display_name": f"brain-vectors-{request_file.stem}"},
            )
    except Exception as e:
        # The texts uploaded do not stay on the service for a job that never ran.
        _forget_remote(client, {"file": uploaded.name}, None)
        (side_dir() / ids_name).unlink(missing_ok=True)
        if _quota(e):
            raise BatchQueueFull(str(e)[:200]) from e
        raise
    state["next_job"] = n + 1
    state["jobs"].append(
        {
            "name": job.name,
            "file": uploaded.name,
            "ids": ids_name,
            "read_at": _now(),
            "attempts": attempts,
            "tokens": sum(_tokens(v, t) for v, t in pairs),
        }
    )
    _save_state(state)


def _state_name(job) -> str:
    state = getattr(job, "state", None)
    return getattr(state, "name", None) or str(state).rsplit(".", 1)[-1]


def _collect(client, state: dict, space: dict, report: dict) -> list[int]:
    """Collect the batch jobs that finished; returns the ids to embed online instead: those a
    job returned no vector for, and those of a job that failed BATCH_ATTEMPTS times."""
    online: list[int] = []
    pending = []
    for job in state["jobs"]:
        found = client.batches.get(name=job["name"])
        name = _state_name(found)
        if name in _DONE_STATES:
            ids = _job_ids(job)
            raw = client.files.download(file=found.dest.file_name)
            vectors = np.zeros((len(ids), int(space["dim"])), dtype=emb.STORE_DTYPE)
            position = {vid: i for i, vid in enumerate(ids)}
            got = np.zeros(len(ids), dtype=bool)
            for line in raw.decode("utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                i = position.get(int(record.get("key", 0)))
                values = ((record.get("response") or {}).get("embedding") or {}).get("values")
                if i is None or not values or len(values) != vectors.shape[1]:
                    continue
                vectors[i] = np.asarray(values, dtype=np.float32)
                got[i] = True
                usage = (record.get("response") or {}).get("usageMetadata") or {}
                report["tokens_billed"] += int(usage.get("promptTokenCount") or 0)
            kept = [vid for vid, ok in zip(ids, got, strict=True) if ok]
            if kept:
                _write_part(state, kept, vectors[got], job["read_at"])
            online += [vid for vid, ok in zip(ids, got, strict=True) if not ok]
            _forget_remote(client, job, found)
            state["jobs_done"] = state.get("jobs_done", 0) + 1
            report["jobs_collected"] += 1
        elif name in _FAILED_STATES:
            _forget_remote(client, job, found)
            if job["attempts"] >= BATCH_ATTEMPTS:
                online += _job_ids(job)
            else:
                state.setdefault("retry", []).append(
                    {"ids": _job_ids(job), "attempts": job["attempts"]}
                )
            emb._log(f"vector rebuild: batch job {job['name']} ended {name}")
        else:
            pending.append(job)
    state["jobs"] = pending
    _save_state(state)
    return online


def _forget_remote(client, job: dict, found) -> None:
    """Delete a job's request file and the job on the service: they hold the texts sent. The
    output file goes with its job (files.delete refuses its name, over 40 characters;
    verified 2026-10-16)."""
    deletes = []
    if job.get("file"):
        deletes.append((client.files.delete, job["file"]))
    if job.get("name"):
        deletes.append((client.batches.delete, job["name"]))
    for delete, name in deletes:
        try:
            delete(name=name)
        except Exception as e:  # noqa: BLE001 - the service keeps them 48 hours at worst
            emb._log(f"vector rebuild: could not delete {name}: {type(e).__name__}")


# --- the command ----------------------------------------------------------------------------


def estimate(conn: sqlite3.Connection, space: dict | None = None) -> dict:
    """What a rebuild into `space` would send, without a call or a write: texts, characters
    and tokens by kind, the dollars online and as batch jobs, and the minutes online."""
    from src.store.chunks import eligible

    space = space or emb.embedding_space()
    by_kind: dict[str, dict] = {}
    for vid, text in emb.summary_items(conn, space):
        row = by_kind.setdefault(_kind(vid), {"texts": 0, "chars": 0, "tokens": 0})
        row["texts"] += 1
        row["chars"] += len(text)
        row["tokens"] += _tokens(vid, text)
    if emb.chunks_wanted(space):
        row = by_kind.setdefault("chunk", {"texts": 0, "chars": 0, "tokens": 0, "documents": 0})
        for aid, cid, _at, _n in eligible(conn):
            text = (
                conn.execute(
                    "SELECT extracted_text FROM attachment_content WHERE id = ?", (cid,)
                ).fetchone()[0]
                or ""
            )
            spans = split_text(text)
            filename, head = attachment_head(conn, aid)
            prefix = len(f"title: {filename} | text: {head}\n\n")
            chars = sum(end - start for start, end in spans) + prefix * len(spans)
            row["documents"] += 1
            row["texts"] += len(spans)
            row["chars"] += chars
            row["tokens"] += int(chars / CHARS_PER_TOKEN_CHUNK) + len(spans)
    online, batch = PRICES.get(space["model"], (0.0, 0.0))
    tokens = sum(r["tokens"] for r in by_kind.values())
    texts = sum(r["texts"] for r in by_kind.values())
    return {
        "space": space,
        "by_kind": by_kind,
        "texts": texts,
        "tokens": tokens,
        "usd_online": round(tokens / 1e6 * online, 2),
        "usd_batch": round(tokens / 1e6 * batch, 2),
        "minutes_online": round(texts / ONLINE_TEXTS_PER_MINUTE),
        "index_mb": round(texts * int(space["dim"]) * np.dtype(emb.STORE_DTYPE).itemsize / 1e6),
    }


def _new_state(space: dict, mode: str) -> dict:
    return {"space": space, "mode": mode, "started_at": _now(), "parts": [], "jobs": []}


def rebuild(
    conn: sqlite3.Connection,
    *,
    online: bool = False,
    token_budget: int | None = None,
    wait: bool = True,
    poll_seconds: float = POLL_SECONDS,
    client=None,
) -> dict:
    """Build the index again in this code's space into the side directory, then swap it in.

    Returns a report: {"state": "swapped" | "pending" | "budget", the counts}. "pending":
    batch jobs are out and `wait` was False; "budget": `token_budget` (estimated tokens sent
    by this run) stopped the sending. Either resumes on the next run. Raises RebuildRunning
    when another rebuild holds the lock, RebuildRefused when the side directory holds the
    build of another space.
    """
    space = emb.embedding_space()
    report: dict[str, Any] = {
        "state": None,
        "space": space,
        "tokens_sent": 0,
        "tokens_billed": 0,
        "embedded_online": 0,
        "jobs_sent": 0,
        "jobs_collected": 0,
    }
    with emb.hold_rebuild_lock():
        state = _load_state()
        if state is None:
            side_dir().mkdir(parents=True, exist_ok=True)
            state = _new_state(space, "batch")
        elif state["space"] != space:
            raise RebuildRefused(
                f"{side_dir()} holds a build in {state['space']}, not this code's {space}; "
                "remove it with `brain maint vectors-rebuild --abandon` to start over"
            )
        state["mode"] = "online" if online or embed_backend() == "vertex" else "batch"
        _save_state(state)
        client = client or emb._get_client()
        wanted = _wanted(conn, space)
        sent = Sender(client, state, space, report)
        done = _part_ids(state) | {v for job in state["jobs"] for v in _job_ids(job)}
        stream = wanted.stream(conn, done)
        exhausted = False
        late: list[int] = []
        idle_refusals = 0
        # The rolling window: send until the stream ends or the service's queue is full,
        # collect what finished, wait, and send again.
        while True:
            try:
                _resubmit(client, state, space, wanted, conn, report)
                sent.flush()  # a job the full queue refused last round
                while not exhausted:
                    item = next(stream, None)
                    if item is None:
                        exhausted = True
                        sent.flush()
                    elif not sent.add(*item, token_budget):
                        report["state"] = "budget"
                        return report
            except BatchQueueFull:
                emb._log("vector rebuild: the batch queue is full; sending more once jobs finish")
                idle_refusals = 0 if state["jobs"] else idle_refusals + 1
                if idle_refusals >= IDLE_REFUSALS:
                    raise RebuildRefused(
                        f"the batch queue took no job in {IDLE_REFUSALS} rounds with none of "
                        "this build's queued: another job of the key holds it, or the key's "
                        "tier takes less than one text; wait, or run with --online"
                    ) from None
            late += _collect(client, state, space, report)
            if exhausted and not sent.pairs and not state["jobs"] and not state.get("retry"):
                break
            if not wait:
                report["state"] = "pending"
                report["jobs_pending"] = len(state["jobs"])
                return report
            time.sleep(poll_seconds)
        if late:
            emb._log(f"vector rebuild: {len(late)} texts the batch jobs missed go online")

        _finish(conn, client, state, space, report)
        report["state"] = "swapped"
        return report


def _resubmit(client, state: dict, space: dict, wanted: "Wanted", conn, report: dict) -> None:
    """Send again, each as a job of its own, the jobs that failed; one the full queue refuses
    stays to be sent next round."""
    while state.get("retry"):
        retry = state["retry"][0]
        again = set(retry["ids"])
        pairs = list(wanted.stream(conn, wanted.ids() - again))
        if pairs:
            _submit(client, state, space, pairs, attempts=retry["attempts"] + 1)
            report["jobs_sent"] += 1
        state["retry"].pop(0)
        _save_state(state)


class Sender:
    """Collects texts into parts (online) or jobs (batch) and sends each as it fills, counting
    the estimated tokens this run sent against its budget."""

    def __init__(self, client, state: dict, space: dict, report: dict):
        self.client, self.state, self.space, self.report = client, state, space, report
        self.size = BATCH_JOB_TEXTS if state["mode"] == "batch" else ONLINE_PART_TEXTS
        self.job_tokens = state.get("job_tokens", BATCH_JOB_TOKENS)
        self.pairs: list[tuple[int, str]] = []
        self.pending_tokens = 0

    def add(self, vid: int, text: str, budget: int | None) -> bool:
        """Queue one text; False, sending what is queued, when it would pass the budget. A
        batch job closes at BATCH_JOB_TEXTS texts or its token size. Raises BatchQueueFull,
        the text kept queued, when the service will take no more jobs now."""
        cost = _tokens(vid, text)
        if budget is not None and self.report["tokens_sent"] + self.pending_tokens + cost > budget:
            self.flush()
            return False
        self.pairs.append((vid, text))
        self.pending_tokens += cost
        full = self.state["mode"] == "batch" and self.pending_tokens >= self.job_tokens
        if len(self.pairs) >= self.size or full:
            self.flush()
        return True

    def flush(self) -> None:
        """Send what is queued: online as one part, as batch jobs of at most the job size.
        On BatchQueueFull what is not sent stays queued, to go next round. A refusal with
        none of this build's jobs queued means one job is more than the key's whole queue
        (tier 1 takes 500,000 tokens): the job size halves, down to 50,000, and it tries
        again."""
        if self.state["mode"] != "batch":
            if self.pairs:
                vectors = _embed_online(self.client, self.space, self.pairs)
                _write_part(self.state, [v for v, _t in self.pairs], vectors, _now())
                self.report["embedded_online"] += len(self.pairs)
                self.report["tokens_sent"] += self.pending_tokens
            self.pairs, self.pending_tokens = [], 0
            return
        while self.pairs:
            take: list[tuple[int, str]] = []
            tokens = 0
            for vid, text in self.pairs:
                cost = _tokens(vid, text)
                if take and tokens + cost > self.job_tokens:
                    break
                take.append((vid, text))
                tokens += cost
            try:
                _submit(self.client, self.state, self.space, take)
            except BatchQueueFull:
                if self.state["jobs"] or self.job_tokens <= BATCH_JOB_MIN_TOKENS:
                    raise
                self.job_tokens //= 2
                self.state["job_tokens"] = self.job_tokens
                emb._log(f"vector rebuild: batch jobs now {self.job_tokens:,} tokens at most")
                continue
            self.report["jobs_sent"] += 1
            self.report["tokens_sent"] += tokens
            self.pairs = self.pairs[len(take) :]
            self.pending_tokens -= tokens


def _finish(conn: sqlite3.Connection, client, state: dict, space: dict, report: dict) -> None:
    """Catch up with the store, assemble the parts, swap the index in, stamp the threads."""
    wanted = _wanted(conn, space)
    want = wanted.ids()
    have = _part_ids(state)
    missing = list(wanted.stream(conn, have))
    for start in range(0, len(missing), ONLINE_PART_TEXTS):
        part = missing[start : start + ONLINE_PART_TEXTS]
        _write_part(state, [v for v, _t in part], _embed_online(client, space, part), _now())
        report["embedded_online"] += len(part)
        report["tokens_sent"] += sum(_tokens(v, t) for v, t in part)
    ids: list[int] = []
    blocks: list[np.ndarray] = []
    read_at: dict[int, str] = {}
    for name in state["parts"]:
        with np.load(side_dir() / name, allow_pickle=False) as data:
            keep = np.isin(data["ids"], np.fromiter(want, dtype=np.int64))
            part_ids = data["ids"][keep].tolist()
            ids += part_ids
            blocks.append(data["vectors"][keep])
            stamp = str(data["read_at"][()])
            read_at.update((vid, stamp) for vid in part_ids if _kind(vid) in ("teams", "whatsapp"))
    # A text embedded twice (a retried job, a part written before a crash was recorded)
    # keeps its last vector.
    stacked = np.concatenate(blocks).astype(emb.STORE_DTYPE, copy=False)
    del blocks
    order = {vid: i for i, vid in enumerate(ids)}
    if len(order) < len(ids):
        unique = sorted(order.values())
        stacked = stacked[unique]
        ids = [ids[i] for i in unique]
    vectors = emb._normalise(stacked)
    if len(ids) != len(want):
        raise RuntimeError(f"vector rebuild holds {len(ids)} of {len(want)} vectors; not swapped")
    emb.install_index(conn, ids, vectors, space)
    _stamp_threads(conn, read_at)
    report["vectors"] = {
        kind: sum(1 for vid in ids if _kind(vid) == kind)
        for kind in ("email", "attachment", "chunk", "conversation", "teams", "whatsapp")
    }
    shutil.rmtree(side_dir(), ignore_errors=True)


def _stamp_threads(conn: sqlite3.Connection, read_at: dict[int, str]) -> None:
    """Mark the Teams threads and WhatsApp sessions the rebuild embedded as embedded when their
    text was read, unless extracted again since, so their syncs take only what changed."""
    for kind, table, offset in (
        ("teams", "teams_threads", emb.TEAMS_THREAD_ID_OFFSET),
        ("whatsapp", "whatsapp_threads", emb.WHATSAPP_THREAD_ID_OFFSET),
    ):
        stamps = {offset - vid: at for vid, at in read_at.items() if _kind(vid) == kind}
        if not stamps:
            continue
        try:
            rows = conn.execute(
                f"SELECT id, extracted_at FROM {table} WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(list(stamps)),),
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for tid, extracted_at in rows:
            at = stamps[tid]
            extracted, read = instant(extracted_at), instant(at)
            if extracted is None or read is None or extracted <= read:
                conn.execute(f"UPDATE {table} SET embedding_at = ? WHERE id = ?", (at, tid))
    conn.commit()


def abandon() -> bool:
    """Remove the side directory and what it holds; True when there was one. The batch jobs it
    sent are left to the service, which deletes their files after a day."""
    path = side_dir()
    if not path.exists():
        return False
    with emb.hold_rebuild_lock():
        shutil.rmtree(path)
    return True


def status() -> dict | None:
    """The side build's state: its space, mode, start, parts and pending jobs; None for none."""
    state = _load_state()
    if state is None:
        return None
    return {
        "space": state["space"],
        "mode": state["mode"],
        "started_at": state["started_at"],
        "parts": len(state["parts"]),
        "vectors": len(_part_ids(state)),
        "jobs_pending": len(state["jobs"]),
        "running": emb.rebuild_running(),
    }


# --- brain maint ----------------------------------------------------------------------------


def _print_estimate(found: dict) -> None:
    space = found["space"]
    print(
        f"Rebuild into {space['model']}, {space['dim']} dimensions, template {space['template']}:"
    )
    for kind, row in found["by_kind"].items():
        docs = f" from {row['documents']:,} documents" if "documents" in row else ""
        print(
            f"  {kind:<12} {row['texts']:>9,} texts{docs}, {row['chars']:>13,} characters, "
            f"about {row['tokens']:>12,} tokens"
        )
    print(
        f"  total        {found['texts']:>9,} texts, about {found['tokens']:,} tokens: "
        f"${found['usd_batch']:,.2f} as batch jobs, ${found['usd_online']:,.2f} online "
        f"(about {found['minutes_online']:,} minutes); the index about {found['index_mb']:,} MB"
    )
    print(f"  chunks of about {CHUNK_CHARS:,} characters; tokens at measured characters per token")


def cmd_vectors_rebuild(args) -> int:
    """--estimate and --status only read, so they run on a replica too (the CLI lets this
    command through there); everything else refuses on one."""
    from src import config
    from src.store.schema import get_connection
    from src.store.sql_readonly import connect_read_only

    if args.status:
        found = status()
        print(json.dumps(found, indent=1) if found else "No side build.")
        return 0
    if args.estimate:
        conn = connect_read_only(args.db, immutable=True)
        try:
            _print_estimate(estimate(conn))
        finally:
            conn.close()
        return 0
    if config.is_replica():
        print(config.replica_refusal("'maint vectors-rebuild'"), file=sys.stderr)
        return 2
    if args.abandon:
        print("Side build removed." if abandon() else "No side build to remove.")
        return 0
    conn = get_connection(str(args.db))
    try:
        try:
            report = rebuild(
                conn,
                online=args.online,
                token_budget=args.token_budget,
                wait=not args.no_wait,
                poll_seconds=args.poll_seconds,
            )
        except emb.RebuildRunning as e:
            print(f"skip: {e}")
            return 75
    finally:
        conn.close()
    print(json.dumps(report, indent=1, default=str))
    if report["state"] == "swapped":
        print(f"Swapped in; the index it replaced is {emb.backup_path()}")
        return 0
    if report["state"] == "pending":
        print("Batch jobs are out: run the same command again to collect them and swap.")
    else:
        print("The token budget stopped this run: run it again to carry on.")
    return 75


def cmd_vectors_slice(args) -> int:
    from src.store.schema import get_connection

    conn = get_connection(str(args.db))
    try:
        with emb.hold_rebuild_lock():
            space = emb.slice_index(conn, args.dim)
    finally:
        conn.close()
    print(f"Sliced to {space}; the index it replaced is {emb.backup_path()}")
    return 0


def add_maint_parsers(maint_subparsers) -> None:
    """`brain maint vectors-rebuild` and `brain maint vectors-slice`."""
    rebuild_parser = maint_subparsers.add_parser(
        "vectors-rebuild",
        help="Embed every summary and long-document chunk again in this code's space, then swap",
    )
    rebuild_parser.add_argument(
        "--estimate", action="store_true", help="Print texts, tokens and dollars; send nothing"
    )
    rebuild_parser.add_argument(
        "--token-budget",
        type=int,
        default=None,
        help="Stop sending once this run's texts reach this many tokens (estimated)",
    )
    rebuild_parser.add_argument(
        "--online", action="store_true", help="Embed online instead of as Gemini batch jobs"
    )
    rebuild_parser.add_argument(
        "--no-wait", action="store_true", help="Send the batch jobs and exit; run again to collect"
    )
    rebuild_parser.add_argument(
        "--poll-seconds", type=float, default=POLL_SECONDS, help="Between batch job checks"
    )
    rebuild_parser.add_argument("--status", action="store_true", help="Show the side build")
    rebuild_parser.add_argument(
        "--abandon", action="store_true", help="Remove the side build and start over next run"
    )
    rebuild_parser.set_defaults(func=cmd_vectors_rebuild)
    slice_parser = maint_subparsers.add_parser(
        "vectors-slice",
        help="Cut the index's vectors to fewer dimensions at no cost (default 768)",
    )
    slice_parser.add_argument("--dim", type=int, default=emb.EMBEDDING_DIM)
    slice_parser.set_defaults(func=cmd_vectors_slice)
