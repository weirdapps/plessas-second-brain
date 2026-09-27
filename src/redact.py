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
    # Anthropic: API keys (sk-ant-api03-), Claude Code OAuth tokens
    # (sk-ant-oat01-) and admin keys (sk-ant-admin01-). The placeholder
    # "sk-ant-..." has no kind and number, so it is left alone.
    ("anthropic-key", re.compile(r"sk-ant-(?:api|oat|admin)\d{2}-[A-Za-z0-9_\-]{20,}")),
    # Google / Gemini: AIzaSy + 33 more.
    ("google-key", re.compile(r"AIza[A-Za-z0-9_\-]{35}")),
    # GitHub PAT / OAuth / refresh / server / user-to-server.
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    # GitHub fine-grained PAT, the current default: github_pat_ + 22 + _ + 59.
    ("github-pat", re.compile(r"github_pat_[A-Za-z0-9_]{60,}")),
    # Slack.
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    # OpenAI project, service-account and admin keys are base64url, so their
    # body holds '-' and '_'. This entry must come before the plain one below,
    # which stops at the first dash: in that order the plain pattern skipped
    # most project keys and replaced only the first run of the rest, leaving
    # the tail in clear.
    ("openai-key", re.compile(r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{40,}")),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9]{40,}")),
    # Google OAuth access token (gcloud auth print-access-token). Dotted
    # segments are taken whole, and a sentence's closing full stop is not.
    ("google-oauth", re.compile(r"ya29\.[A-Za-z0-9_\-]{20,}(?:\.[A-Za-z0-9_\-]+)*")),
    # JWT bearer: header and payload are base64url JSON, so both begin eyJ.
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ),
    # Healthchecks per-check ping URL: whoever holds it can ping the check green
    # and hide an outage. The host stays readable; the check id or ping key and
    # slug go.
    (
        "healthchecks-ping",
        re.compile(
            r"(?<=hc-ping\.com/)(?:[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}"
            r"|[A-Za-z0-9_\-]{22}/[\w\-]+)"
        ),
    ),
    # AWS access key id, which is enough to identify the account.
    ("aws-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # Private key blocks: replace the whole armoured body, not just the header.
    # The body stops at any run of five dashes, so a header with no END line
    # stops looking at the next header, where a block cut off before it ends:
    # looking to the end of the text for every header made the cost grow with
    # the square of the size.
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[^-]|-(?!----))*+"
            r"(?:-----END [A-Z ]*PRIVATE KEY-----|(?=-----BEGIN [A-Z ]*PRIVATE KEY-----))"
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
