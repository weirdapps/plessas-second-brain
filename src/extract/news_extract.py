"""Extraction for news items without the model.

news-reader already summarised what it staged: a digest synthesis carries an
executive brief and categorised sections, an article its own text. Sending them
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
BRIEF_CHARS = 2000
MAX_TOPICS = 10
_FOOTER = "\n---\n"


def is_news(email: dict) -> bool:
    """Whether a staged item came from news-sync."""
    return email.get("mailbox_name") == NEWS_MAILBOX


def _opening(text: str) -> str:
    return text.strip()[:ARTICLE_SUMMARY_CHARS]


def _brief(brief: object) -> str:
    """The executive brief as one line. news-reader writes its bullets as strings,
    or as {"text": ..., "article_ids": [...]} before it flattens them."""
    if isinstance(brief, str):
        return brief.strip()
    if not isinstance(brief, list):
        return ""
    lines = [item.get("text") if isinstance(item, dict) else item for item in brief]
    return " ".join(line.strip() for line in lines if isinstance(line, str) and line.strip())


def _section_topics(sections: object) -> list[str]:
    """A topic per section, from its category (stable across digests), not its
    display name, which news-reader writes new for each day's news."""
    if not isinstance(sections, list):
        return []
    topics: list[str] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        name = section.get("category")
        if not isinstance(name, str) or not name.strip():
            name = section.get("display_name")
        if isinstance(name, str) and name.strip():
            topic = name.strip().replace("_", " ")
            if topic not in topics:
                topics.append(topic)
    return topics[:MAX_TOPICS]


def _synthesis_fields(body: str) -> tuple[str, list[str]]:
    """Summary and topics from a synthesis body, JSON when news-reader wrote JSON."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return _opening(body), []
    if not isinstance(data, dict):
        return _opening(body), []
    summary = _brief(data.get("executive_brief"))[:BRIEF_CHARS] or _opening(body)
    return summary, _section_topics(data.get("sections"))


def _article_fields(content: str) -> tuple[str, list[str]]:
    """Summary and topics from an article: its opening, and its categories line.

    news_export appends its footer last, so the footer starts at the last
    separator, whatever the article's own text contains.
    """
    text, footer = content, ""
    if _FOOTER in content:
        text, _, footer = content.rpartition(_FOOTER)
    topics: list[str] = []
    for line in footer.splitlines():
        if line.startswith("Categories:"):
            topics = [c.strip() for c in line.removeprefix("Categories:").split(",") if c.strip()]
    return _opening(text), topics[:MAX_TOPICS]


def extract_news(email: dict) -> dict:
    """The extraction for a news item, built from its own text. Never raises."""
    content = str(email.get("content") or "")
    if str(email.get("message_id", "")).startswith("news:synthesis:"):
        # news_export writes a one-line header, then the synthesis.
        summary, topics = _synthesis_fields(content.partition("\n")[2])
    else:
        summary, topics = _article_fields(content)
    # The defaults every extraction carries. Set the text afterwards rather than
    # parsing it, which would run the parser's JSON clean-up over its words.
    extraction = parse_extraction("{}")
    extraction["summary"] = summary
    extraction["topics"] = topics
    extraction["message_id"] = email.get("message_id")
    return extraction
