"""Strip credential material before it is written to the store.

Nothing on the ingest path did this, and the corpus proves the consequence: an
audit on 2026-09-09 found 10 conversation turns carrying `sk-ant-api03` keys
(108 to 120 chars, so real keys and not the `sk-ant-...` placeholder from
.env.example), 13 carrying `AIzaSy` Google keys and 8 carrying 45-character
`ghp_` GitHub tokens. Claude Code transcripts are the obvious source: an agent
that reads a .env, echoes a token, or pastes a curl command puts the secret in
its own transcript, and conversation ingest then copies it into brain.db.

That matters more here than in a log file, because this store fans out. Rows are
sent to Vertex AI for extraction, replicated to every consumer host by rsync, and
frozen into encrypted offsite snapshots that are retained for months. A secret
that lands here is not in one place, it is in every backup generation taken since.

Deliberately narrow. Every pattern below matches an issuer-defined prefix plus a
length, so it cannot fire on prose, and the replacement preserves the prefix so a
reader can still tell WHICH credential was present and needs rotating. This is a
containment measure, not a scanner: it will not catch a bare hex string or a
password, and it is not a reason to relax any other control.
"""

import re

# (name, compiled pattern). Each keeps a readable prefix in the replacement.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Anthropic: sk-ant-api03-... (the placeholder "sk-ant-..." has no api NN).
    ("anthropic-key", re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_\-]{20,}")),
    # Google / Gemini: AIzaSy + 33 more.
    ("google-key", re.compile(r"AIza[A-Za-z0-9_\-]{35}")),
    # GitHub PAT / OAuth / refresh / server / user-to-server.
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    # Slack.
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    # OpenAI.
    ("openai-key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{40,}")),
    # AWS access key id, which is enough to identify the account.
    ("aws-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # Private key blocks: replace the whole armoured body, not just the header.
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # Telegram bot tokens: <digits>:<35 char base64ish>.
    ("telegram-token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{32,}")),
]


def redact_secrets(text: str) -> str:
    """Return `text` with credential-shaped substrings replaced by a marker."""
    if not text or not isinstance(text, str):
        return text
    for name, pattern in _PATTERNS:
        text = pattern.sub(f"[REDACTED:{name}]", text)
    return text


def redact_payload(obj):
    """Recursively redact every string in a JSON-serialisable structure.

    Dict KEYS are left alone: a key is a field name, and rewriting one would
    change the staging contract that extract and load read.
    """
    if isinstance(obj, str):
        return redact_secrets(obj)
    if isinstance(obj, list):
        return [redact_payload(v) for v in obj]
    if isinstance(obj, dict):
        return {k: redact_payload(v) for k, v in obj.items()}
    return obj
