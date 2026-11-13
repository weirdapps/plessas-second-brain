"""Teams meeting transcripts: fetched on the Mac, ingested on the producer as documents.

Teams keeps the transcript of a transcribed meeting beside its recording, in the OneDrive of
the meeting's organizer (the person who started it, for a 1:1 call), as a media item of the
recording's file; a meeting transcribed without a recording leaves a file named
'... Meeting Transcript.mp4' that holds the transcript alone. Everyone the meeting was shared
with can read it. Graph's transcript API needs OnlineMeetingTranscript.Read.All, which the
owner's app registration lacks; the file route needs only what it holds (Files.ReadWrite.All,
Sites.Read.All), so it is the one used (probed 2026-11-13: 18 of 21 of the owner's own
recordings and other organizers' meetings alike).

The Mac (fetch_new, `brain meetings-fetch`), hourly through
scripts/wrappers/launchd/push-meeting-transcripts-to-vps.sh:
  1. Microsoft Search (Graph /search/query, driveItem) lists every recording and transcript
     file the owner can open, in his OneDrive and other people's: QUERIES.
  2. For each one not settled before (the state file), the SharePoint media API
     (/_api/v2.1/drives/{d}/items/{i}/media/transcripts) lists its transcripts, and the
     default one is read as JSON (streamContent?format=json): speaker-labelled lines.
  3. render() turns it into one line per speaker turn, '[hh:mm:ss] NAME: text', and the
     record (RECORD_SCHEMA) is written to the outbox, which the wrapper rsyncs to the
     producer's inbox.
  An item settles once written, once its transcript proves too short to carry anything
  (MIN_WORDS: a Greek call transcribed as English comes out as a few words of noise), or
  once it is older than GIVE_UP without a transcript; a younger one is asked again next run,
  since Teams posts the recording before the transcript is ready. A failure is counted and
  asked again.

  Tokens: the refresh token in the MSAL token cache of the Mac's M365 command line (the file
  BRAIN_M365_TOKEN_CACHE names, set in the owner-only ~/.second-brain/env, which the push
  wrapper reads) is exchanged for an access token per audience (Graph, the SharePoint host),
  read-only: the cache is never written, so that CLI keeps its own session. No cache, or a
  refused refresh, is AuthRequired (exit 2 from the CLI): log in to M365 again on the Mac.

The producer (ingest_inbox, `brain meetings-ingest`), in the nightly sb-attachment-pass
before Phase 2: each record becomes a text document through ingest_text_document (an
'External' email anchor, sender SENDER_ADDRESS, class 'document', dated by the meeting's
start), so Phase 2 summarises it and takes its decisions, action items and facts, and the
embedding pass indexes it, as for any document. The message id is the hash of the record's
key (drive, item, transcript), so a record pushed twice is held, not stored again.

Retention: none (owner decision 2026-11-13), as for Teams chats; `brain maint forget` is
manual and covers these rows as it covers every document.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

GRAPH = "https://graph.microsoft.com"
TOKEN_CACHE_ENV = "BRAIN_M365_TOKEN_CACHE"
# The recording files Teams writes, and the transcript-only ones.
QUERIES = ('filename:"Meeting Recording"', 'filename:"Meeting Transcript"')
RECORD_SCHEMA = 1
# Fewer words than this carry nothing to extract: a call transcribed in the wrong language.
MIN_WORDS = 40
# A recording this old with no transcript will not get one.
GIVE_UP = timedelta(days=2)
SOURCE = "teams-transcript"
SENDER_ADDRESS = f"{SOURCE}@documents.local"
SEARCH_PAGE = 200
TIMEOUT_S = 90

_STAMP = re.compile(r"-\d{8}_\d{6}(?:UTC)?-Meeting (?:Recording|Transcript)$", re.IGNORECASE)


class TranscriptError(RuntimeError):
    """One request failed; the item is asked again next run, unless it is GONE and old."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# Answers that will not change: the transcript was deleted (404, 410), or the file is held
# locked (423; seen on 2021 files under a retention hold, 2026-11-13).
GONE = (404, 410, 423)


class AuthRequired(RuntimeError):
    """No usable M365 session on this Mac: log in to M365 again."""


@dataclass(frozen=True)
class Item:
    drive_id: str
    item_id: str
    name: str
    created_at: str
    web_url: str
    created_by: str | None

    @property
    def key(self) -> str:
        return f"{self.drive_id}:{self.item_id}"

    @property
    def site(self) -> str:
        """The OneDrive's site URL ('https://host/personal/user'), which the media API needs."""
        parts = urllib.parse.urlsplit(self.web_url)
        segs = parts.path.split("/")
        base = "/".join(segs[:3]) if len(segs) > 2 and segs[1] in ("personal", "sites") else ""
        return f"{parts.scheme}://{parts.netloc}{base}"


def title_of(name: str) -> str:
    """The meeting's title from its file name: no extension, stamp or kind, spaces folded."""
    stem = name[:-4] if name.lower().endswith(".mp4") else name
    return " ".join(_STAMP.sub("", stem).split())


def _seconds(offset: str | None) -> int:
    if not offset:
        return 0
    h, m, s = (offset.split(":") + ["0", "0"])[:3]
    try:
        return int(h) * 3600 + int(m) * 60 + int(float(s))
    except ValueError:
        return 0


def _clock(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def render(doc: dict) -> tuple[str, dict]:
    """The transcript as text, one line per speaker turn, and what it says of itself."""
    turns: list[list] = []
    words = 0
    end = 0
    speakers: set[str] = set()
    for entry in doc.get("entries") or []:
        text = " ".join(str(entry.get("text") or "").split())
        if not text:
            continue
        who = str(entry.get("speakerDisplayName") or "Unknown").strip() or "Unknown"
        speakers.add(who)
        words += len(text.split())
        end = max(end, _seconds(entry.get("endOffset")))
        if turns and turns[-1][1] == who:
            turns[-1][2].append(text)
        else:
            turns.append([_seconds(entry.get("startOffset")), who, [text]])
    text = "\n".join(f"[{_clock(at)}] {who}: {' '.join(parts)}" for at, who, parts in turns)
    meta = {
        "speakers": sorted(speakers),
        "entries": len(doc.get("entries") or []),
        "duration_s": end,
        "words": words,
    }
    return text, meta


# --- tokens and HTTP ---------------------------------------------------------


def _post_form(url: str, form: dict) -> dict:
    body = urllib.parse.urlencode(form).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=TIMEOUT_S) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
        except ValueError:
            err = {}
        raise AuthRequired(
            f"token refresh refused ({err.get('error', e.code)}): log in to M365 again"
        ) from e


class TokenSource:
    """Access tokens per audience from the M365 CLI's refresh token; the cache is only read."""

    def __init__(self, cache: Path | None = None, post: Callable[[str, dict], dict] = _post_form):
        named = cache or os.environ.get(TOKEN_CACHE_ENV)
        self.cache = Path(named).expanduser() if named else None
        self.post = post
        self._tokens: dict[str, tuple[str, float]] = {}

    def get(self, resource: str) -> str:
        held = self._tokens.get(resource)
        if held and held[1] > time.time() + 120:
            return held[0]
        if self.cache is None:
            raise AuthRequired(f"{TOKEN_CACHE_ENV} is not set: name the M365 CLI's token cache")
        try:
            data = json.loads(self.cache.read_text())
            refresh = next(iter(data["RefreshToken"].values()))
        except (OSError, ValueError, KeyError, StopIteration) as e:
            raise AuthRequired(f"no M365 session in {self.cache}: log in to M365 again") from e
        account: dict = next(iter((data.get("Account") or {}).values()), {})
        tenant = account.get("realm") or str(refresh.get("home_account_id", "")).split(".")[-1]
        answer = self.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            {
                "client_id": refresh["client_id"],
                "grant_type": "refresh_token",
                "refresh_token": refresh["secret"],
                "scope": f"{resource}/.default offline_access",
            },
        )
        token = answer["access_token"]
        self._tokens[resource] = (token, time.time() + float(answer.get("expires_in", 3000)))
        return token


class Api:
    """Graph search and the SharePoint media API, over urllib."""

    def __init__(self, tokens: TokenSource):
        self.tokens = tokens

    def _call(self, url: str, resource: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.tokens.get(resource)}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise TranscriptError(
                f"HTTP {e.code} from {urllib.parse.urlsplit(url).path[:80]}", e.code
            ) from e
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise TranscriptError(f"{type(e).__name__}: {e}") from e

    def search(self, query: str) -> list[dict]:
        hits: list[dict] = []
        start = 0
        while True:
            answer = self._call(
                f"{GRAPH}/v1.0/search/query",
                GRAPH,
                {
                    "requests": [
                        {
                            "entityTypes": ["driveItem"],
                            "query": {"queryString": query},
                            "from": start,
                            "size": SEARCH_PAGE,
                            "fields": [
                                "id",
                                "name",
                                "createdDateTime",
                                "webUrl",
                                "parentReference",
                                "createdBy",
                            ],
                        }
                    ]
                },
            )
            container = answer["value"][0]["hitsContainers"][0]
            page = container.get("hits") or []
            hits += page
            if not container.get("moreResultsAvailable") or not page:
                return hits
            start += len(page)

    def _media(self, item: Item) -> tuple[str, str]:
        host = urllib.parse.urlsplit(item.site)
        return (
            f"{item.site}/_api/v2.1/drives/{item.drive_id}/items/{item.item_id}/media/transcripts",
            f"{host.scheme}://{host.netloc}",
        )

    def list_transcripts(self, item: Item) -> list[dict]:
        url, resource = self._media(item)
        return self._call(url, resource).get("value") or []

    def fetch(self, item: Item, transcript_id: str) -> dict:
        url, resource = self._media(item)
        return self._call(f"{url}/{transcript_id}/streamContent?format=json", resource)


# --- the Mac side --------------------------------------------------------------


def _item(hit: dict) -> Item | None:
    """The file a search hit names. Its own id, or the hit's id, which is the same; never the
    parentReference id, which is the Recordings folder (live run of 2026-11-13: 172 hits
    collapsed into 36 folders, every transcript call failing)."""
    res = hit.get("resource") or {}
    parent = res.get("parentReference") or {}
    drive, item_id = parent.get("driveId"), res.get("id") or hit.get("hitId")
    name = res.get("name") or ""
    if not (drive and item_id and name.lower().endswith(".mp4")):
        return None
    by = ((res.get("createdBy") or {}).get("user") or {}).get("displayName")
    if by == "SharePoint App":  # Teams writes the file as the service, so it names no one
        by = None
    return Item(drive, item_id, name, res.get("createdDateTime") or "", res.get("webUrl") or "", by)


def _parse_time(value: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _load_state(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=0, sort_keys=True))
    tmp.replace(path)


def _pick(transcripts: list[dict]) -> dict:
    return next((t for t in transcripts if t.get("isDefault")), transcripts[0])


def exit_code(counts: dict) -> int:
    """0 when nothing failed; 1 when some items failed but others moved; 4 (temporary, no
    success stamped) when every attempt failed and nothing settled: on 2026-11-13 a DNS
    outage failed all 105 items and the run still counted as a success."""
    if not counts.get("failed"):
        return 0
    moved = sum(counts.get(k, 0) for k in ("written", "too_short", "no_transcript", "unavailable"))
    return 1 if moved else 4


def fetch_new(api, outbox: Path, state_path: Path, now: datetime | None = None) -> dict:
    """Write a record for each transcript not settled before; return the counts."""
    now = now or datetime.now(UTC)
    outbox = Path(outbox)
    outbox.mkdir(parents=True, exist_ok=True)
    state = _load_state(state_path)
    items: dict[str, Item] = {}
    for query in QUERIES:
        for hit in api.search(query):
            item = _item(hit)
            if item:
                items.setdefault(item.key, item)
    counts: dict[str, int] = dict.fromkeys(
        ("written", "too_short", "no_transcript", "unavailable", "pending", "failed"), 0
    )
    counts["found"] = len(items)
    for key, item in sorted(items.items(), key=lambda kv: kv[1].created_at):
        if key in state:
            continue
        created = _parse_time(item.created_at)
        young = bool(created and now - created < GIVE_UP)
        try:
            transcripts = api.list_transcripts(item)
            if not transcripts:
                if young:
                    counts["pending"] += 1
                else:
                    state[key] = "no_transcript"
                    counts["no_transcript"] += 1
                continue
            chosen = _pick(transcripts)
            text, meta = render(api.fetch(item, chosen["id"]))
        except TranscriptError as e:
            if e.status in GONE and not young:
                state[key] = "unavailable"
                counts["unavailable"] += 1
                continue
            counts["failed"] += 1
            print(f"  FAIL {item.name[:70]}: {e}", file=sys.stderr)
            continue
        if meta["words"] < MIN_WORDS:
            state[key] = "too_short"
            counts["too_short"] += 1
            continue
        record_key = f"{key}:{chosen['id']}"
        record = {
            "schema": RECORD_SCHEMA,
            "key": record_key,
            "title": title_of(item.name),
            "file_name": item.name,
            "organizer": item.created_by,
            "started_at": item.created_at,
            "language": chosen.get("languageTag"),
            "web_url": item.web_url,
            "text": text,
            **meta,
        }
        name = hashlib.sha256(record_key.encode()).hexdigest()[:24] + ".json"
        tmp = outbox / (name + ".part")
        tmp.write_text(json.dumps(record, ensure_ascii=False))
        tmp.replace(outbox / name)
        state[key] = "written"
        counts["written"] += 1
    _save_state(state_path, state)
    return counts


# --- the producer side -----------------------------------------------------------


# Header fields come from file names and display names anyone can set: one line each, capped.
FIELD_CHARS = 200
MAX_SPEAKERS = 50


def _field(value, default: str = "") -> str:
    return " ".join(str(value or default).split())[:FIELD_CHARS]


def _document_text(rec: dict) -> str:
    minutes = int(rec.get("duration_s") or 0) // 60
    speakers = [_field(s) for s in (rec.get("speakers") or [])[:MAX_SPEAKERS]]
    head = [
        f"Teams meeting transcript: {_field(rec.get('title'))}",
        f"Started: {_field(rec.get('started_at'))}",
        f"Duration: {minutes} min",
        f"Recording owner: {_field(rec.get('organizer'), 'unknown')}",
        f"Speakers: {', '.join(speakers)}",
        f"Language: {_field(rec.get('language'))}",
    ]
    return "\n".join(head) + "\n\n" + str(rec.get("text") or "")


def ingest_one(conn: sqlite3.Connection, rec: dict) -> dict:
    """Store one record as a text document; skipped when its key is held."""
    from src.extract.attachment_pipeline import ingest_text_document

    if rec.get("schema") != RECORD_SCHEMA or not rec.get("key") or not rec.get("text"):
        raise ValueError("not a transcript record")
    title = _field(rec.get("title"), "Teams meeting") or "Teams meeting"
    return ingest_text_document(
        conn,
        source=SOURCE,
        key=rec["key"],
        filename=f"{title}.txt",
        mime_type="text/plain",
        text=_document_text(rec),
        sha256=hashlib.sha256(rec["key"].encode()).hexdigest(),
        method=SOURCE,
        status="extracted",
        error=None,
        subject=f"[Meeting transcript] {title}",
        sender_name=_field(rec.get("organizer"), "Teams meeting"),
        date=rec.get("started_at") or datetime.now(UTC).isoformat(),
    )


def ingest_inbox(conn: sqlite3.Connection, inbox: Path, remove: bool = False) -> dict:
    """Store every record in the inbox; a stored or held one leaves it when remove is set."""
    counts = {"stored": 0, "held": 0, "failed": 0}
    for path in sorted(Path(inbox).glob("*.json")):
        try:
            outcome = ingest_one(conn, json.loads(path.read_text()))
        except (OSError, ValueError, KeyError, sqlite3.Error) as e:
            counts["failed"] += 1
            print(f"  FAIL {path.name}: {type(e).__name__}: {e}")
            continue
        counts["held" if outcome.get("skipped") else "stored"] += 1
        if remove:
            path.unlink(missing_ok=True)
    return counts
