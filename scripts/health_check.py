#!/usr/bin/env python3
"""
Second Brain nightly health check.

Queries all data sources, detects staleness and failures, attempts auto-fixes,
and emails a status report. Designed to run via launchd at 23:55 daily.

Usage:
    python3 scripts/health_check.py              # print to stdout
    python3 scripts/health_check.py --email      # also send via outlook-cli
    python3 scripts/health_check.py --fix        # auto-fix issues then report
    python3 scripts/health_check.py --email --fix # production mode
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import DEFAULT_DB, NEWS_DB_PATH, SHAREPOINT_HOST  # noqa: E402

DB_PATH = DEFAULT_DB
LOG_DIR = Path.home() / ".second-brain/logs"
STATE_DIR = Path.home() / ".second-brain"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
SHAREPOINT_SESSION = Path.home() / ".sharepoint-cli" / "session.json"

# Reverse-ingest input side: the roots cmd_reverse_ingest scans and the
# extensions it ingests. Kept in sync with src/cli.py:cmd_reverse_ingest.
DOCUMENT_ROOTS = [
    Path("~/Documents/National").expanduser(),
    Path("~/Documents/Personal").expanduser(),
]
INGESTABLE_EXTENSIONS = {".pdf", ".pptx", ".xlsx", ".docx", ".md", ".txt"}

# Heartbeat written by the laptop-side push job after every successful push
# (one ISO-8601 UTC line). Authoritative liveness signal for the document roots:
# our own curate-docs job writes files into those same roots, so a fresh mtime
# can be self-manufactured while the organic source is frozen.
DOCUMENT_SYNC_STAMP = STATE_DIR / "document-sync.stamp"

# Counterpart to the heartbeat: written by the push job when a run FAILS (line 1
# ISO-8601 UTC, line 2 the reason), removed when one succeeds. The stamp alone
# records only successes and is judged against a 14-day window, so a job failing
# on every run emits no signal until that window expires.
DOCUMENT_SYNC_FAIL = STATE_DIR / "document-sync.fail"

# Upstream news database — same path `brain news-sync` reads (honours BRAIN_NEWS_DB).
NEWS_DB = NEWS_DB_PATH

# launchd label prefix (macOS); override with BRAIN_LABEL_PREFIX. systemd units
# (VPS) use the sb-* names in SYSTEMD_UNITS below and are unaffected by this.
DEFAULT_LABEL_PREFIX = "com.secondbrain"

# Suffixes used to read the prefix off installed plists. Only unambiguous ones:
# ".teams-sync" and ".calendar-sync" also end in "-sync", so keying on that
# suffix would truncate the prefix to "<prefix>.teams".
_DISCOVERY_JOB_SUFFIXES = (
    ".noon-catchup",
    ".attachments",
    ".auth-watch",
    ".curate-docs",
    ".reverse-ingest",
)


def detect_label_prefix(agents_dir: Path = LAUNCH_AGENTS_DIR) -> str:
    """The launchd label prefix this machine actually uses.

    BRAIN_LABEL_PREFIX wins when set; otherwise the prefix is read off the
    installed plists. A hardcoded default cannot be right for both worlds: this
    script ships to a public repo, so the shipped literal is generic and matched
    nothing on the host that actually runs it — _is_migrated() then looked for
    filenames that do not exist and reported all 9 migrated jobs as NOT_LOADED.
    """
    env = os.environ.get("BRAIN_LABEL_PREFIX")
    if env:
        return env
    counts: Counter[str] = Counter()
    for suffix in _DISCOVERY_JOB_SUFFIXES:
        for path in agents_dir.glob(f"*{suffix}.plist*"):
            prefix = path.name.split(f"{suffix}.plist", 1)[0]
            if prefix:
                counts[prefix] += 1
    if not counts:
        return DEFAULT_LABEL_PREFIX
    return counts.most_common(1)[0][0]


LABEL_PREFIX = detect_label_prefix()

STALE_THRESHOLDS = {
    "emails": timedelta(hours=6),
    "teams": timedelta(hours=24),
    # How long the Teams sync may go without polling a single chat. The timer
    # runs at :30 from 07:00 to 22:00 plus 01:00, so the widest legitimate gap
    # is the 01:30 -> 07:30 overnight window; eight hours leaves two of slack.
    # This is the signal that means "we stopped listening" — see check_teams,
    # where message recency only means "nobody spoke".
    "teams_pull": timedelta(hours=8),
    "calendar": timedelta(days=2),
    # embeddings.npz is rewritten by every sync that loads new rows. Two days is
    # a couple of missed daily runs, not a quiet afternoon.
    "embeddings": timedelta(days=2),
    # SharePoint fetches ride the daily sync. Applied to links still inside
    # their retry budget, never to ones resting past MAX_SHAREPOINT_ATTEMPTS.
    "sharepoint": timedelta(days=2),
    "conversations": timedelta(days=2),
    "daily_sync": timedelta(hours=26),
    "outlook_sync": timedelta(hours=3),
    "attachments_llm": timedelta(days=3),
    "document_roots": timedelta(days=14),
    "news": timedelta(hours=12),
    # Vision runs inside the daily attachment job, so three days is three missed
    # runs. Measured against an image's own arrival, not against wall-clock
    # MAX(visioned_at): a quiet stretch with no eligible images must stay silent.
    "images_vision": timedelta(days=3),
}

# Classes the classifier filters out before vision by design — they stay
# undescribed forever and are not a backlog. Everything else, including a class
# added after this was written, counts as work the vision stage still owes.
VISION_SKIPPED_CLASSES = ("signature", "noise")

# Share of Teams chats sitting at ingest_disabled=1 that means ingestion has
# quietly shrunk rather than skipped a few genuinely unreadable rooms. Prod hit
# 97% (1,179 of 1,219) from a single Graph-wide 403 and stayed green for 33h.
TEAMS_DISABLED_WARN_SHARE = 0.25

# Inline-image queue depth that means the pipeline is no longer keeping up.
# Step 8 of the sync is time-boxed (BRAIN_IMAGE_CLASSIFY_BUDGET_S, default 8min)
# and drains ~50 images per run across two full syncs a day. A heavy news day can
# deposit more than that, so a few hundred pending is a spike that clears on the
# next quiet day; a sustained queue past this line is not draining and wants a
# manual `brain process-images` pass or more budget.
IMAGE_QUEUE_WARN = 500

# Mirrors MAX_SHAREPOINT_ATTEMPTS in src/export/sharepoint_fetcher.py, which is
# the source of truth — this script is loaded standalone (no package import), so
# the value is duplicated here and pinned by a test that reads the real one.
# Links at or past the cap are resting out their cool-off, not being neglected.
SHAREPOINT_MAX_ATTEMPTS = 5

LAUNCHD_JOBS = {
    f"{LABEL_PREFIX}.sync": "Hourly Outlook sync",
    f"{LABEL_PREFIX}-sync": "Daily full sync",
    f"{LABEL_PREFIX}.noon-catchup": "Noon catchup",
    f"{LABEL_PREFIX}.teams-sync": "Teams sync",
    f"{LABEL_PREFIX}.calendar-sync": "Calendar sync",
    f"{LABEL_PREFIX}.attachments": "Attachment processing",
    f"{LABEL_PREFIX}.auth-watch": "Auth watcher",
    f"{LABEL_PREFIX}.curate-docs": "Document curation",
    f"{LABEL_PREFIX}.reverse-ingest": "Reverse ingest",
}

# Linux/systemd counterpart. The VPS runs the same repo under `systemctl --user`;
# the same logical jobs have different identifiers there (and Linux has no
# launchctl at all, which is why the launchd path errors out on the VPS).
SYSTEMD_UNITS = {
    "sb-outlook-sync.service": "Hourly Outlook sync",
    "sb-daily-sync.service": "Daily full sync",
    "sb-noon-catchup.service": "Noon catchup",
    "sb-teams-sync.service": "Teams sync",
    "sb-calendar-sync.service": "Calendar sync",
    "sb-attachments.service": "Attachment processing",
    "sb-auth-watch.service": "Auth watcher",
    "sb-curate-docs.service": "Document curation",
    "sb-reverse-ingest.service": "Reverse ingest",
    # Registered late. The VPS runs twelve sb-* units but only the nine above
    # were listed, so these three had neither a job status nor a log-age signal
    # — the same hand-listed-subset gap that hid http-error from the SharePoint
    # tally. This dict is the source both check_jobs and check_sync_logs read,
    # so anything absent from it is a job nobody is watching.
    "sb-news-sync.service": "News sync",
    "sb-conversation-sync.service": "Conversation sync",
    "sb-health-check.service": "Health check",
}

IS_MACOS = sys.platform == "darwin"

# Platform-native identifiers for the jobs auto_fix needs to (re)start.
AUTH_WATCH_JOB = f"{LABEL_PREFIX}.auth-watch" if IS_MACOS else "sb-auth-watch.service"
LOADER_JOB = f"{LABEL_PREFIX}-sync" if IS_MACOS else "sb-daily-sync.service"


def launchctl_bin() -> str:
    """Absolute path to launchctl.

    launchd-spawned jobs — and any invocation outside sb-health-check.sh — can
    run with a minimal PATH that omits /bin, making a bare "launchctl" raise
    FileNotFoundError. That silently turns every job into ERROR and disables
    auto_fix(). Resolving the absolute path keeps both working regardless of PATH.
    """
    for candidate in ("/bin/launchctl", "/usr/bin/launchctl"):
        if os.path.exists(candidate):
            return candidate
    return shutil.which("launchctl") or "launchctl"


def systemctl_bin() -> str:
    """Absolute path to systemctl (Linux/VPS), PATH-independent like launchctl_bin."""
    for candidate in ("/usr/bin/systemctl", "/bin/systemctl"):
        if os.path.exists(candidate):
            return candidate
    return shutil.which("systemctl") or "systemctl"


def kick_job(label: str):
    """(Re)start a scheduled job by its platform-native identifier:
    `launchctl kickstart` on macOS, `systemctl --user start` on Linux (VPS)."""
    if IS_MACOS:
        uid = os.getuid()
        return subprocess.run(
            [launchctl_bin(), "kickstart", f"gui/{uid}/{label}"],
            capture_output=True,
            timeout=30,
        )
    return subprocess.run(
        [systemctl_bin(), "--user", "start", label],
        capture_output=True,
        timeout=30,
    )


def get_db():
    if not DB_PATH.exists():
        return None
    return sqlite3.connect(str(DB_PATH), timeout=10)


def _age(ts):
    """Age of a timestamp, handling both naive-local values (macOS sqlite) and
    tz-aware UTC ones (the VPS stores Graph 'Z' timestamps). Without this, a UTC
    'Z' value is compared against local now() — inflating every age by the UTC
    offset (EEST = +3h) and tripping false STALE alerts on the VPS."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is not None:
        return datetime.now(UTC) - dt
    return datetime.now() - dt


def _utc(ts):
    """Parse a stored timestamp to an aware UTC datetime (naive values are local,
    matching _age). Returns None for missing/unparseable values. Used by the
    source-side checks, which compare two timestamps against an injectable now."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None
    return dt.astimezone(UTC)


def check_emails(db):
    # `message_id > 0` excludes reverse-ingest documents, which carry NEGATIVE
    # integer ids. It does NOT exclude the news source: SQLite ranks TEXT above
    # INTEGER, so 'news:article:…' > 0 is true. Without the mailbox filter one
    # fresh news row would mask stale mail and permanently silence this alarm
    # (and the loader auto-fix keyed off it). Filtering on typeof() is not an
    # option — 13k genuine mail rows carry text ids alongside 49k integer ones.
    real_mail = "message_id > 0 AND (mailbox_name IS NULL OR mailbox_name <> 'News')"
    r = db.execute(f"SELECT COUNT(*), MAX(date_received) FROM emails WHERE {real_mail}").fetchone()
    total, latest = r[0], r[1]
    # datetime() around the column, not the bare column: stored values carry the
    # ISO 'T' separator while datetime('now', ...) renders a SPACE, and lexical
    # comparison puts 'T' (0x54) above ' ' (0x20). Every row sharing the cutoff's
    # DATE therefore passed regardless of its time — prod read 108 arrivals
    # against 35 real ones, and advertised "30 today" for a Teams source that had
    # been silent for 28 hours. datetime() also normalises 'Z', '+00:00' and the
    # 7-fractional-digit form Graph emits.
    recent_24h = db.execute(
        f"SELECT COUNT(*) FROM emails WHERE datetime(date_received) > "
        f"datetime('now', '-1 day') AND {real_mail}"
    ).fetchone()[0]
    recent_7d = db.execute(
        f"SELECT COUNT(*) FROM emails WHERE datetime(date_received) > "
        f"datetime('now', '-7 days') AND {real_mail}"
    ).fetchone()[0]

    age = _age(latest)

    stale = age and age > STALE_THRESHOLDS["emails"]
    return {
        "name": "Emails",
        "total": total,
        "latest": latest,
        "recent_24h": recent_24h,
        "recent_7d": recent_7d,
        "age": age,
        "stale": stale,
        "status": "STALE" if stale else "OK",
    }


def check_teams(db):
    total = db.execute("SELECT COUNT(*) FROM teams_messages").fetchone()[0]
    latest = db.execute("SELECT MAX(composed_at) FROM teams_messages").fetchone()[0]
    chats = db.execute("SELECT COUNT(*) FROM teams_chats").fetchone()[0]
    # `ingest_disabled` is one-way — set on a permanent-looking error, cleared
    # only by hand. A Graph-wide 403 therefore dropped 1,179 of 1,219 chats in a
    # single sweep, and message recency alone stayed green on what the 40
    # survivors still produced. Coverage has to be its own signal.
    disabled = db.execute("SELECT COUNT(*) FROM teams_chats WHERE ingest_disabled = 1").fetchone()[
        0
    ]
    threads = db.execute("SELECT COUNT(*) FROM teams_threads").fetchone()[0]
    # datetime() on both sides — see check_emails for why the bare column lied.
    recent_24h = db.execute(
        "SELECT COUNT(*) FROM teams_messages "
        "WHERE datetime(composed_at) > datetime('now', '-1 day')"
    ).fetchone()[0]

    age = _age(latest)

    # MAX(composed_at) measures whether COLLEAGUES were talking, which is not a
    # property of this system. Greek August and every weekend produce genuinely
    # silent stretches — two Saturdays in Aug 2026 recorded zero messages — so a
    # message-age alarm pages on a healthy pipeline until it gets ignored.
    # last_pulled_at is stamped by the sync on every poll whether or not the chat
    # said anything, so it separates "nobody spoke" from "we stopped listening".
    try:
        last_pull = db.execute("SELECT MAX(last_pulled_at) FROM teams_chats").fetchone()[0]
        pull_supported = True
    except sqlite3.OperationalError:
        last_pull, pull_supported = None, False
    pull_age = _age(last_pull)

    # When the corpus shrank (v19). The count says 1,179 chats are gone; the
    # date says whether they went in one instant — a Graph-wide outage — or a
    # few at a time as rooms were archived. Diagnosing the August sweep needed a
    # DB dig precisely because nothing recorded this. NULL on pre-v19 rows, and
    # on any DB predating the column: never let a missing one kill the report.
    try:
        disabled_at = db.execute(
            "SELECT MAX(ingest_disabled_at) FROM teams_chats WHERE ingest_disabled = 1"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        disabled_at = None

    if not pull_supported:
        # Pre-v18 schema has no heartbeat; message recency is all there is.
        stale = bool(age and age > STALE_THRESHOLDS["teams"])
    elif chats == 0:
        stale = False
    elif pull_age is None:
        stale = True  # chats on the books and not one has ever been polled
    else:
        stale = pull_age > STALE_THRESHOLDS["teams_pull"]
    # A handful of archived teams and guest-only channels are legitimately
    # unreadable, so only a large share means ingestion has quietly shrunk.
    coverage_lost = chats > 0 and disabled > chats * TEAMS_DISABLED_WARN_SHARE
    if stale:
        status = "STALE"
    elif coverage_lost:
        status = "WARN"
    else:
        status = "OK"
    return {
        "name": "Teams",
        "total": total,
        "chats": chats,
        "chats_disabled": disabled,
        "threads": threads,
        "latest": latest,
        "recent_24h": recent_24h,
        "age": age,
        "last_pull": last_pull,
        "pull_age": pull_age,
        "disabled_at": disabled_at,
        "stale": stale,
        "coverage_lost": coverage_lost,
        "status": status,
    }


def check_attachments(db):
    total = db.execute("SELECT COUNT(*) FROM attachment_content").fetchone()[0]
    extracted = db.execute(
        "SELECT COUNT(*) FROM attachment_content WHERE extraction_status = 'extracted'"
    ).fetchone()[0]
    failed = db.execute(
        "SELECT COUNT(*) FROM attachment_content WHERE extraction_status = 'failed'"
    ).fetchone()[0]
    llm_done = db.execute(
        "SELECT COUNT(*) FROM attachment_content WHERE llm_status = 'extracted'"
    ).fetchone()[0]
    # "pending" = actionable Phase-2 queue only: extracted text still awaiting an
    # LLM summary. Rows whose text extraction was skipped (no text) or failed
    # (corrupt/encrypted) also carry llm_status='pending' but can never be
    # summarized — counting them inflated the backlog to thousands of phantom items.
    llm_pending = db.execute(
        "SELECT COUNT(*) FROM attachment_content "
        "WHERE extraction_status = 'extracted' AND llm_status = 'pending'"
    ).fetchone()[0]
    llm_no_text = db.execute(
        "SELECT COUNT(*) FROM attachment_content "
        "WHERE extraction_status IN ('skipped', 'failed') AND llm_status = 'pending'"
    ).fetchone()[0]
    llm_failed = db.execute(
        "SELECT COUNT(*) FROM attachment_content WHERE llm_status = 'failed'"
    ).fetchone()[0]
    latest_llm = db.execute(
        "SELECT MAX(llm_extracted_at) FROM attachment_content WHERE llm_status = 'extracted'"
    ).fetchone()[0]

    age = _age(latest_llm)

    stale = age and age > STALE_THRESHOLDS["attachments_llm"]
    pct = (extracted * 100 / total) if total > 0 else 0
    return {
        "name": "Attachments",
        "total": total,
        "text_extracted": extracted,
        "text_failed": failed,
        "text_pct": pct,
        "llm_done": llm_done,
        "llm_pending": llm_pending,
        "llm_no_text": llm_no_text,
        "llm_failed": llm_failed,
        "latest_llm": latest_llm,
        "age": age,
        "stale": stale,
        "status": "STALE" if stale else ("WARN" if llm_failed > 200 else "OK"),
    }


def check_images(db):
    total = db.execute("SELECT COUNT(*) FROM inline_images").fetchone()[0]
    classified = db.execute(
        "SELECT COUNT(*) FROM inline_images WHERE vision_description IS NOT NULL"
    ).fetchone()[0]
    pct = (classified * 100 / total) if total > 0 else 0
    # Work still WAITING, using the same predicate run_backfill(unprocessed_only=True)
    # selects on. Coverage % alone hid a growing backlog: it stayed flat at 88% while
    # the queue sat 200 deep, because the denominator grows with the numerator.
    # The JOIN matters: run_backfill only sees attachments joinable to an email, so
    # counting orphans (email_id NULL) inflates this permanently — 91 reported vs 2
    # reachable on prod — and would trip the WARN for work that can never drain.
    pending = db.execute(
        "SELECT COUNT(*) FROM attachments a JOIN emails e ON a.email_id = e.id "
        "WHERE a.mime_type LIKE 'image/%' AND a.file_path IS NOT NULL "
        "AND a.message_id NOT IN (SELECT message_id FROM inline_image_occurrences)"
    ).fetchone()[0]
    # Vision-stage liveness. Neither signal above has a time dimension: coverage %
    # freezes numerator and denominator together, and `pending` only sees images
    # that never reached inline_images at all. So a vision stage that stopped
    # producing looked identical to one with nothing to do — prod ran 20 days at
    # "88% classified, 3 queued / OK" with MAX(visioned_at) stuck on 2026-07-27.
    # Ageing each image against its OWN arrival (not wall-clock) keeps a genuinely
    # quiet stretch silent while a stalled one is loud.
    placeholders = ", ".join("?" * len(VISION_SKIPPED_CLASSES))
    stuck = db.execute(
        "SELECT COUNT(*) FROM inline_images WHERE vision_description IS NULL "
        f"AND (classification IS NULL OR classification NOT IN ({placeholders})) "
        "AND datetime(classified_at) < datetime('now', ?)",
        (*VISION_SKIPPED_CLASSES, f"-{STALE_THRESHOLDS['images_vision'].days} days"),
    ).fetchone()[0]
    latest_vision = db.execute("SELECT MAX(visioned_at) FROM inline_images").fetchone()[0]
    return {
        "name": "Inline Images",
        "total": total,
        "classified": classified,
        "pct": pct,
        "pending": pending,
        "stuck": stuck,
        "latest_vision": latest_vision,
        # Keyed "age" because that is what build_report reads. Under its old name
        # the report printed "?" here, so the one source with a known outage was
        # also the one whose age was invisible.
        "age": _age(latest_vision),
        "status": "WARN" if pct < 50 or pending > IMAGE_QUEUE_WARN or stuck else "OK",
    }


def check_calendar(db):
    total = db.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0]
    # MAX(end_at) is an event END date, routinely months in the future, so it
    # stays high off events already stored no matter how long the sync has been
    # dead — this row returned a literal "OK" and could not go red. ingested_at
    # is stamped by the loader, so it is the only column here that ages when
    # ingestion stops.
    latest = db.execute("SELECT MAX(ingested_at) FROM calendar_events").fetchone()[0]
    age = _age(latest)
    stale = age is not None and age > STALE_THRESHOLDS["calendar"]
    return {
        "name": "Calendar",
        "total": total,
        "latest": latest,
        "age": age,
        "stale": stale,
        # No parseable ingested_at at all means the column is empty or the table
        # is — either way nothing here can be trusted as fresh.
        "status": "STALE" if stale else ("WARN" if age is None else "OK"),
    }


def check_conversations(db):
    total = db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    turns = db.execute("SELECT COUNT(*) FROM conversation_turns").fetchone()[0]
    latest = db.execute("SELECT MAX(started_at) FROM conversations").fetchone()[0]

    age = _age(latest)

    stale = age and age > STALE_THRESHOLDS["conversations"]
    return {
        "name": "Conversations",
        "total": total,
        "turns": turns,
        "latest": latest,
        "age": age,
        "stale": stale,
        "status": "STALE" if stale else "OK",
    }


def check_embeddings(db, npz_path: Path | None = None, now: datetime | None = None):
    total_emails = db.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    import numpy as np

    npz = npz_path or DB_PATH.parent / "embeddings.npz"
    if not npz.exists():
        return {
            "name": "Embeddings",
            "embedded": 0,
            "total_emails": total_emails,
            "age": None,
            "status": "WARN",
        }
    d = np.load(str(npz), allow_pickle=True)
    n = len(d["ids"]) if "ids" in d else 0
    # Presence and vector count both freeze the instant the stage dies, so the
    # two signals this check used could not tell a live index from one written
    # months ago. The file's mtime is the only thing here that keeps moving.
    age = (now or datetime.now()) - datetime.fromtimestamp(npz.stat().st_mtime)
    stale = age > STALE_THRESHOLDS["embeddings"]
    return {
        "name": "Embeddings",
        "embedded": n,
        "total_emails": total_emails,
        "age": age,
        "stale": stale,
        "status": "STALE" if stale else "OK",
    }


def check_sharepoint(db):
    try:
        rows = db.execute(
            "SELECT last_status, COUNT(*) FROM sharepoint_links GROUP BY last_status"
        ).fetchall()
        status_map = {r[0]: r[1] for r in rows}
        # Derive the tally from the rows present rather than a hand-listed subset.
        # FetchStatus has five members (src/export/sharepoint_fetcher.py) but this
        # summed only ok+exception+stale, so 'http-error' — where every HTTP
        # failure lands — was missing from the denominator: prod printed "973/996"
        # against 1,016 real rows and called 20 unfetched links "0 exceptions".
        # Fail-closed on anything that is not 'ok', so a status added upstream
        # later shows up as a problem instead of silently disappearing again.
        total = sum(status_map.values())
        ok = status_map.get("ok", 0)
        failed = total - ok
        # A ratio cannot move once the fetcher stops: no row changes status, so
        # the share of non-'ok' rows is pinned and this reads OK forever. Age the
        # ELIGIBLE work instead. Links past MAX_SHAREPOINT_ATTEMPTS are resting
        # inside their cool-off by design — prod's 43 sit at 6-9 attempts — and
        # must stay silent, but a link still inside its retry budget that nobody
        # has touched for days means the fetcher is not running at all.
        overdue = db.execute(
            "SELECT COUNT(*) FROM sharepoint_links "
            "WHERE fetched_at IS NULL AND COALESCE(attempts, 0) < ? "
            "AND (last_attempt_at IS NULL "
            "     OR datetime(last_attempt_at) < datetime('now', ?))",
            (SHAREPOINT_MAX_ATTEMPTS, f"-{STALE_THRESHOLDS['sharepoint'].days} days"),
        ).fetchone()[0]
        latest_attempt = db.execute("SELECT MAX(last_attempt_at) FROM sharepoint_links").fetchone()[
            0
        ]
        if overdue:
            status = "STALE"
        elif failed > total * 0.3:
            status = "WARN"
        else:
            status = "OK"
        return {
            "name": "SharePoint",
            "ok": ok,
            "failed": failed,
            "by_status": {s: n for s, n in status_map.items() if s != "ok"},
            "total": total,
            "overdue": overdue,
            "age": _age(latest_attempt),
            "stale": bool(overdue),
            "status": status,
        }
    except sqlite3.OperationalError:
        return {"name": "SharePoint", "status": "N/A"}


def check_sharepoint_token(path: Path = SHAREPOINT_SESSION, now: datetime | None = None):
    """Surface an expired/expiring SharePoint session token.

    The SharePoint bearer is a *separate* token from the Outlook mail session and
    does not renew headlessly once fully expired — it needs an interactive
    `sharepoint-cli login --host <host>`. When it lapses, mail keeps
    working (its token still renews) while every SharePoint fetch fails with
    SHAREPOINT_SESSION_MISSING. The file mtime stays fresh because the token-sync
    job keeps copying the dead token, so mtime alone can't detect this — only the
    embedded tokenExpiresAt can. This check makes that rot visible instead of
    silent.
    """
    now = now or datetime.now(UTC)
    if not path.exists():
        return {"name": "SP Session", "status": "N/A"}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return {"name": "SP Session", "status": "WARN", "error": str(e)}

    exp = data.get("tokenExpiresAt")
    try:
        dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return {"name": "SP Session", "status": "WARN", "error": f"bad tokenExpiresAt: {exp!r}"}

    remaining = dt - now
    if remaining.total_seconds() <= 0:
        status = "FAIL"
    elif remaining < timedelta(hours=24):
        status = "WARN"
    else:
        status = "OK"
    # This row asserts on time-to-expiry, not on age, so it is the one legitimate
    # blank in the age column — and a blank there is exactly how the checks that
    # could NOT go red used to look. Showing when the session was captured keeps
    # "'?' means nothing is being asserted" true as a reading of the report.
    captured = data.get("capturedAt")
    try:
        age = now - datetime.fromisoformat(str(captured).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        age = None
    return {
        "name": "SP Session",
        "status": status,
        "expires_at": exp,
        "remaining": remaining,
        "age": age,
    }


def check_documents(db):
    total = db.execute("SELECT COUNT(*) FROM emails WHERE message_id < 0").fetchone()[0]
    latest = db.execute("SELECT MAX(date_received) FROM emails WHERE message_id < 0").fetchone()[0]
    return {
        "name": "Documents",
        "total": total,
        "latest": latest,
        "age": _age(latest),
        # Deliberately not a staleness gate. These roots go months without a new
        # file — 0 added in the 14 days to 2026-08-22 — so ageing the newest
        # ingested document would page on the normal state of the source.
        # Liveness for reverse-ingest is asserted by check_document_roots, which
        # watches the sync stamp (job ran) and its failure marker (job failed)
        # rather than the data. The age is exposed here for the report only.
        "status": "OK",
    }


def _read_push_failure(marker: Path, last_success):
    """(failing, reason) from the push job's failure marker.

    A marker older than the last recorded success has been superseded by that
    success and is ignored. An unparseable marker still counts as failing: its
    existence is the evidence, and swallowing it would restore the blind spot
    this marker exists to close.
    """
    try:
        raw = Path(marker).read_text(errors="replace").strip()
    except OSError:
        return False, None
    if not raw:
        return False, None
    lines = raw.splitlines()
    reason = lines[1].strip() if len(lines) > 1 else None
    failed_at = _utc(lines[0].strip())
    if failed_at is None:
        return True, reason or "unreadable failure marker"
    if last_success is not None and failed_at <= last_success:
        return False, None
    return True, reason


def check_document_roots(
    roots=None,
    now=None,
    stamp: Path = DOCUMENT_SYNC_STAMP,
    fail_marker: Path = DOCUMENT_SYNC_FAIL,
):
    """Freshness of the reverse-ingest *input*, not its output.

    check_documents only proves the job wrote rows; it stays green when the
    document roots are a frozen copy (a disconnected mirror whose newest file
    is months old) because the job still runs and still writes. This looks at
    the source.

    Liveness comes from DOCUMENT_SYNC_STAMP when it is readable: newest mtime
    alone can be manufactured by our own curate-docs job, which mirrors
    attachments into these very roots (filename is no discriminator — the
    operator uses the same naming convention). Without a usable stamp we fall
    back to the newest mtime among ingestable files and say so in the report.
    """
    roots = DOCUMENT_ROOTS if roots is None else [Path(r) for r in roots]
    now = now or datetime.now(UTC)

    per_root = []
    missing = []
    total = 0
    newest = None
    for root in roots:
        if not root.is_dir():
            missing.append(root.name)
            per_root.append({"name": root.name, "files": 0, "age": None, "missing": True})
            continue
        files = 0
        root_newest = None
        for path in root.rglob("*"):
            if path.suffix.lower() not in INGESTABLE_EXTENSIONS:
                continue
            try:
                if not path.is_file():
                    continue
                mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            except OSError:
                continue
            files += 1
            # Clamp to `now`, like check_news does with published_at: one
            # clock-skewed file dated in the future would otherwise become the
            # "newest input", making the age negative and the check OK forever.
            if mtime > now:
                continue
            if root_newest is None or mtime > root_newest:
                root_newest = mtime
        total += files
        if root_newest is not None and (newest is None or root_newest > newest):
            newest = root_newest
        per_root.append(
            {
                "name": root.name,
                "files": files,
                "age": None if root_newest is None else now - root_newest,
                "missing": False,
            }
        )

    age = None if newest is None else now - newest

    stamp_at = None
    if stamp is not None:
        try:
            # errors="replace": a clobbered/binary stamp must not raise —
            # main() runs the checks unguarded, so one bad byte here would kill
            # the whole nightly report and send no email at all.
            stamp_at = _utc(Path(stamp).read_text(errors="replace").strip())
        except OSError:
            stamp_at = None
    stamp_age = None if stamp_at is None else now - stamp_at
    # A stamp dated in the future is a clock-skewed write, not evidence of a
    # live source. Same clamp as the mtime scan above; without it one bad write
    # pins this check to OK forever.
    if stamp_age is not None and stamp_age < timedelta(0):
        stamp_age = None

    if stamp_age is None:
        note = "no sync stamp — using file mtimes"
        stale = age is not None and age > STALE_THRESHOLDS["document_roots"]
    else:
        note = None
        stale = stamp_age > STALE_THRESHOLDS["document_roots"]

    # A failing push outranks both signals above. The stamp records only
    # successes and is judged against a 14-day window, so a run that fails every
    # time emits nothing until that window expires — 56 consecutive TCC denials
    # reported OK for 8 days on exactly this path.
    push_failing, fail_reason = _read_push_failure(fail_marker, stamp_at)
    if push_failing:
        stale = True
        note = f"push FAILING: {fail_reason}" if fail_reason else "push FAILING"

    return {
        "name": "Doc Roots",
        "total": total,
        "latest": None if newest is None else newest.isoformat(),
        "age": age,
        "stamp_age": stamp_age,
        "roots": per_root,
        "missing": missing,
        "note": note,
        "stale": stale,
        "push_failing": push_failing,
        "status": "STALE" if stale else ("WARN" if missing or age is None else "OK"),
    }


def check_news(db, news_db=None, now=None):
    """Upstream news database vs. what actually reached the brain.

    Same question as check_document_roots, other source: the news pipeline can
    keep publishing while ingestion silently stops, and every downstream check
    still passes. STALE means the source moved on and we did not. Never raises
    — an absent upstream or a brain not backfilled yet is informational (N/A,
    like SharePoint without a session file), since those are steady states that
    would otherwise make every nightly report unhealthy. Only a corrupt/
    unreadable upstream file is a WARN.
    """
    path = Path(news_db) if news_db else NEWS_DB
    now = now or datetime.now(UTC)

    ingested, latest = db.execute(
        "SELECT COUNT(*), MAX(date_received) FROM emails WHERE mailbox_name = 'News'"
    ).fetchone()
    ingested_at = _utc(latest)
    result = {
        "name": "News",
        "total": ingested,
        "latest": latest,
        "age": None if ingested_at is None else now - ingested_at,
        "upstream_latest": None,
        "lag": None,
        "stale": False,
        "status": "N/A",
    }

    if not path.exists():
        result["note"] = "news db not found"
        return result

    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            # Clamp to `now`: some digest-pipeline rows carry published_at months
            # in the future, and a naive MAX() would report permanent staleness.
            article = con.execute(
                "SELECT MAX(published_at) FROM articles WHERE published_at <= ?",
                (now.isoformat(),),
            ).fetchone()[0]
            digest = con.execute("SELECT MAX(created_at) FROM digests").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as e:
        result["note"] = f"news db unreadable: {e}"
        result["status"] = "WARN"
        return result

    upstream = max((t for t in (article, digest) if _utc(t) is not None), key=_utc, default=None)
    result["upstream_latest"] = upstream

    if ingested == 0:
        result["note"] = "no News rows ingested yet"
        return result
    if upstream is None or ingested_at is None:
        result["note"] = "no upstream material"
        return result

    result["lag"] = _utc(upstream) - ingested_at
    result["stale"] = result["lag"] > STALE_THRESHOLDS["news"]
    result["status"] = "STALE" if result["stale"] else "OK"
    return result


def _is_migrated(label: str, agents_dir: Path = LAUNCH_AGENTS_DIR) -> bool:
    """Whether a launchd job was intentionally retired to the VPS.

    When ingestion moved to the VPS, the Mac's ingestion plists were renamed to
    `<label>.plist.disabled-migrated-to-vps`. Such a label is deliberately gone,
    not broken — reporting it as NOT_LOADED read as a fault on every run.
    """
    disabled = agents_dir / f"{label}.plist.disabled-migrated-to-vps"
    active = agents_dir / f"{label}.plist"
    return disabled.exists() and not active.exists()


def _check_jobs_launchd():
    results = {}
    for label, desc in LAUNCHD_JOBS.items():
        try:
            out = subprocess.run(
                [launchctl_bin(), "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in out.stdout.strip().split("\n"):
                parts = line.split("\t")
                if len(parts) == 3 and parts[2] == label:
                    pid = parts[0]
                    exit_code = parts[1]
                    running = pid != "-" and pid != "0"
                    ok = exit_code == "0"
                    results[label] = {
                        "desc": desc,
                        "pid": pid,
                        "exit_code": exit_code,
                        "running": running,
                        "status": "RUNNING"
                        if running
                        else ("OK" if ok else f"FAIL(exit={exit_code})"),
                    }
                    break
            else:
                results[label] = {
                    "desc": desc,
                    "status": "MIGRATED" if _is_migrated(label) else "NOT_LOADED",
                }
        except Exception as e:
            results[label] = {"desc": desc, "status": f"ERROR: {e}"}
    return results


def _check_jobs_systemd():
    results = {}
    for unit, desc in SYSTEMD_UNITS.items():
        try:
            out = subprocess.run(
                [
                    systemctl_bin(),
                    "--user",
                    "show",
                    unit,
                    "--property=LoadState,ActiveState,SubState,Result,ExecMainStatus",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            props = dict(
                line.split("=", 1) for line in out.stdout.strip().splitlines() if "=" in line
            )
            load = props.get("LoadState", "")
            if load in ("", "not-found", "masked"):
                results[unit] = {"desc": desc, "status": "NOT_LOADED"}
                continue
            active = props.get("ActiveState", "")
            result = props.get("Result", "success")
            exit_code = props.get("ExecMainStatus", "0")
            running = active in ("active", "activating", "reloading")
            # Trust systemd's own verdict (ActiveState/Result), NOT ExecMainStatus:
            # after a `reset-failed`, Result reverts to "success" while
            # ExecMainStatus stays stale at the last process's exit code. Timer
            # oneshots sit inactive(dead)/Result=success between runs — healthy.
            if active == "failed" or result not in ("success", ""):
                status = f"FAIL(result={result})"
            elif running:
                status = "RUNNING"
            else:
                status = "OK"
            results[unit] = {
                "desc": desc,
                "active": active,
                "result": result,
                "exit_code": exit_code,
                "running": running,
                "status": status,
            }
        except Exception as e:
            results[unit] = {"desc": desc, "status": f"ERROR: {e}"}
    return results


def check_jobs():
    """Scheduled-job status — launchd on macOS, systemd --user on Linux (VPS)."""
    return _check_jobs_launchd() if IS_MACOS else _check_jobs_systemd()


def check_sync_logs(log_dir: Path | None = None):
    """Log freshness for every scheduled job, derived from the job registry.

    Four filenames used to be hand-listed, so five of the nine jobs — including
    attachments and calendar-sync — had no log-age signal at all: a dead one
    raised nothing here because its log simply was not in the dict. Names come
    off SYSTEMD_UNITS on both platforms; the log basenames match the unit stems
    (`sb-outlook-sync.service` -> `outlook-sync.log`) regardless of host.
    """
    log_dir = log_dir or LOG_DIR
    results = {}
    log_files = {
        unit.removeprefix("sb-").removesuffix(".service").replace("-", "_"): log_dir
        / f"{unit.removeprefix('sb-').removesuffix('.service')}.log"
        for unit in SYSTEMD_UNITS
    }
    for name, path in log_files.items():
        if path.exists():
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            age = datetime.now() - mtime
            results[name] = {
                "path": str(path),
                "last_modified": mtime.isoformat(),
                "age": age,
                "stale": age > STALE_THRESHOLDS.get(name, timedelta(days=1)),
            }
        else:
            results[name] = {"path": str(path), "status": "MISSING"}
    return results


def check_sentinels(state_dir: Path | None = None):
    """Blocking sentinels, discovered by pattern rather than by list.

    Two filenames were hardcoded, so a `needs_*` file written by any job added
    later was invisible — a blocking condition nobody would ever be told about.
    The two known names stay in the set so they still report absent-and-OK when
    the state dir is empty.
    """
    state_dir = state_dir or STATE_DIR
    results = {}
    known = ("needs_reauth", "needs_gcloud_reauth")
    discovered = {p.name for p in state_dir.glob("needs_*")} if state_dir.exists() else set()
    sentinels = {name: state_dir / name for name in sorted(discovered.union(known))}
    for name, path in sentinels.items():
        if path.exists():
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            results[name] = {
                "present": True,
                "since": mtime.isoformat(),
                "status": "BLOCKING",
            }
        else:
            results[name] = {"present": False, "status": "OK"}
    return results


def auto_fix(issues):
    """Attempt to fix detected issues. Returns list of actions taken.

    Platform-agnostic: kick_job() dispatches to launchctl (macOS) or
    systemctl --user (Linux/VPS).
    """
    actions = []

    for issue in issues:
        if issue["type"] == "sentinel" and issue["name"] == "needs_reauth":
            try:
                kick_job(AUTH_WATCH_JOB)
                actions.append("Kicked auth-watch to attempt silent renewal")
            except Exception as e:
                actions.append(f"Failed to kick auth-watch: {e}")

        elif issue["type"] == "job_failed":
            label = issue["label"]
            try:
                kick_job(label)
                actions.append(f"Re-kicked {label}")
            except Exception as e:
                actions.append(f"Failed to kick {label}: {e}")

        elif issue["type"] == "stale_data":
            label = issue["label"]
            try:
                kick_job(label)
                actions.append(f"Kicked {label} to drain staging ({issue['name']} stale)")
            except Exception as e:
                actions.append(f"Failed to kick {label}: {e}")

    return actions


def format_age(td):
    if not td:
        return "?"
    total_secs = int(td.total_seconds())
    if total_secs < 3600:
        return f"{total_secs // 60}m"
    if total_secs < 86400:
        return f"{total_secs // 3600}h {(total_secs % 3600) // 60}m"
    return f"{total_secs // 86400}d {(total_secs % 86400) // 3600}h"


def build_report(checks, jobs, logs, sentinels, fix_actions):
    now = datetime.now()
    lines = []
    issues = []

    lines.append(f"Second Brain Health Report — {now.strftime('%Y-%m-%d %H:%M')}")
    lines.append("=" * 55)

    # Sentinels (blocking issues first)
    blocking = [s for s, v in sentinels.items() if v.get("present")]
    if blocking:
        lines.append("")
        lines.append("BLOCKING SENTINELS:")
        for s in blocking:
            lines.append(f"  {s}: present since {sentinels[s]['since']}")
        issues.extend(blocking)

    # Data sources
    lines.append("")
    lines.append("DATA SOURCES")
    lines.append("-" * 55)
    fmt = "{:<18} {:>8} {:>7} {:>6}"
    lines.append(fmt.format("Source", "Count", "Age", "Status"))
    lines.append("-" * 55)

    for c in checks:
        count = c.get("total", c.get("embedded", "?"))
        age = format_age(c.get("age"))
        status = c.get("status", "?")
        extra = ""
        if c["name"] == "Attachments":
            extra = (
                f" (LLM: {c.get('llm_done', 0):,} done, {c.get('llm_pending', 0):,} pending"
                f", {c.get('llm_no_text', 0):,} no-text)"
            )
        elif c["name"] == "Inline Images":
            extra = f" ({c.get('pct', 0):.0f}% classified, {c.get('pending', 0):,} queued)"
            if c.get("stuck"):
                extra += (
                    f" — {c['stuck']:,} awaiting vision, last output {format_age(c.get('age'))} ago"
                )
        elif c["name"] == "Emails":
            extra = f" ({c.get('recent_24h', 0)} today)"
        elif c["name"] == "Teams":
            extra = f" ({c.get('recent_24h', 0)} today)"
            if c.get("coverage_lost"):
                extra += (
                    f" — {c['chats_disabled']:,} of {c.get('chats', 0):,} chats dropped"
                    " from ingestion"
                )
                # The date is what separates one bad sweep from slow attrition.
                if c.get("disabled_at"):
                    extra += f" (latest {str(c['disabled_at'])[:19]})"
        elif c["name"] == "SharePoint":
            count = f"{c.get('ok', 0)}/{c.get('total', 0)}"
            failed = c.get("failed", 0)
            if failed:
                breakdown = ", ".join(f"{n} {s}" for s, n in sorted(c.get("by_status", {}).items()))
                extra = f" ({failed} unfetched: {breakdown})"
            else:
                extra = " (all fetched)"
        elif c["name"] == "Doc Roots":
            if c.get("push_failing"):
                # The marker names the cause; hedging about a disconnected
                # source on top of it would only bury the actual reason.
                extra = (
                    f" (last success {format_age(c['stamp_age'])} ago)"
                    if c.get("stamp_age") is not None
                    else ""
                )
            elif c.get("stale"):
                signal = (
                    f"last push {format_age(c['stamp_age'])} ago"
                    if c.get("stamp_age") is not None
                    else f"newest input {format_age(c.get('age'))} old"
                )
                extra = f" ({signal} — source may be disconnected)"
            elif c.get("missing"):
                extra = f" (missing: {', '.join(c['missing'])})"
            else:
                extra = " (" + ", ".join(f"{r['name']} {r['files']:,}" for r in c["roots"]) + ")"
            if c.get("note"):
                extra += f" ({c['note']})"
        elif c["name"] == "News":
            if c.get("stale"):
                extra = f" (upstream {format_age(c.get('lag'))} ahead of last ingested)"
            elif c.get("note"):
                extra = f" ({c['note']})"
        elif c["name"] == "SP Session":
            count = "—"
            rem = c.get("remaining")
            if c.get("status") == "N/A":
                extra = " (no session file)"
            elif rem is not None and rem.total_seconds() <= 0:
                extra = (
                    f" (EXPIRED {str(c.get('expires_at', ''))[:10]} — run: "
                    f"sharepoint-cli login --host {SHAREPOINT_HOST})"
                )
            elif rem is not None:
                extra = f" (expires in {format_age(rem)})"

        count_str = f"{count:,}" if isinstance(count, int) else str(count)
        lines.append(f"  {c['name']:<16} {count_str:>8} {age:>7}  {status}{extra}")

        if status in ("STALE", "FAIL", "WARN"):
            issues.append(f"{c['name']}: {status}")

    # LaunchD jobs
    lines.append("")
    lines.append("SCHEDULED JOBS")
    lines.append("-" * 55)
    for label, info in sorted(jobs.items()):
        desc = info.get("desc", label)
        status = info.get("status", "?")
        marker = "OK" if status in ("OK", "RUNNING") else status
        lines.append(f"  {desc:<28} {marker}")
        if "FAIL" in str(status):
            issues.append(f"Job {desc}: {status}")
    # Every job MIGRATED means this host checked nothing: the plists are retired
    # and the systemd path only runs on the VPS. Nine "MIGRATED" lines otherwise
    # read as nine healthy jobs, when a crashed, failed or masked VPS unit looks
    # exactly the same from here. Say so rather than implying coverage.
    jobs_elsewhere = bool(jobs) and all(info.get("status") == "MIGRATED" for info in jobs.values())
    if jobs_elsewhere:
        lines.append("")
        lines.append("  NOTE: job health is not verifiable from this host — these jobs run on")
        lines.append("        the VPS and are asserted by the health check that runs there.")

    # Job logs. This result used to be computed and dropped: build_report took
    # `logs` and never read it, so a job holding a healthy systemd unit while
    # writing nothing to its log raised no signal anywhere. Skipped entirely
    # when the jobs run elsewhere — this host's log dir is then a
    # pre-migration relic describing a machine that stopped running them.
    # An ABSENT log carries no "stale" flag, so it used to be silence rather
    # than a signal — the same shape as every other blind spot in this file.
    # All twelve resolve on the VPS today, so a missing one is a real fault.
    bad_logs = (
        sorted(
            (name, info)
            for name, info in logs.items()
            if info.get("stale") or info.get("status") == "MISSING"
        )
        if logs and not jobs_elsewhere
        else []
    )
    if bad_logs:
        lines.append("")
        lines.append("JOB LOGS")
        lines.append("-" * 55)
        for name, info in bad_logs:
            if info.get("status") == "MISSING":
                lines.append(f"  {name:<28} MISSING ({info.get('path')})")
                issues.append(f"Log {name}: missing")
            else:
                lines.append(f"  {name:<28} {format_age(info.get('age'))} since last write")
                issues.append(f"Log {name}: stale")

    # Fix actions
    if fix_actions:
        lines.append("")
        lines.append("AUTO-FIX ACTIONS TAKEN")
        lines.append("-" * 55)
        for a in fix_actions:
            lines.append(f"  {a}")

    # Summary
    lines.append("")
    lines.append("=" * 55)
    if issues:
        lines.append(f"ISSUES: {len(issues)}")
        for i in issues:
            lines.append(f"  - {i}")
    else:
        lines.append("ALL SYSTEMS HEALTHY")
    lines.append("=" * 55)

    return "\n".join(lines), issues


def build_html(text_report, issues):
    """Convert text report to styled HTML for email."""
    status_color = "#006141" if not issues else "#AA0028"
    status_text = (
        "ALL HEALTHY" if not issues else f"{len(issues)} ISSUE{'S' if len(issues) > 1 else ''}"
    )

    pre_html = text_report.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    return f"""<div style="font-family: Aptos, Calibri, sans-serif; font-size: 12pt; color: #404040;">
<span style="color: {status_color}; font-weight: bold;">Second Brain Status: {status_text}</span>
<br><br>
<pre style="font-family: 'SF Mono', 'Cascadia Code', Consolas, monospace; font-size: 10pt; color: #404040; background: #f5f5f5; padding: 16px; border-radius: 8px; overflow-x: auto;">{pre_html}</pre>
</div>"""


# Healthchecks slug for the data-freshness signal, pinged only when --hc-ping is
# passed. Distinct from the 'sb-health-check' check, which says only that the
# reporter RAN: this one says the data the reporter looked at is fresh. The check
# already exists in Healthchecks, so no '?create=1' here. Auto-provision is what
# left eleven checks on the 'every minute / 1d timeout / UTC' default, and this
# must not add a twelfth.
FRESHNESS_SLUG = "sb-brain-freshness"


def freshness_verdict(checks, sentinels):
    """(ok, detail) for the data-freshness ping.

    STALE only. Two things are deliberately NOT failures here:

    WARN, because it is a quality signal (Teams coverage share, inline-image
    queue depth) that the nightly email already carries in full, and because
    alert volume is the standing problem: 64% of the Healthchecks mail in the 30
    days to 2026-08-25 described a condition that self-resolved within two hours.
    A dead-man's switch that fires on quality drift stops being read, which is
    how a genuine 14-day red went unnoticed on this very check.

    A blocking sentinel, because setting one is the sanctioned way to degrade
    gracefully while a re-auth is pending. Failing on the sentinel would put this
    check red for the whole latch period, which is precisely the outcome the
    owner ruled out when the same question was asked of the sync jobs' exit
    codes. If the latch lasts long enough to matter, the affected source crosses
    its own STALE threshold (Teams: 24h on message recency, 8h on last poll) and
    the ping goes red then, on evidence rather than on intent.

    ``sentinels`` is accepted and unused for exactly that reason: the argument
    documents the decision at the call site instead of hiding it.
    """
    stale = sorted(c["name"] for c in checks if c.get("status") == "STALE")
    if stale:
        return False, "stale: " + ", ".join(stale)
    return True, f"{len(checks)} sources fresh"


def ping_freshness(ok, detail, slug=FRESHNESS_SLUG):
    """Fire-and-forget freshness ping. Never raises, never changes the exit code.

    Same curl invocation as hc-success@.service, so the retry and timeout
    behaviour is identical to every other ping on the box.

    curl's own stderr is never printed: -fsS writes the failing URL into it, and
    that URL carries the Healthchecks ping key. Only the exit code is logged.
    """
    base = os.environ.get("HC_PING_URL")
    if not base:
        print("HC_PING_URL not set, skipping freshness ping.", file=sys.stderr)
        return False

    url = f"{base.rstrip('/')}/{slug}" + ("" if ok else "/fail")
    try:
        result = subprocess.run(
            [
                "curl",
                "-fsS",
                "-m",
                "10",
                "--retry",
                "3",
                "-o",
                "/dev/null",
                "--data-binary",
                "@-",
                url,
            ],
            input=detail[:10000],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:
        print(f"Freshness ping failed: {type(e).__name__}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"Freshness ping failed: curl exit {result.returncode}", file=sys.stderr)
        return False
    return True


def send_email(html_body, issues):
    """Send via outlook-cli (--html takes a file path). Requires HEALTH_EMAIL_TO."""
    import tempfile

    recipient = os.environ.get("HEALTH_EMAIL_TO")
    if not recipient:
        print("HEALTH_EMAIL_TO not set — skipping email.", file=sys.stderr)
        return False

    status = "healthy" if not issues else f"{len(issues)} issues"
    subject = f"second brain health — {datetime.now().strftime('%Y-%m-%d')} — {status}"

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".html", prefix="sb-health-", delete=False)
    try:
        tmp.write(html_body)
        tmp.close()

        env = os.environ.copy()
        # outlook-cli is a Node tool; if fnm manages Node, add its latest bin to PATH.
        fnm_versions = Path.home() / ".local/share/fnm/node-versions"
        if fnm_versions.is_dir():
            node_bins = sorted(fnm_versions.glob("*/installation/bin"))
            if node_bins:
                env["PATH"] = f"{node_bins[-1]}:{env.get('PATH', '')}"

        result = subprocess.run(
            [
                "outlook-cli",
                "send-mail",
                "--to",
                recipient,
                "--subject",
                subject,
                "--html",
                tmp.name,
                "--send-now",
                "--no-open",
                "--no-cc-self",
                "--no-signature",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        if result.returncode != 0:
            print(f"outlook-cli stderr: {result.stderr[:200]}", file=sys.stderr)
        return result.returncode == 0
    except Exception as e:
        print(f"Email send failed: {e}", file=sys.stderr)
        return False
    finally:
        os.unlink(tmp.name)


def main():
    parser = argparse.ArgumentParser(description="Second Brain health check")
    parser.add_argument("--email", action="store_true", help="Send report via email")
    parser.add_argument(
        "--email-if-issues",
        action="store_true",
        help="Send report via email only when unresolved issues remain (quiet on healthy runs)",
    )
    parser.add_argument("--fix", action="store_true", help="Auto-fix detected issues")
    parser.add_argument(
        "--hc-ping",
        action="store_true",
        help=(
            "Report data freshness to Healthchecks (needs HC_PING_URL). Off by default "
            "so an ad-hoc manual run cannot reset the dead-man's switch."
        ),
    )
    args = parser.parse_args()

    db = get_db()
    if not db:
        print("ERROR: Database not found", file=sys.stderr)
        sys.exit(1)

    checks = [
        check_emails(db),
        check_teams(db),
        check_attachments(db),
        check_images(db),
        check_calendar(db),
        check_conversations(db),
        check_embeddings(db),
        check_documents(db),
        check_document_roots(),
        check_news(db),
        check_sharepoint(db),
        check_sharepoint_token(),
    ]
    db.close()

    jobs = check_jobs()
    logs = check_sync_logs()
    sentinels = check_sentinels()

    # Detect fixable issues
    fixable = []
    for name, info in sentinels.items():
        if info.get("present"):
            fixable.append({"type": "sentinel", "name": name})
    for label, info in jobs.items():
        if "FAIL" in str(info.get("status", "")):
            fixable.append({"type": "job_failed", "label": label})
    # Stale-data backstop: if emails went stale, kick the loader so the next run
    # drains staging. The hourly sync now loads every hour, so this should rarely
    # fire — it's defense-in-depth for when the hourly load is skipped/failing.
    for c in checks:
        if c.get("stale") and c.get("name") == "Emails":
            fixable.append(
                {
                    "type": "stale_data",
                    "name": c["name"],
                    "label": LOADER_JOB,
                }
            )

    fix_actions = auto_fix(fixable) if args.fix else []

    text_report, issues = build_report(checks, jobs, logs, sentinels, fix_actions)
    print(text_report)

    # Before the email, not after: a send_email failure exits 1, and the ping is
    # the dead-man's switch for the data, not for the mail.
    if args.hc_ping:
        ping_freshness(*freshness_verdict(checks, sentinels))

    if args.email or (args.email_if_issues and issues):
        html = build_html(text_report, issues)
        ok = send_email(html, issues)
        if ok:
            print("\nEmail sent successfully.")
        else:
            print("\nEmail send FAILED.", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
