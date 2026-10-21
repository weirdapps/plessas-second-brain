"""Pre-meeting briefs: a two-week pilot that mails the owner a brief before some meetings.

Push the owner did not ask for has a poor record: in six weeks he asked for no briefing
and ran no morning digest, and OpenAI and Microsoft withdrew their generic daily feeds in
the summer of 2026. What survives is push bound to an event. So this is a pilot with a
stop rule, not a feature: a timer every 15 minutes on the producer, and for each meeting
that starts in 30 to 45 minutes and qualifies, one brief built from meeting_prep and one
model call, mailed to the owner and to no one else.

A meeting qualifies when someone outside the owner's own domain attends, or someone he
has not met in the last MET_DAYS days, unless it is an occurrence of a recurring internal
series, or more than MAX_ATTENDEES attend (a town hall is not prepared for).

The stop rule:
- the pilot ends by itself after BRAIN_BRIEF_PILOT_UNTIL (YYYY-MM-DD), which the install
  sets to the install date plus 14 days (docs/DEPLOY.md); moving the date is the only way
  to extend it, and with no date set nothing is sent;
- BRAIN_BRIEFS_OFF=1 is the kill switch;
- at most MAX_PER_DAY briefs a day.

Each brief is logged, with its meeting id and what its call cost, to
`state/briefs.jsonl` under the data home; the log is also what keeps a meeting from being
briefed twice. The mail goes through the path the health check mails the owner by
(outlook-cli send-mail to HEALTH_EMAIL_TO), and only when that address is one of his own
(BRAIN_USER_EMAIL_PATTERN).
"""

import html
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.config import DATA_ROOT
from src.store.owner import is_owner_address, owner_addresses

LEAD_MIN_MINUTES = 30
LEAD_MAX_MINUTES = 45
MET_DAYS = 90
MAX_ATTENDEES = 20
MAX_PER_DAY = 8
# The attendees a brief's dossiers cover at most, the ones the rule picked first.
DOSSIER_PEOPLE = 6
DOSSIER_CHARS = 30_000
MAX_TOKENS = 3_000
EFFORT = "medium"
LOG_NAME = "briefs.jsonl"

SYSTEM = """\
You write a short brief an executive reads in the 30 minutes before a meeting. You are \
given the meeting and, for the attendees, what his mail, decisions, open actions and \
earlier meetings hold about them.

Write plain text, at most 250 words, in English, in three parts with these headings:
WHO: one line per attendee who matters: who they are, and the last contact or exchange \
with him, dated.
OPEN ITEMS: what is owed either way, what is pending a decision, dated.
WATCH: one to three points worth raising or checking.
Use only what the material states; when it holds nothing on someone, say "nothing held". \
No greeting, no sign-off.\
"""


def log_path() -> Path:
    return DATA_ROOT / "state" / LOG_NAME


def pilot_state(environ: Mapping[str, str], today: date) -> tuple[bool, str]:
    """Whether the pilot runs today, and the sentence that says why or why not."""
    if environ.get("BRAIN_BRIEFS_OFF", "").strip() not in ("", "0"):
        return False, "BRAIN_BRIEFS_OFF is set: the kill switch is on; nothing sent"
    raw = environ.get("BRAIN_BRIEF_PILOT_UNTIL", "").strip()
    if not raw:
        return False, "BRAIN_BRIEF_PILOT_UNTIL is not set: the pilot is not installed"
    try:
        until = date.fromisoformat(raw)
    except ValueError:
        return False, f"BRAIN_BRIEF_PILOT_UNTIL is not a date: {raw!r}; nothing sent"
    if today > until:
        return False, f"pilot ended on {until.isoformat()}; nothing sent"
    return True, f"pilot runs until {until.isoformat()}"


def owner_recipient(environ: Mapping[str, str], pattern: str) -> str | None:
    """HEALTH_EMAIL_TO, when it is one address and one of the owner's own; else None."""
    to = environ.get("HEALTH_EMAIL_TO", "").strip()
    if not to or any(sep in to for sep in ",; ") or not is_owner_address(to, pattern):
        return None
    return to


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def slot(now: datetime) -> datetime:
    """The quarter hour `now` falls in: the timer fires on the quarter hours, a few seconds
    late, and each meeting must fall in exactly one run's window."""
    return now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)


def upcoming(conn, now: datetime) -> list[dict]:
    """The meetings that start LEAD_MIN to LEAD_MAX minutes after `now`'s quarter hour (the
    end excluded), not cancelled and not declined. Stored times are UTC."""
    start = _stamp(slot(now) + timedelta(minutes=LEAD_MIN_MINUTES))
    end = _stamp(slot(now) + timedelta(minutes=LEAD_MAX_MINUTES))
    # '2026-10-16T09:45:00Z' sorts after '2026-10-16T09:45:00': the end is excluded.
    cursor = conn.execute(
        """SELECT id, outlook_event_id, subject, start_at, end_at, location, organizer_name,
                  organizer_email, body_summary, is_recurring, response_status
           FROM calendar_events
           WHERE is_cancelled = 0 AND start_at >= ? AND start_at < ?
             AND LOWER(COALESCE(response_status, '')) NOT LIKE 'declined%'
           ORDER BY start_at, id""",
        (start, end),
    )
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _domain(address: str | None) -> str:
    return (address or "").strip().lower().rpartition("@")[2]


def qualifies(conn, event: dict, pattern: str, now: datetime) -> tuple[bool, str, list[str]]:
    """Whether `event` gets a brief, why, and the attendees its dossiers cover first."""
    rows = conn.execute(
        "SELECT email, name, is_self FROM event_attendees WHERE event_id = ? ORDER BY id",
        (event["id"],),
    ).fetchall()
    others = []
    for email, _name, is_self in rows:
        if is_self or not email or is_owner_address(email, pattern):
            continue
        if email.lower() not in others:
            others.append(email.lower())
    if not others:
        return False, "no one but the owner", []
    if len(others) > MAX_ATTENDEES:
        return False, f"{len(others)} attendees: not a meeting to prepare", []
    own_domains = {_domain(a) for a in owner_addresses(pattern)[0]}
    external = [a for a in others if own_domains and _domain(a) not in own_domains]
    if event.get("is_recurring") and not external:
        return False, "a recurring internal series", []
    since = _stamp(now - timedelta(days=MET_DAYS))
    unmet = [
        a
        for a in others
        if not conn.execute(
            """SELECT 1 FROM event_attendees ea JOIN calendar_events ce ON ce.id = ea.event_id
               WHERE LOWER(ea.email) = ? AND ce.id != ? AND ce.is_cancelled = 0
                 AND ce.start_at >= ? AND ce.start_at < ? LIMIT 1""",
            (a, event["id"], since, _stamp(now)),
        ).fetchone()
    ]
    if external:
        first = external + [a for a in others if a not in external]
        return True, f"{len(external)} external attendee(s)", first
    if unmet:
        first = unmet + [a for a in others if a not in unmet]
        return True, f"{len(unmet)} attendee(s) not met in {MET_DAYS} days", first
    return False, "only internal attendees met recently", []


def _sent_before(log: Path) -> tuple[set[tuple[int, str]], dict[str, int]]:
    """The (meeting id, start) pairs already briefed, and the briefs sent per UTC day."""
    briefed: set[tuple[int, str]] = set()
    per_day: dict[str, int] = {}
    try:
        lines = log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return briefed, per_day
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("sent"):
            briefed.add((record.get("event_id"), record.get("start_at")))
            day = str(record.get("at", ""))[:10]
            per_day[day] = per_day.get(day, 0) + 1
    return briefed, per_day


def _append(log: Path, record: dict) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_prompt(conn, event: dict, people: list[str]) -> str:
    """The user turn: the meeting, then meeting_prep's dossiers, fenced as third-party text."""
    from src.extract.untrusted import fence
    from src.mcp_budget import budget_response
    from src.store.calendar_query import athens_time
    from src.store.query import meeting_prep

    prep = meeting_prep(conn, people[:DOSSIER_PEOPLE], days=365)
    prep = budget_response(prep, DOSSIER_CHARS)
    meeting = {
        "subject": event.get("subject"),
        "starts": athens_time(event.get("start_at")),
        "location": event.get("location"),
        "organiser": event.get("organizer_name") or event.get("organizer_email"),
        "purpose": event.get("body_summary"),
        "attendees_covered": people[:DOSSIER_PEOPLE],
    }
    material = json.dumps({"meeting": meeting, "prep": prep}, ensure_ascii=False, default=str)
    return "The meeting and what the store holds on its attendees:\n\n" + fence(material)


def ask(user: str):
    """One plain-text call through claude_extract.complete(), logged under this site."""
    from src.extract.claude_extract import _response_text, complete
    from src.pipeline.topic_pages import Reply, _cost

    response = complete(
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": EFFORT},
    )
    input_tokens, output_tokens, cost = _cost(response)
    stop = getattr(response, "stop_reason", None)
    text = "" if stop == "refusal" else _response_text(response)
    return Reply(text, getattr(response, "model", None), input_tokens, output_tokens, cost, stop)


def _html(subject: str, starts: str | None, text: str, reason: str, until: str) -> str:
    body = html.escape(text.strip()).replace("\n", "<br>")
    note = html.escape(f"Brief pilot ({reason}); it ends by itself, {until}.")
    return (
        '<div style="font-family: Aptos, Calibri, sans-serif; font-size: 12pt; color: #404040;">'
        f"<b>{html.escape(subject or '(no subject)')}</b>, {html.escape(starts or '')}<br><br>"
        f"{body}<br><br>"
        f'<span style="font-size: 9pt; color: #808080;">{note}</span></div>'
    )


def send_to_owner(to: str, subject: str, html_body: str) -> bool:
    """Mail `to` through outlook-cli, the way scripts/health_check.py send_email does."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".html", prefix="sb-brief-", delete=False)
    try:
        tmp.write(html_body)
        tmp.close()
        env = os.environ.copy()
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
                to,
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
            print(f"  outlook-cli exit {result.returncode}", file=sys.stderr)
        return result.returncode == 0
    except Exception as e:
        print(f"  send failed: {type(e).__name__}", file=sys.stderr)
        return False
    finally:
        os.unlink(tmp.name)


def run_briefs(
    conn,
    *,
    now: datetime | None = None,
    environ: Mapping[str, str] = os.environ,
    pattern: str | None = None,
    dry_run: bool = False,
    send: Callable[[str, str, str], bool] = send_to_owner,
    log: Path | None = None,
    say: Callable[[str], None] = print,
    clock: Callable[[], float] = time.time,
) -> dict:
    """One pass of the pilot. Returns counts: upcoming, qualified, sent, skipped, failed,
    deferred (due, but the unit's LLM deadline had passed)."""
    from src import config
    from src.llm_policy import resolve_deadline
    from src.store.calendar_query import athens_time

    now = now or datetime.now(UTC)
    pattern = config.USER_EMAIL_PATTERN if pattern is None else pattern
    log = log or log_path()
    stats: dict[str, Any] = {"upcoming": 0, "qualified": 0, "sent": 0, "skipped": 0}
    stats.update({"failed": 0, "deferred": 0, "cost_usd": 0.0})
    active, why = pilot_state(environ, now.date())
    if not active:
        say(f"briefs: {why}")
        stats["state"] = why
        return stats
    to = owner_recipient(environ, pattern)
    if to is None and not dry_run:
        say("briefs: HEALTH_EMAIL_TO is not one of the owner's addresses; nothing sent")
        stats["state"] = "no owner address"
        return stats
    until = environ.get("BRAIN_BRIEF_PILOT_UNTIL", "").strip()
    # The unit's deadline, which src/llm_deadline exports for sb-briefs as for the other
    # units, read the way every caller reads it: no brief starts once it has passed. It
    # holds back one worst-case call and the shutdown grace from TimeoutStartSec=300, so
    # a brief begun just before it still ends, mail included, before systemd stops the
    # unit. Unset (a hand run) it is the policy's own default budget.
    deadline = resolve_deadline(clock(), environ)
    briefed, per_day = _sent_before(log)
    events = upcoming(conn, now)
    stats["upcoming"] = len(events)
    for event in events:
        ok, reason, people = qualifies(conn, event, pattern, now)
        if not ok:
            stats["skipped"] += 1
            say(f"  meeting {event['id']}: no brief ({reason})")
            continue
        stats["qualified"] += 1
        if (event["id"], event["start_at"]) in briefed:
            stats["skipped"] += 1
            say(f"  meeting {event['id']}: briefed already")
            continue
        if per_day.get(now.strftime("%Y-%m-%d"), 0) >= MAX_PER_DAY:
            stats["skipped"] += 1
            say(f"  meeting {event['id']}: {MAX_PER_DAY} briefs sent today already")
            continue
        if dry_run:
            say(f"  meeting {event['id']}: would brief ({reason}); dry run, no call")
            continue
        record = {
            "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event_id": event["id"],
            "start_at": event["start_at"],
            "reason": reason,
            "attendees": len(people),
        }
        if clock() >= deadline:
            # Logged unsent, so the pilot's record shows the brief that was due.
            stats["deferred"] += 1
            _append(log, {**record, "sent": False, "error": "deadline"})
            say(f"  meeting {event['id']}: no brief, the unit's LLM deadline has passed")
            continue
        try:
            reply = ask(build_prompt(conn, event, people))
            if not reply.text.strip() or reply.stop_reason in ("refusal", "max_tokens"):
                raise ValueError(f"no usable reply ({reply.stop_reason})")
        except Exception as e:
            stats["failed"] += 1
            _append(log, {**record, "sent": False, "error": type(e).__name__})
            say(f"  meeting {event['id']}: the call failed ({type(e).__name__})")
            continue
        starts = athens_time(event["start_at"])
        subject = f"brief: {event.get('subject') or 'meeting'}"
        page = _html(event.get("subject") or "", starts, reply.text, reason, until)
        sent = bool(to) and send(to or "", subject, page)
        _append(
            log,
            {
                **record,
                "sent": sent,
                "model": reply.model,
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
                "cost_usd": reply.cost_usd,
            },
        )
        stats["cost_usd"] += reply.cost_usd or 0.0
        if sent:
            stats["sent"] += 1
            per_day[now.strftime("%Y-%m-%d")] = per_day.get(now.strftime("%Y-%m-%d"), 0) + 1
            say(f"  meeting {event['id']}: brief sent ({reason}), ${reply.cost_usd or 0:.3f}")
        else:
            stats["failed"] += 1
            say(f"  meeting {event['id']}: the mail failed")
    stats["cost_usd"] = round(stats["cost_usd"], 4)
    return stats


def run(args) -> int:
    """`brain briefs`: one pass of the pilot; --dry-run lists what it would brief."""
    from src import config
    from src.store.sql_readonly import connect_read_only

    if not args.dry_run and config.is_replica():
        print(config.replica_refusal("'briefs'"), file=sys.stderr)
        return 2
    conn = connect_read_only(args.db, immutable=config.is_pulled_copy())
    try:
        stats = run_briefs(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    print("briefs: " + ", ".join(f"{k} {v}" for k, v in stats.items() if k != "state"))
    return 1 if stats["failed"] else 0
