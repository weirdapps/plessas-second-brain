"""Strip credentials, and mask card numbers, IBANs and passwords, before they reach the store.

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

Deliberately narrow. Every credential pattern below matches an issuer-defined
prefix plus a length, so it cannot fire on prose, and the replacement preserves the
prefix so a reader can still tell WHICH credential was present and needs rotating.

Card numbers, IBANs and passwords carry no prefix, and an audit on 2026-10-11 found
all three in the store: lists of card numbers in attachment text, card numbers and
IBANs copied into key facts, and meeting passwords lifted into facts as well. Each
is matched by its shape and then checked before anything is replaced: a card
number by its issuer range, length and Luhn digit, an IBAN by its mod-97 check, a
password by the key in front of it. A candidate that fails is left exactly as it
was. The mask keeps what tells a reader which one was meant: a card's first six and
last four digits, an IBAN's country and last four characters, a password's key.

This is a containment measure, not a scanner: it will not catch a bare hex string,
a password written without its key or a tax number, and it is not a reason to relax
any other control.
"""

import re
from collections.abc import Callable

# What groups a card number's digits: one space, no-break space or narrow no-break
# space (word processors and number formatting set the last two), or a hyphen.
_GROUP = "[ \u00a0\u202f-]"

# (name, compiled pattern). The replacement names what was there: a credential
# becomes [REDACTED:<name>], and the entries at the end are masked in part (_MASKS).
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
    # The entries below are checked before they are replaced, and replaced in part
    # (_MASKS). The IBAN comes before the card, so an account's digits are not read
    # as a card number.
    #
    # IBAN: two letters and two check digits, then letters and digits, whole or
    # printed in groups of four with the groups after it (_mask_ibans finds where
    # each IBAN ends). Each of these three patterns starts with a character class
    # and asserts what precedes it only after, which lets the regex engine skip
    # ahead to that class instead of trying every position.
    (
        "iban",
        re.compile(
            r"[A-Z](?<![0-9A-Za-z][A-Z])[A-Z][0-9]{2}"
            r"(?:[0-9A-Z]{11,30}|(?:[ \u00a0\u202f][0-9A-Z]{1,4})+)(?![0-9A-Za-z])"
        ),
    ),
    # Card number: a run of at least 13 digits, whole or in groups, that stands on
    # its own: not part of a word or a longer number, not after "+" (a phone
    # number) and not the fraction of a decimal.
    (
        "card",
        re.compile(
            rf"[0-9](?<![0-9A-Za-z+][0-9])(?<![0-9]\.[0-9])(?=(?:{_GROUP}?[0-9]){{12}})"
            rf"[0-9]*(?:{_GROUP}[0-9]+)*(?![0-9A-Za-z])"
        ),
    ),
    # Password: a password, passcode or «κωδικός πρόσβασης» key, then ":" or "=",
    # maybe across HTML tags, then the value. «κωδικός» alone also means a product
    # or customer code, so it is not a key. A value already redacted is left alone.
    (
        "password",
        re.compile(
            r"(?P<key>[PpΚκ](?:(?<![A-Za-z][Pp])(?i:ass(?:word|code|wd)|wd)(?![A-Za-z])"
            r"|(?<!\w[Κκ])(?i:ωδικ[οό][σς]?[ \t\u00a0]+πρ[οό]σβασ[ηή][σς])(?!\w)))"
            r"(?P<sep>(?:[ \t\u00a0]{0,4}<[^<>\n]{0,200}>){0,4}[ \t\u00a0]{0,4}[\"']?"
            r"[ \t\u00a0]{0,4}(?:(?i:is|είναι)[ \t\u00a0]{0,4})?[:=](?!=)"
            r"(?:[ \t\u00a0]{0,4}<[^<>\n]{0,200}>){0,4}[ \t\u00a0]{0,4})"
            r"(?![\"']?\[REDACTED:)"
            r"(?P<value>\"[^\"\n]{1,200}\"|'[^'\n]{1,200}'|[^\s<\"']+)"
        ),
    ),
]

# The layouts a card number is printed in, longest first: 19 digits, 16 in fours,
# then American Express and Diners.
_LAYOUTS = ((4, 4, 4, 4, 3), (4, 4, 4, 4), (4, 6, 5), (4, 6, 4))
_GROUPS = re.compile(f"({_GROUP})")
_IBAN_GROUPS = re.compile("([ \u00a0\u202f])")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _is_card_number(digits: str) -> bool:
    """Whether 13 to 19 digits can be a card number: in an issuer's range (Visa,
    Mastercard, American Express, Diners, Discover) at a length it issues, with
    more than two distinct digits and a valid Luhn check digit."""
    head = int(digits[:4])
    lengths: tuple[int, ...] | range
    if digits[0] == "4":
        lengths = (13, 16, 19)
    elif 5100 <= head <= 5599 or 2221 <= head <= 2720:
        lengths = (16,)
    elif head // 100 in (34, 37):
        lengths = (15,)
    elif head // 100 in (36, 38) or 3000 <= head <= 3059:
        lengths = range(14, 20)
    elif head == 6011 or head // 100 == 65 or 6440 <= head <= 6499:
        lengths = range(16, 20)
    else:
        return False
    return len(digits) in lengths and len(set(digits)) > 2 and _luhn_ok(digits)


def _card_at(groups: list[str], i: int) -> int:
    """How many groups from groups[i] make a card number, or 0: one group of 13 to
    19 digits, or groups in a layout cards are printed in."""
    size = len(groups[i])
    if 13 <= size <= 19:
        return 1 if _is_card_number(groups[i]) else 0
    if size == 4:
        for layout in _LAYOUTS:
            window = groups[i : i + len(layout)]
            if tuple(len(g) for g in window) == layout and _is_card_number("".join(window)):
                return len(layout)
    return 0


def _mask_run(
    run: str,
    split: re.Pattern[str],
    found: Callable[[list[str], int], int],
    mask: Callable[[str], str],
) -> str:
    """`run` with every value in it masked. The run is cut into groups at `split`;
    `found` says how many groups from a given one make a value, and the value's
    characters, separators left out, are replaced by `mask` of them."""
    parts = split.split(run)
    groups, seps = parts[::2], parts[1::2]
    out: list[str] = []
    i = 0
    while i < len(groups):
        taken = found(groups, i)
        out.append(mask("".join(groups[i : i + taken])) if taken else groups[i])
        i += taken or 1
        if i < len(groups):
            out.append(seps[i - 1])
    return "".join(out)


def _mask_cards(match: re.Match[str]) -> str:
    """Each card number in the run masked to its first six and last four digits.
    A run can hold several, or a row number or a year before one."""
    return _mask_run(match.group(0), _GROUPS, _card_at, lambda d: f"{d[:6]}[REDACTED:card]{d[-4:]}")


def _is_iban(value: str) -> bool:
    """An ISO 13616 IBAN: 15 to 34 characters whose mod-97 check holds."""
    if not 15 <= len(value) <= 34:
        return False
    return int("".join(str(int(c, 36)) for c in value[4:] + value[:4])) % 97 == 1


def _is_iban_head(group: str) -> bool:
    """A printed IBAN's first group: the country's two letters and the check digits."""
    return len(group) == 4 and group[:2].isascii() and group[:2].isupper() and group[2:].isdigit()


def _iban_ends(groups: list[str], i: int) -> list[int]:
    """Each j, longest first, for which groups[i:j] passes the IBAN check. Printed,
    every group of an IBAN but its last is four long."""
    end = i + 1
    while end < len(groups) and end - i < 9 and len(groups[end - 1]) == 4:
        end += 1
    return [j for j in range(end, i, -1) if _is_iban("".join(groups[i:j]))]


def _iban_at(groups: list[str], i: int) -> int:
    """How many groups from groups[i] make an IBAN, or 0. A word, a number or another
    IBAN after a printed one reads as more groups, so the longest run whose check
    holds is taken that does not take in the start of another IBAN."""
    if len(groups) > 1 and not _is_iban_head(groups[i]):
        return 0
    ends = _iban_ends(groups, i)
    for j in ends:
        size = len(groups[i])
        for m in range(i + 1, j):
            if size >= 15 and _is_iban_head(groups[m]) and _iban_ends(groups, m):
                break
            size += len(groups[m])
        else:
            return j - i
    return ends[0] - i if ends else 0


def _mask_ibans(match: re.Match[str]) -> str:
    """Each IBAN in the run masked to its country and last four characters."""
    return _mask_run(
        match.group(0), _IBAN_GROUPS, _iban_at, lambda v: f"{v[:2]}[REDACTED:iban]{v[-4:]}"
    )


def _mask_password(match: re.Match[str]) -> str:
    """The key and what joins it to the value stay; the value goes, inside its quotes."""
    value = match.group("value")
    quote = value[0] if value[0] in "\"'" else ""
    return f"{match.group('key')}{match.group('sep')}{quote}[REDACTED:password]{quote}"


# How the checked entries are replaced. Every other entry is replaced whole by
# [REDACTED:<name>].
_MASKS: dict[str, Callable[[re.Match[str]], str]] = {
    "iban": _mask_ibans,
    "card": _mask_cards,
    "password": _mask_password,
}

# The extraction prompts' rule for the same values: the model is told not to copy
# them, and the loaders mask what it copies anyway.
PROMPT_RULE = (
    "Never copy card, account, IBAN, tax or password values; "
    'refer to them as "a card ending 1234" at most.'
)


def redact_secrets(text: str) -> str:
    """Return `text` with credential-shaped substrings replaced by a marker, and
    card numbers, IBANs and password values masked."""
    if not text or not isinstance(text, str):
        return text
    for name, pattern in _PATTERNS:
        text = pattern.sub(_MASKS.get(name, f"[REDACTED:{name}]"), text)
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
