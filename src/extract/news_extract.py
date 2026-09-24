"""Extraction for news items without the model.

news-reader already summarised what it staged: a digest synthesis carries an
executive brief and named sections, an article its own text. Sending them
through the email extraction, written for the owner's own mail, spent a top-model
call on each of 12,249 items and produced 19,533 decisions and 12,948 actions
that the store then hides by default. This builds the same shape from what the
item already says, and invents nothing: no decisions, actions, people or facts.
"""

import json

from src.export.news_export import NEWS_MAILBOX
from src.extract.parser import parse_extraction

# Enough of an article's opening to stand for it in the vector index and the
# summary index.
ARTICLE_SUMMARY_CHARS = 600
MAX_TOPICS = 10


def is_news(email: dict) -> bool:
    """Whether a staged item came from news-sync."""
    return email.get("mailbox_name") == NEWS_MAILBOX


def _synthesis_fields(body: str) -> tuple[str, list[str]]:
    """Summary and topics from a synthesis body, JSON when news-reader wrote JSON."""
    try:
        data = json.loads(body)
    except ValueError:
        return body.strip()[:ARTICLE_SUMMARY_CHARS], []
    if not isinstance(data, dict):
        return body.strip()[:ARTICLE_SUMMARY_CHARS], []
    brief = data.get("executive_brief")
    if isinstance(brief, list):
        summary = " ".join(str(line).strip() for line in brief if str(line).strip())
    elif isinstance(brief, str):
        summary = brief.strip()
    else:
        summary = body.strip()[:ARTICLE_SUMMARY_CHARS]
    sections = data.get("sections")
    topics = []
    if isinstance(sections, list):
        topics = [
            s["display_name"].strip()
            for s in sections
            if isinstance(s, dict) and isinstance(s.get("display_name"), str)
        ]
    return summary, [t for t in topics if t][:MAX_TOPICS]


def _article_fields(content: str) -> tuple[str, list[str]]:
    """Summary and topics from an article: its opening, and its categories line."""
    text, _, footer = content.partition("\n---\n")
    topics: list[str] = []
    for line in footer.splitlines():
        if line.startswith("Categories:"):
            topics = [c.strip() for c in line.removeprefix("Categories:").split(",") if c.strip()]
    return text.strip()[:ARTICLE_SUMMARY_CHARS], topics[:MAX_TOPICS]


def extract_news(email: dict) -> dict:
    """The extraction for a news item, built from its own text. Never raises."""
    content = str(email.get("content") or "")
    if str(email.get("message_id", "")).startswith("news:synthesis:"):
        # news_export writes a one-line header, then the synthesis.
        summary, topics = _synthesis_fields(content.partition("\n")[2])
    else:
        summary, topics = _article_fields(content)
    extraction = parse_extraction(json.dumps({"summary": summary, "topics": topics}))
    extraction["message_id"] = email.get("message_id")
    return extraction
