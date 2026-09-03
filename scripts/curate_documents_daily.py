"""
Daily incremental curation: classify new second-brain attachments via Vertex AI
Claude, place into ~/Documents/ sub-folders, refresh per-folder READMEs
and top-level INDEX.md files.

Designed to be idempotent and resume-safe. State lives in
~/.second-brain/curate-state.json (processed attachment IDs, candidates parked
by a full folder, and cached folder summaries). Run via launchd at 05:07
Athens local time.

Caps:
  --max-new <N>  process at most N new candidates per run (default: 30)
  --refresh-index  force re-summarize ALL folders (default: only folders that
                   gained files this run)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

# Make src.extract.* importable when run as a standalone script (scripts/ is sys.path[0]).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# _response_text and MAX_OUTPUT_TOKENS are the repo's single implementations of
# "first block that has text" and "how much output to allow". Imported rather
# than copied so this script cannot drift away from them again the way it did
# while it lived untracked on the VPS.
from src.extract.claude_extract import MAX_OUTPUT_TOKENS, _response_text  # noqa: E402
from src.extract.vertex_fallback import create_with_refusal_fallback  # noqa: E402
from src.llm_deadline import install_llm_deadline_for_this_process  # noqa: E402

# --- Paths ---------------------------------------------------------------
DB = Path.home() / "SourceCode/plessas-second-brain/data/brain.db"
DOCS = Path.home() / "Documents"
STATE = Path.home() / ".second-brain/curate-state.json"
LOG_DIR = Path.home() / ".second-brain/logs"
LOG_FILE = LOG_DIR / "curate-docs.log"

PATH_REWRITES = [
    (
        str(Path.home()) + "/SourceCode/claude/second-brain/",
        str(Path.home()) + "/SourceCode/plessas-second-brain/",
    ),
]

NOISE_SUBJECT_PATTERNS = [
    r"ημερήσια αναφορά",
    r"εβδομαδιαία reports?",
    r"daily\s+report",
    r"weekly\s+report",
    r"loans applications dashboard",
    r"τεύχος\s+\d+",
    r"newsletter",
    r"\bτυπετ\b",
    r"phishing.*simulation",
    r"επικαιροποιηση προσωπικων στοιχειων",
]

# How many runs a cap-blocked candidate may come back for before it is left
# parked for good.  A folder the operator never prunes would otherwise cost one
# classification call per candidate per run, twice a day, forever.
MAX_DEFER_ATTEMPTS = 5

# --- Org-specific taxonomy (private) ------------------------------------
# MANAGED_FOLDERS, SOFT_CAPS, TAXONOMY, CLASSIFY_PROMPT, SUMMARIZE_PROMPT,
# and NOISE_SENDERS are intentionally absent from this public file.
# They live in a private configuration file that must be present at runtime.
#
# Canonical location: a private config repo (path intentionally not published)
# (synced from the private claude-config repo; auto-deployed to the VPS by
# repo-autoupdate.timer at 05:43 UTC).  On a Mac with the standard claude-config
# symlink setup, ~/.claude/private/curate-taxonomy.py resolves to the same file.
#
# If the file is missing the script exits loud — there is no silent fallback.
_TAXONOMY_FILE = Path.home() / "SourceCode" / "claude-config" / "private" / "curate-taxonomy.py"

if not _TAXONOMY_FILE.exists():
    sys.exit(
        f"ERROR: Private taxonomy configuration not found: {_TAXONOMY_FILE}\n"
        "This file must define MANAGED_FOLDERS, SOFT_CAPS, TAXONOMY,\n"
        "CLASSIFY_PROMPT, SUMMARIZE_PROMPT and NOISE_SENDERS.\n"
        "It lives in claude-config/private/ (private repo) and is never\n"
        "committed to the public plessas-second-brain repository."
    )

_spec = importlib.util.spec_from_file_location("_curate_taxonomy", _TAXONOMY_FILE)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]
MANAGED_FOLDERS: list[str] = _mod.MANAGED_FOLDERS
SOFT_CAPS: dict[str, int] = _mod.SOFT_CAPS
TAXONOMY: str = _mod.TAXONOMY
CLASSIFY_PROMPT: str = _mod.CLASSIFY_PROMPT
SUMMARIZE_PROMPT: str = _mod.SUMMARIZE_PROMPT
NOISE_SENDERS: set[str] = _mod.NOISE_SENDERS
# Top-level document areas that this script writes INDEX.md files for.
# Stored in the taxonomy file so the same script works with different folder schemes.
AREAS: list[str] = _mod.AREAS
del _spec, _mod


# --- Helpers -------------------------------------------------------------
def log(msg: str):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(line)


def resolve_path(p: str | None) -> Path | None:
    if not p:
        return None
    for old, new in PATH_REWRITES:
        if p.startswith(old):
            p = new + p[len(old) :]
            break
    pp = Path(p)
    return pp if pp.exists() else None


def slugify(s: str, maxlen: int = 60) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^\w\s.-]", "", s).strip().lower()
    s = re.sub(r"[\s\-_]+", "_", s)
    s = re.sub(r"\.+", ".", s)
    if len(s) > maxlen:
        if "." in s[-8:]:
            stem, ext = s.rsplit(".", 1)
            s = stem[: maxlen - len(ext) - 1] + "." + ext
        else:
            s = s[:maxlen]
    return s.strip("._")


def fmt_date(date_received: str) -> str:
    try:
        d = datetime.fromisoformat(date_received.replace("Z", "+00:00"))
        return d.strftime("%Y%m%d%H%M")
    except Exception:
        return "200001010000"


def is_subject_noise(text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in NOISE_SUBJECT_PATTERNS)


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {
        "last_run": None,
        "processed_ids": [],
        "copied": [],  # {id, src, dst, category, confidence, classified_at}
        "deferred": {},  # id -> {folder, attempts, last_attempt}; see defer_candidate
        "folder_summaries": {},
    }


def save_state(state: dict):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def get_client():
    from anthropic import AnthropicVertex

    project_id = os.environ.get("VERTEX_SDK_PROJECT") or os.environ["ANTHROPIC_VERTEX_PROJECT_ID"]
    region = os.environ.get("VERTEX_SDK_REGION") or os.environ.get(
        "CLOUD_ML_REGION", "europe-west1"
    )
    model = os.environ.get("VERTEX_MODEL_EXTRACT", "claude-sonnet-4-6")
    return AnthropicVertex(project_id=project_id, region=region, timeout=120.0), model


def classify_one(client, model, c: dict) -> dict:
    prompt = CLASSIFY_PROMPT.format(
        taxonomy=TAXONOMY,
        filename=c["filename"],
        subject=c["subject"] or "(none)",
        sender=c["sender"] or "(unknown)",
        date=c["date"],
        size_mb=round(c["file_size"] / 1024 / 1024, 2),
        summary=c["summary"][:1500],
    )
    response = create_with_refusal_fallback(
        client,
        model=model,
        # Extended thinking draws on the same budget as the answer, so the old
        # 300 was spent entirely on thinking and the response carried no text
        # block at all (stop_reason=max_tokens, thinking_tokens=300).
        max_tokens=MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    text = _response_text(response).strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[0].startswith("```") else lines[:-1])
    try:
        return json.loads(text)
    except Exception:
        return {"folder": "SKIP", "confidence": "low", "reasoning": "parse error"}


def summarize_folder(client, model, folder: str, readme_text: str) -> dict:
    truncated = readme_text[:18000]
    response = create_with_refusal_fallback(
        client,
        model=model,
        max_tokens=MAX_OUTPUT_TOKENS,  # 600 left no room after thinking either
        messages=[
            {
                "role": "user",
                "content": SUMMARIZE_PROMPT.format(folder=folder, readme=truncated),
            }
        ],
    )
    text = _response_text(response).strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[0].startswith("```") else lines[:-1])
    try:
        return json.loads(text)
    except Exception as e:
        return {"error": str(e)}


def query_new_candidates(
    processed_ids: set[int], limit: int, deferred: dict | None = None
) -> list[dict]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    # Static parameterized query; filter against processed_ids in Python.
    # Avoids dynamic IN-clause string building entirely.
    #
    # THE `mailbox_name = 'External' AND message_id < 0` CLAUSE IS THE THING
    # THAT STOPS THIS JOB EATING ITS OWN OUTPUT.  We copy classified
    # attachments into ~/Documents/<folder>/; sb-reverse-ingest then scans
    # those same roots and loads every file it finds back into the store
    # through ingest_document(), which writes a synthetic anchor email with
    # mailbox_name='External' and a NEGATIVE message_id (a content hash; see
    # _file_to_message_id in src/extract/attachment_pipeline.py).  Those rows
    # satisfied every clause below, so curate re-offered its own placements to
    # itself, re-classified them and re-placed them under a second "[Document]"
    # prefix.  Measured 2026-08-30: 41% of the tree was curate's own re-placed
    # output and 553 files carried a doubled or tripled prefix, which is what
    # drove all 17 folders into their soft caps in the first place.
    #
    # The pair is exact, not a heuristic.  ingest_document is the only writer
    # of mailbox_name='External' and it always writes a negative integer id.
    # Real mail cannot match it: Outlook message_ids are TEXT ('AAMk...'), and
    # SQLite sorts every TEXT value above every INTEGER, so `< 0` is false for
    # them.  News rows are TEXT ids under mailbox_name='News'.  COALESCE keeps
    # a NULL mailbox_name out of the NOT, where it would otherwise turn the
    # whole predicate NULL and silently drop the row.
    rows = conn.execute(
        "SELECT a.id, a.filename, a.file_path, a.file_size, a.mime_type, "
        "       e.subject, e.sender_address, e.date_received, ac.summary "
        "FROM attachments a "
        "JOIN attachment_content ac ON ac.attachment_id = a.id "
        "JOIN emails e ON e.id = a.email_id "
        "WHERE a.is_inline = 0 "
        "  AND a.file_size > 200000 "
        "  AND ac.llm_status = 'extracted' "
        "  AND ac.summary IS NOT NULL "
        "  AND LENGTH(ac.summary) > 300 "
        "  AND NOT (COALESCE(e.mailbox_name, '') = 'External' AND e.message_id < 0) "
        "  AND (a.mime_type LIKE '%pdf%' OR a.mime_type LIKE '%presentation%' "
        "       OR a.mime_type LIKE '%spreadsheet%' OR a.mime_type LIKE '%word%') "
        "ORDER BY a.id DESC"
    ).fetchall()

    deferred = deferred or {}
    out = []
    seen_filenames = set()
    for r in rows:
        if r["id"] in processed_ids:
            continue
        parked = deferred.get(str(r["id"]))
        if parked is not None and not deferred_is_ready(parked):
            continue
        if r["sender_address"] in NOISE_SENDERS:
            continue
        if is_subject_noise(" ".join(filter(None, [r["subject"] or "", r["filename"]]))):
            continue
        resolved = resolve_path(r["file_path"])
        if resolved is None:
            continue
        base = re.sub(r"_v?\d+(\.\d+)?\b", "", r["filename"].lower())
        base = re.sub(r"\(\d+\)", "", base).strip()
        if base in seen_filenames:
            continue
        seen_filenames.add(base)
        out.append(
            {
                "id": r["id"],
                "filename": r["filename"],
                "file_path": str(resolved),
                "file_size": r["file_size"],
                "subject": r["subject"] or "",
                "sender": r["sender_address"] or "",
                "date": r["date_received"],
                "summary": r["summary"],
            }
        )
        if len(out) >= limit:
            break
    conn.close()
    return out


def folder_size(folder: str) -> tuple[int, float]:
    p = DOCS / folder
    if not p.exists():
        return (0, 0.0)
    files = [f for f in p.iterdir() if f.is_file() and f.name not in {"README.md", ".DS_Store"}]
    return (len(files), round(sum(f.stat().st_size for f in files) / 1024 / 1024, 1))


def defer_candidate(deferred: dict, candidate_id: int, folder: str):
    """Park a candidate the soft cap turned away, so it is neither lost nor burned.

    Deliberately a SEPARATE record from processed_ids, and deliberately not
    just "add to processed later".  A full folder is a fact about the
    DESTINATION and can change the moment the operator prunes it; a
    low-confidence verdict is a fact about the DOCUMENT and never will, which
    is why only this one rejection defers and every other still records the
    candidate as processed.  Leaving cap-blocked candidates in the main stream
    instead is the other wrong answer: candidates come back id DESC, so the
    same blocked ids would be re-offered first on every run forever, the
    thousands behind them would never be reached, and each run would spend a
    classification call re-deciding items it cannot place.
    """
    entry = deferred.get(str(candidate_id), {})
    deferred[str(candidate_id)] = {
        "folder": folder,
        "attempts": entry.get("attempts", 0) + 1,
        "last_attempt": datetime.now().isoformat(),
    }


def deferred_is_ready(entry: dict) -> bool:
    """True when a parked candidate may be offered again: room, and retries left."""
    if entry.get("attempts", 0) >= MAX_DEFER_ATTEMPTS:
        return False
    folder = entry.get("folder")
    if not folder:
        return False
    cnt, _ = folder_size(folder)
    return cnt < SOFT_CAPS.get(folder, 50)


def write_readme(folder: str, conn):
    target_dir = DOCS / folder
    if not target_dir.exists():
        return
    all_files = sorted(
        f for f in target_dir.iterdir() if f.is_file() and f.name not in {"README.md", ".DS_Store"}
    )
    rows = []
    for f in all_files:
        row = conn.execute(
            """
            SELECT e.subject, e.sender_address, ac.summary
            FROM attachments a
            LEFT JOIN emails e ON e.id = a.email_id
            LEFT JOIN attachment_content ac ON ac.attachment_id = a.id
            WHERE a.filename = ?  LIMIT 1
        """,
            (f.name,),
        ).fetchone()
        if row:
            rows.append(
                (
                    f.name,
                    (row[0] or "—").replace("|", "·")[:80],
                    (row[1] or "—").replace("|", "·"),
                    (row[2] or "(no summary)").replace("|", "·").replace("\n", " ")[:180],
                )
            )
        else:
            rows.append((f.name, "—", "—", "(pre-existing, no DB match)"))

    size_mb = sum(f.stat().st_size for f in all_files) / 1024 / 1024
    lines = [
        f"# {folder}",
        "",
        f"_{len(all_files)} files ({size_mb:.0f} MB) — refreshed {datetime.now().strftime('%Y-%m-%d %H:%M')}._",
        "",
        f"## Files ({len(rows)})",
        "",
        "| File | Email subject | From | Summary |",
        "|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| `{r[0]}` | {r[1]} | {r[2]} | {r[3]} |")
    (target_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_index(area: str, summaries: dict):
    """Aggregate cached folder summaries into top-level INDEX.md."""
    folders = [f for f in MANAGED_FOLDERS if f.startswith(area + "/")]
    rows = []
    for f in folders:
        cnt, mb = folder_size(f)
        rows.append({"folder": f, "files": cnt, "mb": mb, **summaries.get(f, {})})

    total_files = sum(r["files"] for r in rows)
    total_mb = sum(r["mb"] for r in rows)
    lines = [
        f"# `~/Documents/{area}/` — Index",
        "",
        f"_{total_files} files, {total_mb:.0f} MB across {len(rows)} folders. "
        f"Refreshed {datetime.now().strftime('%Y-%m-%d %H:%M')}._",
        "",
        "Each folder has a `README.md` with the full file list. Skim **Purpose** "
        "and **Themes** below to decide where to look.",
        "",
        "## Quick navigation",
        "",
        "| Folder | Files | Purpose |",
        "|---|---|---|",
    ]
    for r in rows:
        rel = r["folder"].split("/", 1)[1]
        purpose = (r.get("purpose") or "(empty)").replace("|", "·").replace("\n", " ")[:120]
        lines.append(f"| [`{rel}/`](#{rel.replace('/', '').lower()}) | {r['files']} | {purpose} |")
    lines.append("")
    lines.append("---")
    lines.append("")

    for r in rows:
        rel = r["folder"].split("/", 1)[1]
        lines.append(f"### `{rel}/` &nbsp;·&nbsp; {r['files']} files &nbsp;·&nbsp; {r['mb']} MB")
        lines.append("")
        if r.get("purpose"):
            lines.append(f"**Purpose.** {r['purpose']}")
            lines.append("")
        if r.get("themes"):
            lines.append(f"**Themes.** {', '.join(r['themes'])}")
            lines.append("")
        if r.get("date_range"):
            lines.append(f"**Date range.** {r['date_range']}")
            lines.append("")
        if r.get("key_documents"):
            lines.append("**Key documents.**")
            for kd in r["key_documents"]:
                lines.append(f"- `{kd}`")
            lines.append("")
        if r.get("watchout"):
            lines.append(f"> **Does NOT belong here:** {r['watchout']}")
            lines.append("")
        lines.append(f"_See full file list_: [README.md](./{rel}/README.md)")
        lines.append("")
        lines.append("---")
        lines.append("")
    (DOCS / area / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    # No needs_reauth gate here. That sentinel is an M365 signal, and this
    # script reads brain.db and calls Vertex: it names neither of the M365 CLIs,
    # so a dead mail session cannot affect it. The check used to be here and in
    # sb-curate-docs.sh, both copied from the mail wrappers, and on 2026-09-02
    # they stopped six days of curation because Outlook was down while
    # curation's own SharePoint session was healthy throughout. The real
    # dependency, needs_gcloud_reauth, is still guarded in the wrapper.

    # Install the per-unit LLM deadline before any LLM work, mirroring what
    # src/cli.py does for the nine units that go through that entrypoint.
    # sb-curate-docs is a standalone script, so the hook must live here.
    # The unit is already in _UNIT_TIMEOUT_SECONDS at 1800s.  On a non-systemd
    # host (developer Mac, CI) this is a no-op.  If the unit's TimeoutStartSec
    # cannot fund one worst-case call plus shutdown grace, it raises RuntimeError
    # and exits loud — the same loud refusal that cli.py uses.
    install_llm_deadline_for_this_process()

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--max-new",
        type=int,
        default=30,
        help="Max new candidates per run (default 30)",
    )
    parser.add_argument(
        "--refresh-index",
        action="store_true",
        help="Re-summarize ALL folders (default: only changed ones)",
    )
    args = parser.parse_args()

    state = load_state()
    processed = set(state.get("processed_ids", []))
    deferred = state.get("deferred", {})
    log(
        f"Daily curate start. Processed history: {len(processed)} ids. "
        f"Deferred: {len(deferred)}. Cap: {args.max_new}"
    )

    candidates = query_new_candidates(processed, args.max_new, deferred)
    log(f"New candidates after filter: {len(candidates)}")
    if not candidates and not args.refresh_index:
        log("Nothing to do.")
        return 0

    client, model = get_client()
    log(f"Using model: {model}")

    new_placements = []
    for c in candidates:
        try:
            result = classify_one(client, model, c)
        except Exception as e:
            log(f"  classify error for id={c['id']}: {e}")
            continue
        processed.add(c["id"])
        folder = result.get("folder", "SKIP")
        if folder == "SKIP" or not folder.startswith(tuple(f"{a}/" for a in AREAS)):
            continue
        if result.get("confidence") != "high":
            log(f"  SKIP (conf={result.get('confidence')}): {c['filename'][:40]}")
            continue
        # Soft cap check
        cnt, _ = folder_size(folder)
        if cnt >= SOFT_CAPS.get(folder, 50):
            # Take the id back OUT of processed.  It went in above because
            # every other rejection is a verdict on the document, but this
            # one is a verdict on the folder, and recording it as done
            # destroyed the document: 287 distinct files were dropped and
            # permanently flagged processed this way in August 2026, while all
            # 17 folders sat at cap and 40 of the last 44 runs placed nothing
            # and still exited 0.  defer_candidate keeps it out of the main
            # stream so this run still advances, and brings it back only once
            # folder_size() shows headroom.
            processed.discard(c["id"])
            defer_candidate(deferred, c["id"], folder)
            log(f"  DEFER (cap {SOFT_CAPS.get(folder, 50)} hit): {folder} ← {c['filename'][:40]}")
            continue

        # Copy
        target_dir = DOCS / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        date_prefix = fmt_date(c["date"])
        slug = slugify(c["filename"])
        ext = Path(c["filename"]).suffix.lower()
        new_name = (
            f"{date_prefix}_{slug}"
            if slug.endswith(ext)
            else f"{date_prefix}_{Path(slug).stem}{ext}"
        )
        dst = target_dir / new_name
        if dst.exists():
            continue
        try:
            shutil.copy2(c["file_path"], dst)
            new_placements.append(
                {
                    "id": c["id"],
                    "src": c["file_path"],
                    "dst": str(dst),
                    "category": folder,
                    "confidence": result.get("confidence"),
                    "classified_at": datetime.now().isoformat(),
                }
            )
            log(f"  + {folder} ← {new_name}")
        except Exception as e:
            log(f"  copy error: {e}")

    state["processed_ids"] = sorted(processed)
    # Anything that reached processed on this run is decided, so drop its
    # parking record: the map only ever holds candidates still blocked by a
    # full folder.  This is also what clears the entry when a previously
    # deferred candidate finally lands.
    state["deferred"] = {k: v for k, v in deferred.items() if int(k) not in processed}
    state["copied"].extend(new_placements)
    state["last_run"] = datetime.now().isoformat()

    # Refresh READMEs for folders that gained content (or all if --refresh-index)
    affected = {p["category"] for p in new_placements}
    if args.refresh_index:
        affected = set(MANAGED_FOLDERS)
    if affected:
        log(f"Refreshing READMEs for {len(affected)} folders")
        conn = sqlite3.connect(DB)
        for folder in affected:
            write_readme(folder, conn)
        conn.close()

    # Re-summarize affected folders (skip if no change AND not forced)
    summaries = state.get("folder_summaries", {})
    to_summarize = affected if (affected or args.refresh_index) else set()
    if to_summarize:
        log(f"Re-summarizing {len(to_summarize)} folders via LLM")
        for folder in to_summarize:
            readme = DOCS / folder / "README.md"
            if not readme.exists():
                continue
            try:
                summary = summarize_folder(
                    client, model, folder, readme.read_text(encoding="utf-8")
                )
                if "error" not in summary:
                    summaries[folder] = summary
            except Exception as e:
                log(f"  summarize error for {folder}: {e}")

    state["folder_summaries"] = summaries
    save_state(state)
    client.close()

    # Always rebuild INDEX (cheap — uses cached summaries)
    for area in AREAS:
        write_index(area, summaries)
    log(f"Done. New placements: {len(new_placements)}. State saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
