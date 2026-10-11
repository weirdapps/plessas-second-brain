"""What model calls cost: one rate table, a run's token budget, and the usage log read back.

src/extract/claude_extract.py writes one line per completed call to
`llm-usage-YYYY-MM.jsonl` under the data root, and for its first week nothing read it: on
2026-10-04 one day of attachment summaries cost about $2,680 and no alarm fired (audit
2026-10-11). The dry-run estimates (`process-attachments --estimate`, `reextract --estimate`) and
the health check's spend row price calls from the table below. A cost is derived from the tokens
when it is read and never stored, so a corrected rate reprices the whole log.
"""

import json
import math
import os
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Rates:
    """USD per million tokens."""

    input: float
    output: float
    batch_input: float
    batch_output: float
    cache_read: float
    # A five-minute cache write.
    cache_write: float
    # A one-hour cache write, which call shape v2 asks for (src/extract/call_shape.py).
    cache_write_1h: float
    # The three cache rates in a batch job (src/extract/vertex_batch.py).
    batch_cache_read: float
    batch_cache_write: float
    batch_cache_write_1h: float


# Vertex AI, `eu` multi-region (10% over the global endpoint), from Google's Vertex AI pricing
# page as read on 2026-10-11. Keyed by the model id a response names. Sonnet 5.5 is the one model
# the deployment's allowlist accepts; a model missing here is reported unpriced, never guessed.
RATES: dict[str, Rates] = {
    "claude-sonnet-5-5": Rates(
        input=2.20,
        output=11.00,
        batch_input=1.10,
        batch_output=5.50,
        cache_read=0.11,
        cache_write=2.75,
        cache_write_1h=4.40,
        batch_cache_read=0.055,
        batch_cache_write=1.375,
        batch_cache_write_1h=2.20,
    ),
}
# The rates a dry-run estimate is priced at.
ESTIMATE_MODEL = "claude-sonnet-5-5"

# Characters per input token on this corpus: Greek prose and spreadsheet cells tokenize densely.
# Attachment part prompts in the usage log ran 1.0 to 2.0 characters a token (audit 2026-10-11).
DEFAULT_CHARS_PER_TOKEN = 1.6
# Output tokens assumed for a call before it is answered: attachment calls in the usage log
# averaged 1,420, thinking included.
OUTPUT_TOKENS_PER_CALL = 1_500


def rates_for(model: str | None) -> Rates | None:
    """The rates of a model id, read past a Vertex `@version` and a `[1m]` suffix; None when the
    table has no rate for it."""
    if not model:
        return None
    base = model.split("@", 1)[0].removesuffix("[1m]")
    return RATES.get(base)


def cost_usd(
    rates: Rates,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    batch: bool = False,
) -> float:
    """The cost of these tokens at these rates, online or as a batch job.
    `cache_write_tokens` are five-minute writes, `cache_write_1h_tokens` one-hour ones."""
    if batch:
        i, o = rates.batch_input, rates.batch_output
        read, write, write_1h = (
            rates.batch_cache_read,
            rates.batch_cache_write,
            rates.batch_cache_write_1h,
        )
    else:
        i, o = rates.input, rates.output
        read, write, write_1h = rates.cache_read, rates.cache_write, rates.cache_write_1h
    return (
        input_tokens * i
        + output_tokens * o
        + cache_read_tokens * read
        + cache_write_tokens * write
        + cache_write_1h_tokens * write_1h
    ) / 1_000_000


def configured_chars_per_token() -> float:
    """BRAIN_CHARS_PER_TOKEN, or DEFAULT_CHARS_PER_TOKEN when it is unset or not a positive
    number. Read at each call, so a run picks up the environment it was started with."""
    raw = os.environ.get("BRAIN_CHARS_PER_TOKEN", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CHARS_PER_TOKEN
    return value if math.isfinite(value) and value > 0 else DEFAULT_CHARS_PER_TOKEN


class OverBudget(Exception):
    """The run's token budget refused a call."""


class TokenBudget:
    """A run's allowance of model tokens, input and output together.

    Each call is estimated before it is sent, its prompt's characters at `chars_per_token` plus
    `output_tokens` for the answer, and refused with OverBudget when the tokens spent and held
    for calls in flight, plus that estimate, would pass the limit. The first refusal ends the
    run: `exhausted` stays set, so a smaller item further down the queue does not slip in. A
    finished call is charged what its response reports (input, output and cache tokens), or its
    estimate when the response reports nothing; a call that raised is charged nothing. Calls
    already in flight finish, so a run can end over the limit by the error of their estimates.
    Thread-safe: Phase 2's workers share one.
    """

    def __init__(
        self,
        limit: int,
        *,
        chars_per_token: float | None = None,
        output_tokens: int = OUTPUT_TOKENS_PER_CALL,
    ):
        self.limit = limit
        self.chars_per_token = chars_per_token or configured_chars_per_token()
        self.output_tokens = output_tokens
        self.spent = 0
        self.calls = 0
        self.exhausted = False
        self._held = 0
        self._lock = threading.Lock()

    def estimate(self, prompt_chars: int) -> int:
        return math.ceil(prompt_chars / self.chars_per_token) + self.output_tokens

    def reserve(self, prompt_chars: int) -> int:
        """Hold room for a call of this prompt size, and return what was held."""
        need = self.estimate(prompt_chars)
        with self._lock:
            if self.exhausted or self.spent + self._held + need > self.limit:
                self.exhausted = True
                raise OverBudget(
                    f"token budget: {self.spent:,} spent and {self._held:,} in flight of"
                    f" {self.limit:,}; the next call needs about {need:,}"
                )
            self._held += need
        return need

    def settle(self, held: int, usage: Any = None) -> None:
        """Charge a finished call: what `usage` reports, or `held` when it reports nothing."""
        counts = [
            getattr(usage, name, None)
            for name in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        ]
        reported = [n for n in counts if isinstance(n, int)]
        with self._lock:
            self._held -= held
            self.spent += sum(reported) if reported else held
            self.calls += 1

    def release(self, held: int) -> None:
        """A call that raised: its room is free again and nothing is charged."""
        with self._lock:
            self._held -= held


# --- the usage log, read back --------------------------------------------------


def usage_log_path(log_dir: Path, day: date) -> Path:
    """The monthly file that holds a UTC day's calls: claude_extract._log_usage names it so."""
    return Path(log_dir) / f"llm-usage-{day:%Y-%m}.jsonl"


def _tokens(record: dict, key: str) -> int:
    value = record.get(key)
    return value if isinstance(value, int) else 0


def usage_for_day(log_dir: Path, day: date) -> dict | None:
    """Calls, tokens and cost by call site for one UTC day of the usage log. None when that
    month has no log. A line that does not parse is skipped; a model with no rate is counted
    in `unpriced_calls` and adds no cost.

    `cache_write_tokens` counts every cache write and `cache_write_1h_tokens` the one-hour
    ones among them, priced at their own rate; a line from before the split was logged
    prices its writes as five-minute ones. `thinking_tokens` is the share of the output
    tokens a line says was thinking. A line that names a `batch_job` was answered in a
    Vertex AI batch job, and is priced at batch rates.

    Only lines that mention the day are parsed, so a month of backfill (hundreds of
    thousands of lines) is read in about a second."""
    path = usage_log_path(log_dir, day)
    if not path.exists():
        return None
    stamp = day.isoformat()
    sites: dict[str, dict] = {}
    totals = dict.fromkeys(
        (
            "calls",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cache_write_1h_tokens",
            "thinking_tokens",
        ),
        0,
    )
    cost = 0.0
    unpriced = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if stamp not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or not str(record.get("ts", "")).startswith(stamp):
                continue
            used = {
                "input_tokens": _tokens(record, "input_tokens"),
                "output_tokens": _tokens(record, "output_tokens"),
                "cache_read_tokens": _tokens(record, "cache_read_input_tokens"),
                "cache_write_tokens": _tokens(record, "cache_creation_input_tokens"),
                "cache_write_1h_tokens": _tokens(record, "cache_write_1h_tokens"),
                "thinking_tokens": _tokens(record, "thinking_tokens"),
            }
            rates = rates_for(record.get("model"))
            call_cost = 0.0
            if rates is None:
                unpriced += 1
            else:
                one_hour = min(used["cache_write_1h_tokens"], used["cache_write_tokens"])
                call_cost = cost_usd(
                    rates,
                    input_tokens=used["input_tokens"],
                    output_tokens=used["output_tokens"],
                    cache_read_tokens=used["cache_read_tokens"],
                    cache_write_tokens=used["cache_write_tokens"] - one_hour,
                    cache_write_1h_tokens=one_hour,
                    batch=bool(record.get("batch_job")),
                )
            site = sites.setdefault(
                str(record.get("site")),
                {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
            )
            site["calls"] += 1
            site["input_tokens"] += used["input_tokens"]
            site["output_tokens"] += used["output_tokens"]
            site["cost_usd"] += call_cost
            totals["calls"] += 1
            for key, value in used.items():
                totals[key] += value
            cost += call_cost
    return {**totals, "cost_usd": cost, "unpriced_calls": unpriced, "sites": sites}


def newest_usage_at(log_dir: Path, now: datetime) -> datetime | None:
    """When the newest logged call was made, from the tail of this month's log, or last
    month's early in a month. None when neither holds a readable line."""
    today = now.astimezone(UTC).date()
    for day in (today, today.replace(day=1) - timedelta(days=1)):
        path = usage_log_path(log_dir, day)
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 8192))
                tail = f.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        for line in reversed(tail.splitlines()):
            try:
                ts = datetime.fromisoformat(json.loads(line)["ts"])
            except (ValueError, KeyError, TypeError):
                continue
            return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    return None
