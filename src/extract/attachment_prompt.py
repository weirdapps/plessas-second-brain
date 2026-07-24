"""Prompt template for Vertex AI attachment content extraction."""

from src.config import USER_NAME, USER_ROLE


def build_attachment_prompt(
    extracted_text: str,
    filename: str,
    mime_type: str,
    email_subject: str = None,
    email_date: str = None,
) -> str:
    """Build extraction prompt for an attachment's extracted text."""
    identity_context = ""
    if USER_NAME or USER_ROLE:
        parts = []
        if USER_NAME:
            parts.append(f"The document owner is {USER_NAME}")
        if USER_ROLE:
            parts.append(f"whose role is {USER_ROLE}")
        identity_context = f"\nContext: {', '.join(parts)}.\n"

    email_context = ""
    if email_subject:
        email_context += f"Parent email subject: {email_subject}\n"
    if email_date:
        email_context += f"Parent email date: {email_date}\n"

    max_chars = 50_000
    text = extracted_text[:max_chars] if len(extracted_text) > max_chars else extracted_text
    truncation_note = ""
    if len(extracted_text) > max_chars:
        truncation_note = (
            f"\n[Document truncated from {len(extracted_text)} to {max_chars} characters]\n"
        )

    return f"""You are extracting structured information from a document attachment.
{identity_context}
{email_context}Attachment filename: {filename}
File type: {mime_type or "unknown"}
{truncation_note}
Document content:
{text}

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
