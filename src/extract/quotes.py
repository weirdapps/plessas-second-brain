"""Quoted history in a reply: where the newest message ends (issue 164).

Quoted reply chains were 46.5% of the text sent to the model (audit 2026-10, code-extract-05),
and each reply re-extracted the history as new rows: every message of a thread is extracted
from its own email, so a reply's quotes are mostly text the store already read.

`split_quoted` finds the first quoted message of a body, HTML or text:

- in HTML, the containers mail clients wrap a quote in: Outlook's `divRplyFwdMsg`,
  `appendonsend` and `mail-editor-reference-message-container`, Gmail's `gmail_quote`,
  Apple Mail's `<blockquote type="cite">`, Yahoo's `yahoo_quoted`;
- in text, an Outlook header block (From, then Sent or Date, then To, Cc or Subject, within a
  few lines; in Greek Από, Στάλθηκε or Ημερομηνία, Προς, Κοιν. or Θέμα), the
  `-----Original Message-----` rule, and a Gmail attribution ("On ... wrote:", «Στις ...
  έγραψε:»), unless answers are written between the quoted lines.

A line that only mentions From or Sent, a signature, and a body that is all quote (nothing
new above it) are not split. `strip_quoted` is what the extraction is sent: the newest
message, the start of the message it answers as context, and a note of what was left out.
A forward is never stripped: what it passes on was never extracted from its own email.
"""

import os
import re
from dataclasses import dataclass

from src.extract.html_text import html_to_text, looks_like_html

# When the quoted history is left out: "auto" (the default) when the store holds, or the run
# stages, an earlier message of the thread (src/extract/thread_state.py), so what the quotes
# repeat was extracted from its own email; "strip" always, as for the eval; "keep" never.
QUOTES_VAR = "BRAIN_EXTRACT_QUOTES"
MODES = ("auto", "strip", "keep")

# What is kept of the message a reply answers, as context: enough to read "agreed" or
# «συμφωνώ» against what was proposed.
QUOTED_CONTEXT_CHARS = 1500

_HTML_QUOTE = re.compile(
    r"""id\s*=\s*["']?(?:divRplyFwdMsg|appendonsend|mail-editor-reference-message-container)\b"""
    r"""|class\s*=\s*["'][^"']*\b(?:gmail_quote(?:_container)?|yahoo_quoted)\b"""
    r"""|<blockquote\b[^>]*\btype\s*=\s*["']?cite""",
    re.IGNORECASE,
)

# One line of an Outlook header block. Plain-text renderings may bold the label (*From:*).
_LABEL = r"^\s*\*?(?:{})\s*:\*?\s*\S"
_FROM = re.compile(_LABEL.format("from|από|de|von"), re.IGNORECASE)
_SENT = re.compile(_LABEL.format("sent|date|στάλθηκε|ημερομηνία|envoyé|gesendet"), re.IGNORECASE)
_TO = re.compile(_LABEL.format(r"to|cc|subject|προς|κοιν\.?|θέμα|objet|betreff"), re.IGNORECASE)
_HEADER_SPAN = 6  # the lines after From in which Sent and To or Subject must appear

_ORIGINAL = re.compile(r"^\s*-{2,}\s*(?:original message|αρχικό μήνυμα)\s*-{2,}\s*$", re.IGNORECASE)
_FORWARD_RULE = re.compile(
    r"^\s*(?:-{2,}\s*(?:forwarded message|προωθημένο μήνυμα)\s*-{2,}|begin forwarded message:)\s*$",
    re.IGNORECASE,
)
_RULE = re.compile(r"^\s*(?:_{10,}|-{10,})\s*$")
_ATTRIBUTION = re.compile(
    r"^\s*(?:on\s.+\swrote|στις\s.+\sέγραψε)\s*:\s*$", re.IGNORECASE | re.DOTALL
)
_QUOTED_LINE = re.compile(r"^\s*>")

_FORWARD_SUBJECT = re.compile(r"^\s*(?:fw|fwd|πρ|προώθηση)\s*:", re.IGNORECASE)


@dataclass(frozen=True)
class Split:
    newest: str  # the newest message, as text
    quoted: str  # everything from the first quoted message on, as text
    quoted_count: int  # how many quoted messages it holds, at least one
    forward: bool  # the first boundary is a forward's


@dataclass(frozen=True)
class Body:
    text: str  # what the extraction is sent
    newest: str  # the newest message alone, which items and their quotes come from
    stripped: bool
    quoted_count: int = 0  # quoted messages, when stripped
    left_out_chars: int = 0


def _header_at(lines: list[str], i: int) -> bool:
    if not _FROM.match(lines[i]):
        return False
    following = lines[i + 1 : i + 1 + _HEADER_SPAN]
    return any(_SENT.match(line) for line in following) and any(
        _TO.match(line) for line in following
    )


def _attribution_at(lines: list[str], i: int) -> int:
    """How many lines an attribution starting at line i takes (Gmail wraps a long one onto
    the next line); 0 when none starts there."""
    if _ATTRIBUTION.match(lines[i]):
        return 1
    if i + 1 < len(lines) and _ATTRIBUTION.match(lines[i] + " " + lines[i + 1]):
        return 2
    return 0


def _inline_answers(lines: list[str]) -> bool:
    """Whether text after quoted (>) lines is written between them: an inline reply."""
    seen_quote = False
    for line in lines:
        if _QUOTED_LINE.match(line):
            seen_quote = True
        elif seen_quote and line.strip():
            return True
    return False


def _boundaries(lines: list[str]) -> list[tuple[int, str]]:
    """Each line that starts a quoted message, with its kind: header, original, forward or
    attribution."""
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FORWARD_RULE.match(line):
            out.append((i, "forward"))
        elif _ORIGINAL.match(line):
            out.append((i, "original"))
        elif _header_at(lines, i):
            # A header straight after a rule or an Original Message line belongs to it.
            if not (
                out
                and out[-1][1] in ("original", "forward")
                and _blank_between(lines, out[-1][0], i)
            ):
                out.append((i, "header"))
        elif width := _attribution_at(lines, i):
            out.append((i, "attribution"))
            i += width
            continue
        i += 1
    return out


def _blank_between(lines: list[str], start: int, end: int) -> bool:
    return all(not line.strip() for line in lines[start + 1 : end])


def _split_text(text: str) -> Split | None:
    lines = text.split("\n")
    found = _boundaries(lines)
    if not found:
        return None
    first, kind = found[0]
    if kind == "attribution" and _inline_answers(lines[first:]):
        return None
    start = first
    # An Outlook rule (underscores) on the line before the header is part of the quote.
    back = start - 1
    while back >= 0 and not lines[back].strip():
        back -= 1
    if back >= 0 and _RULE.match(lines[back]):
        start = back
    newest = "\n".join(lines[:start]).strip()
    if not newest:
        return None
    quoted = "\n".join(lines[start:]).strip()
    return Split(newest, quoted, len(found), kind == "forward")


def split_quoted(content: str | None) -> Split | None:
    """The newest message of `content` (HTML or text) and its quoted history; None when it
    quotes nothing, when its answers are written between the quoted lines, or when nothing
    stands above the quote."""
    if not content:
        return None
    if looks_like_html(content):
        marker = _HTML_QUOTE.search(content)
        if marker is None:
            return _split_text(html_to_text(content))
        cut = content.rfind("<", 0, marker.start() + 1)
        cut = marker.start() if cut < 0 else cut
        newest = html_to_text(content[:cut]).strip()
        quoted = html_to_text(content[cut:]).strip()
        if not newest or not quoted:
            return None
        found = _boundaries(quoted.split("\n"))
        forward = bool(found) and found[0] == (0, "forward")
        return Split(newest, quoted, max(1, len(found)), forward)
    return _split_text(content)


def _first_message(quoted: str) -> str:
    """The first quoted message: its header and its text, up to the next quoted message."""
    lines = quoted.split("\n")
    found = [i for i, _ in _boundaries(lines) if i > 0]
    return "\n".join(lines[: found[0]] if found else lines).strip()


def quotes_mode() -> str:
    """BRAIN_EXTRACT_QUOTES, checked: any other value stops the call, as an unknown call
    shape does."""
    mode = os.environ.get(QUOTES_VAR, "").strip().lower() or "auto"
    if mode not in MODES:
        raise ValueError(f"{QUOTES_VAR} must be one of {', '.join(MODES)}, not {mode!r}")
    return mode


def should_strip(thread_state: dict | None) -> bool:
    """Whether this email's quoted history is left out, by the mode and its thread state."""
    mode = quotes_mode()
    if mode == "auto":
        return bool(thread_state and thread_state.get("earlier_messages"))
    return mode == "strip"


def is_forward(subject: str | None) -> bool:
    return bool(_FORWARD_SUBJECT.match(subject or ""))


def strip_quoted(content: str | None, *, subject: str | None = None) -> Body:
    """What the extraction is sent of a body: the newest message, the start of the message
    it answers (QUOTED_CONTEXT_CHARS) as context, and a line saying what was left out. A
    body that quotes nothing, a forward, and one that would come out no shorter come back
    whole, as text."""
    content = content or ""
    whole = html_to_text(content) if looks_like_html(content) else content
    split = split_quoted(content)
    if split is None:
        return Body(whole, whole, stripped=False)
    if split.forward or is_forward(subject):
        return Body(whole, split.newest, stripped=False)
    excerpt = _first_message(split.quoted)
    if len(excerpt) > QUOTED_CONTEXT_CHARS:
        excerpt = excerpt[:QUOTED_CONTEXT_CHARS] + " [...]"
    left_out = max(0, len(split.quoted) - len(excerpt))
    text = (
        f"{split.newest}\n\n"
        "[Quoted below, as context only: the start of the message this one answers.]\n"
        f"{excerpt}\n"
        f"[{split.quoted_count} quoted message(s), {left_out} characters, left out: each was"
        " extracted from its own email.]"
    )
    if len(text) >= len(whole):
        # A short quote fits in the excerpt: leaving it out would only add the notes.
        return Body(whole, split.newest, stripped=False)
    return Body(
        text, split.newest, stripped=True, quoted_count=split.quoted_count, left_out_chars=left_out
    )
