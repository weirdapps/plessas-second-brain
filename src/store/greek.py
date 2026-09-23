"""Greek accent folding, defined once for both Python and SQL.

Most of this corpus is Greek, and FTS5's unicode61 tokenizer case-folds but does
NOT strip the Greek tonos. `remove_diacritics 2` does not help either: measured on
SQLite 3.53.4, it leaves the precomposed Greek vowels alone. So the index held
"παρουσίαση" and a search for "παρουσιαση" found almost nothing. Measured against
a LIKE baseline on the live corpus before this landed:

    παρουσίαση  4577 accented   137 unaccented    (3%)
    πελατών     6675 accented   106 unaccented    (2%)
    κάρτες      3550 accented   277 unaccented    (8%)
    ψηφιακή      827 accented     9 unaccented    (1%)

Greeks routinely type without accents, and an agent reformulating a query drops
them freely, so this was a large and silent loss.

The fix folds BOTH sides: the index stores folded text and the query is folded
before it is matched. Accented queries keep working, because folding an accented
query yields the same folded form.

The pairs below are the single source of truth. `fold()` is the Python side, used
on the query string; `fold_sql_expr()` generates the equivalent nested replace()
chain used by the generated columns the FTS indexes. A test asserts the two agree
on real corpus text, because two implementations of one rule is exactly how an
index and its queries drift apart.

Folding is 1:1 per character, which is what makes it safe here: snippet() offsets
computed against the folded text still line up with the original.
"""

import re
import sqlite3
import unicodedata
from functools import lru_cache

# Precomposed Greek vowels carrying tonos or dialytika, mapped to their bare form.
# Uppercase entries are included because Greek all-caps legitimately keeps the
# dialytika even though it drops the tonos, and because text arrives in both.
FOLD_PAIRS: tuple[tuple[str, str], ...] = (
    ("ά", "α"),
    ("έ", "ε"),
    ("ή", "η"),
    ("ί", "ι"),
    ("ό", "ο"),
    ("ύ", "υ"),
    ("ώ", "ω"),
    ("ϊ", "ι"),
    ("ϋ", "υ"),
    ("ΐ", "ι"),
    ("ΰ", "υ"),
    ("Ά", "Α"),
    ("Έ", "Ε"),
    ("Ή", "Η"),
    ("Ί", "Ι"),
    ("Ό", "Ο"),
    ("Ύ", "Υ"),
    ("Ώ", "Ω"),
    ("Ϊ", "Ι"),
    ("Ϋ", "Υ"),
)


def fold(text: str) -> str:
    """Strip Greek tonos and dialytika. Non-Greek text passes through unchanged."""
    if not text:
        return text
    for accented, bare in FOLD_PAIRS:
        text = text.replace(accented, bare)
    return text


def search_fold(text: str | None) -> str:
    """fold() plus lower case and one sigma: the form both sides of a LIKE are compared in.

    The FTS indexes fold accents in SQL and case-fold in the tokenizer, but the
    LIKE lookups on names, topics and extracted text did neither for Greek:
    SQLite's LIKE folds ASCII case only, and people are mostly stored in ALL-CAPS
    Greek, so "Παπαδόπουλος" never matched "ΠΑΠΑΔΟΠΟΥΛΟΣ". Final sigma is merged
    into sigma because a lower-cased capital and a typed word disagree on it.
    NFC first: text from PDFs and macOS arrives decomposed (alpha plus a
    combining acute), which the FTS tokenizer folds and fold() would not.
    """
    if not text:
        return ""
    return fold(unicodedata.normalize("NFC", text)).lower().replace("ς", "σ")


# Words too common to carry a search on their own, including the question words
# agents open with. The any-token fallback drops them. Stored folded.
STOPWORDS = frozenset(
    search_fold(word)
    for word in (
        "και", "της", "του", "των", "για", "στο", "στη", "στην", "στον", "στα",
        "από", "που", "με", "να", "τα", "το", "τη", "την", "τον", "οι", "ένα",
        "μια", "είναι", "θα", "δεν", "τους", "τις", "στις", "στους", "ότι",
        "πώς", "τι", "ποιος", "ποια", "ποιο", "ποιες", "ποιοι", "όταν",
        "σχετικά", "έχει", "είχε", "ήταν", "αυτό", "αυτή", "αυτά", "μας", "σας",
        "the", "and", "for", "with", "from", "that", "this", "are", "was", "not",
        "but", "you", "all", "any", "our", "what", "how", "when", "who", "which",
        "about", "did", "does", "have", "has", "there", "will", "can", "should",
        "would", "could", "they", "their", "them", "into", "also",
    )
)  # fmt: skip


def _strip_punctuation(word: str) -> str:
    """``word`` without leading or trailing punctuation (any Unicode P* category).

    Quotes of every kind, question marks (the Greek one is U+037E) and ellipses
    around a word said nothing about it and broke both the phrase and its tokens.
    """
    start, end = 0, len(word)
    while start < end and unicodedata.category(word[start]).startswith("P"):
        start += 1
    while end > start and unicodedata.category(word[end - 1]).startswith("P"):
        end -= 1
    return word[start:end]


def search_phrase(text: str | None) -> str:
    """The folded query as one phrase, punctuation stripped from each word."""
    words = (_strip_punctuation(word) for word in (text or "").split())
    return " ".join(search_fold(word) for word in words if word)


def search_tokens(text: str | None) -> list[str]:
    """Distinct folded tokens worth an any-token search, in query order.

    Three characters or more; a two-character word only when it is written as an
    acronym (UX, EU, ΔΤ) or carries a digit (Q4), because it is often the subject
    of the whole query; numbers only from five digits, so a reference number is
    kept and a year, which would match half the corpus, is not.
    """
    tokens = []
    for word in (text or "").split():
        raw = _strip_punctuation(word)
        token = search_fold(raw)
        if not token or token in STOPWORDS:
            continue
        if token.isdigit():
            keep = len(token) >= 5
        elif len(token) >= 3:
            keep = True
        else:
            keep = len(token) == 2 and (raw.isupper() or any(c.isdigit() for c in raw))
        if keep:
            tokens.append(token)
    return list(dict.fromkeys(tokens))


# sb_match's score for a row holding the whole query: above any token count.
PHRASE_MATCH = 1 << 20


@lru_cache(maxsize=64)
def _token_pattern(joined_tokens: str) -> re.Pattern | None:
    """One regex for a query's tokens, each matched at the start of a word.

    A token used to count anywhere inside a word, so 'act' matched 'contract'.
    At the start it still takes Greek inflection (καρτ-ες, καρτ-ων). A token
    under three characters is an acronym and must be the whole word.
    """
    tokens = sorted((t for t in joined_tokens.split("\x1f") if t), key=len, reverse=True)
    if not tokens:
        return None
    parts = (re.escape(t) + (r"(?!\w)" if len(t) < 3 else "") for t in tokens)
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + ")")


def _match_score(text: str | None, phrase: str, joined_tokens: str) -> int:
    """PHRASE_MATCH if the folded text holds ``phrase``, else how many tokens it holds.

    One function, so a search that falls back from the whole query to its tokens
    folds each row once, in one pass over the table: the fold runs in Python and
    a second pass cost as much again. The phrase is a substring test, as the
    LIKE it replaced was.
    """
    folded = search_fold(text)
    if phrase and phrase in folded:
        return PHRASE_MATCH
    pattern = _token_pattern(joined_tokens)
    return len(set(pattern.findall(folded))) if pattern else 0


def register_sql_functions(conn) -> None:
    """sb_fold(text) and sb_match(text, phrase, tokens) for the LIKE-based lookups.

    Registered by every connection the store opens (schema.create_database and
    get_connection), and again, lazily, by the entry points that use them. A
    connection that has them is left alone: create_function fails while any of
    its statements is mid-iteration. Unlike the generated FTS columns, which must
    stay pure SQL, these only ever run inside a query issued by this code.
    """
    try:
        conn.execute("SELECT sb_match('', '', '')").fetchone()
        return
    except sqlite3.OperationalError:
        pass
    conn.create_function("sb_fold", 1, search_fold, deterministic=True)
    conn.create_function("sb_match", 3, _match_score, deterministic=True)


def fold_sql_expr(column: str) -> str:
    """A nested replace() chain equivalent to fold(), for use in SQL.

    Pure SQL on purpose. These expressions live in GENERATED columns that SQLite
    evaluates on its own, including during an FTS5 'rebuild', so they cannot
    depend on a Python function being registered by whichever process happens to
    open the database.
    """
    expr = column
    for accented, bare in FOLD_PAIRS:
        expr = f"replace({expr},'{accented}','{bare}')"
    return expr
