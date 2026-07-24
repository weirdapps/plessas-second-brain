"""Filesystem scanning + dedup for the reverse-ingest pipeline.

Pure functions (no I/O beyond Path.iterdir / Path.stat) so the dedup
logic can be unit-tested without a real filesystem walk. The CLI
orchestrator (cmd_reverse_ingest in src/cli.py) handles I/O and
calls into ingest_document.
"""

import re
from datetime import datetime
from pathlib import Path

# Curated filename pattern: YYYYMMDDHHMM_<rest>
# Set by an external curation step when it mirrors email attachments to disk.
_PREFIX_RE = re.compile(r"^(\d{12})_(.+)$")


def is_curated_filename(name: str) -> bool:
    """True iff the filename starts with the curate-docs YYYYMMDDHHMM_ prefix."""
    return _PREFIX_RE.match(name) is not None


def _version_key(path: Path) -> str:
    """Return a YYYYMMDDHHMM string used to compare versions of the same logical
    document. Files with the curate-docs prefix use that prefix verbatim.
    Files without a prefix fall back to the file's mtime, formatted to the
    same fixed-width string. Lex sort over this key matches chronological order.
    """
    m = _PREFIX_RE.match(path.name)
    if m:
        return m.group(1)
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return mtime.strftime("%Y%m%d%H%M")


def select_files_to_ingest(scanned: list[Path]) -> list[Path]:
    """Latest-version-per-(folder, logical-name) dedup.

    For each path:
      - logical_name = post-prefix tail if filename matches ^\\d{12}_*, else full filename
      - group key    = (parent_dir, logical_name)
    Within each group, the file with the lex-greatest _version_key wins.
    """
    groups: dict[tuple[Path, str], list[Path]] = {}
    for path in scanned:
        m = _PREFIX_RE.match(path.name)
        logical = m.group(2) if m else path.name
        groups.setdefault((path.parent, logical), []).append(path)

    selected: list[Path] = []
    for paths in groups.values():
        if len(paths) == 1:
            selected.append(paths[0])
        else:
            selected.append(max(paths, key=_version_key))
    return selected


def scan_roots(roots: list[Path], extensions: set[str]) -> list[Path]:
    """Recursively walk each root, returning files whose suffix (lowercased)
    is in `extensions`. Missing roots are silently skipped.
    """
    out: list[Path] = []
    lower_exts = {e.lower() for e in extensions}
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in lower_exts:
                out.append(path)
    return out


def topic_label(file_path: Path, roots: list[Path]) -> str:
    """Build a source-label for the file based on which root it sits under.

    Examples (with roots = [~/Documents/National, ~/Documents/Personal]):
      ~/Documents/National/units/cards/x.pdf  →  "National/units/cards"
      ~/Documents/Personal/PD2024EN.pdf       →  "Personal"
      /tmp/stray/doc.pdf (no matching root)   →  "stray"  (parent dir name)
    """
    file_path = file_path.resolve()
    for root in roots:
        try:
            rel = file_path.resolve().relative_to(root.resolve())
        except ValueError:
            continue
        # Use root's last segment (e.g. "National") + relative path of the parent dir
        parent_rel = rel.parent  # may be PosixPath('.') if file is at root level
        if str(parent_rel) == ".":
            return root.name
        return f"{root.name}/{parent_rel}"
    # No root matched — degrade to parent dir name
    return file_path.parent.name
