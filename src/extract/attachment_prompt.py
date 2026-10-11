"""Prompt template for Vertex AI attachment content extraction."""

import json

from src.config import USER_NAME, USER_ROLE
from src.extract.untrusted import fence

# Phase 2 sends a longer text in parts (src/extract/attachment_pipeline.py). A part stays under
# the prompt's own 50,000-character guard, with room for the instructions.
PART_CHARS = 40_000


def _identity_context() -> str:
    if not (USER_NAME or USER_ROLE):
        return ""
    parts = []
    if USER_NAME:
        parts.append(f"The document owner is {USER_NAME}")
    if USER_ROLE:
        parts.append(f"whose role is {USER_ROLE}")
    return f"\nContext: {', '.join(parts)}.\n"


def _email_context(email_subject: str | None, email_date: str | None) -> str:
    context = ""
    if email_subject:
        context += f"Parent email subject: {email_subject}\n"
    if email_date:
        context += f"Parent email date: {email_date}\n"
    return context


def split_text(text: str, max_chars: int = PART_CHARS) -> list[str]:
    """Cut text into parts of at most max_chars, at paragraph boundaries where it can.

    A paragraph longer than a part is cut where the limit falls.
    """
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for para in text.split("\n\n"):
        while len(para) > max_chars:
            if current:
                parts.append("\n\n".join(current))
                current, size = [], 0
            parts.append(para[:max_chars])
            para = para[max_chars:]
        extra = len(para) + (2 if current else 0)
        if current and size + extra > max_chars:
            parts.append("\n\n".join(current))
            current, size, extra = [], 0, len(para)
        current.append(para)
        size += extra
    if current:
        parts.append("\n\n".join(current))
    return parts


def build_attachment_prompt(
    extracted_text: str,
    filename: str,
    mime_type: str,
    email_subject: str | None = None,
    email_date: str | None = None,
    part: tuple[int, int] | None = None,
    digest: bool = False,
) -> str:
    """Build extraction prompt for an attachment's extracted text.

    `part` is (this part, how many) when Phase 2 sends a long text in parts. `digest` says the
    text is a long spreadsheet's digest (src/extract/attachment_digest.py), not its cells.
    """
    identity_context = _identity_context()
    email_context = _email_context(email_subject, email_date)

    max_chars = 50_000
    text = extracted_text[:max_chars] if len(extracted_text) > max_chars else extracted_text
    truncation_note = ""
    if len(extracted_text) > max_chars:
        truncation_note = (
            f"\n[Document truncated from {len(extracted_text)} to {max_chars} characters]\n"
        )
    part_note = f"\nThis is part {part[0]} of {part[1]} of a longer document.\n" if part else ""
    # An instruction, so it stays outside the fence, where the model is told to follow nothing.
    digest_note = (
        (
            "\nThe document content is a digest of a spreadsheet, not its cells: per sheet its"
            " size, header row, sample rows, column types with their statistics, and any free"
            " text. Describe what the workbook holds and shows. Take decisions and action items"
            " only from text that states them, never from table rows.\n"
        )
        if digest
        else ""
    )

    # The filename and the parent email's subject are third-party text too.
    document = f"""{email_context}Attachment filename: {filename}
File type: {mime_type or "unknown"}

Document content:
{text}"""

    return f"""You are extracting structured information from a document attachment.
{identity_context}
{truncation_note}{part_note}{digest_note}
{fence(document)}

---

Extract the following and return ONLY a valid JSON object (no markdown, no code blocks):

{{
  "summary": "2-3 sentence summary of the document's substance and purpose",
  "topics": ["topic keywords relevant to this document"],
  "decisions": [
    {{"decision": "description of decision", "decided_by": "person or body"}}
  ],
  "action_items": [
    {{"task": "description", "owner": "person responsible", "deadline": "YYYY-MM-DD or null"}}
  ],
  "key_facts": ["important facts, numbers, dates, findings"],
  "language": "greek | english | mixed"
}}

Rules:
1. Handle both Greek and English text
2. Return empty lists for fields with no data
3. Only extract information actually present in the document
4. For key_facts, focus on substantive data points, not formatting
5. For summary, describe what the document IS and what it SAYS, not just the topic"""


def build_merge_prompt(
    parts: list[dict],
    filename: str,
    mime_type: str,
    email_subject: str | None = None,
    email_date: str | None = None,
    covered: tuple[int, int] | None = None,
) -> str:
    """One extraction over the extractions of a long document's parts.

    `covered` is (parts summarised, parts in the document) when only some were summarised.
    """
    body = "\n\n".join(
        f"Extraction {i} of {len(parts)}:\n{json.dumps(p, ensure_ascii=False)}"
        for i, p in enumerate(parts, 1)
    )
    # An instruction, so it belongs outside the fence, where the model is told to follow nothing.
    coverage = ""
    if covered and covered[0] < covered[1]:
        coverage = (
            f"\n4. These are the extractions of {covered[0]} of the document's {covered[1]} parts,"
            " taken from across it: describe the whole document from them, and say the"
            f" summary covers {covered[0]} of {covered[1]} parts"
        )
    document = f"""{_email_context(email_subject, email_date)}Attachment filename: {filename}
File type: {mime_type or "unknown"}

{body}"""

    return f"""You are combining the extractions of the parts of one long document into one.
{_identity_context()}
{fence(document)}

---

Return ONLY a valid JSON object (no markdown, no code blocks) with the same fields:

{{
  "summary": "2-3 sentence summary of the WHOLE document's substance and purpose",
  "topics": ["topic keywords relevant to this document"],
  "decisions": [
    {{"decision": "description of decision", "decided_by": "person or body"}}
  ],
  "action_items": [
    {{"task": "description", "owner": "person responsible", "deadline": "YYYY-MM-DD or null"}}
  ],
  "key_facts": ["important facts, numbers, dates, findings"],
  "language": "greek | english | mixed"
}}

Rules:
1. The summary covers the whole document, not one part
2. Keep every decision, action item and key fact that matters; drop only repeats
3. Only use information present in the parts{coverage}"""
