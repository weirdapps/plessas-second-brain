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
# agents open with. The any-token fallback drops them, and a row need not hold
# them to hold the whole query. Stored folded.
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
        # Two-letter function words, which the acronym rule would otherwise keep
        # from an ALL-CAPS query. Not "it": IT is a department.
        "σε", "σαν", "ως", "αν", "is", "of", "or", "to", "in", "on", "at", "by",
        "an", "as", "be", "we", "if", "do", "so", "no",
        # One-letter articles, so a question is not whole only where they appear.
        "ο", "η", "a", "i",
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
        if word[end - 1] == "#" and end - start == 2 and word[start].isalpha():
            break  # C#, F#: without the sign it is a letter every row holds
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
            keep = (
                len(token) == 2
                and token.isalnum()
                and (raw.isupper() or any(c.isdigit() for c in raw))
            )
        if keep:
            tokens.append(token)
    return list(dict.fromkeys(tokens))


def is_stopword(word: str) -> bool:
    """Whether a query word is one a row need not hold.

    A capital letter is never one: 'A' in 'Series A' and 'I' in 'Basel I' name a
    variant, where 'a' and 'i' are an article and a pronoun.
    """
    raw = _strip_punctuation(word)
    capital = len(raw) == 1 and raw.isascii() and raw.isupper()
    return not capital and search_fold(raw) in STOPWORDS


def search_words(text: str | None) -> list[str]:
    """Distinct folded words a row must hold, in any order, to hold the whole query.

    Every word but the stopwords and bare symbols ('+', '->'). Unlike
    search_tokens this keeps years and short words: they are too common to find a
    row by, but a row without the query's year does not answer it.
    """
    words = []
    for word in (text or "").split():
        folded = search_fold(_strip_punctuation(word))
        if any(c.isalnum() for c in folded) and not is_stopword(word):
            words.append(folded)
    return list(dict.fromkeys(words))


# sb_match's score for a row holding the whole query: above any token count.
PHRASE_MATCH = 1 << 20


def _word_end(word: str) -> str:
    """What must follow a folded query word where it matches.

    Nothing, for a word of three letters or more: it matches at the start of a
    longer one, for Greek inflection (καρτ-ες, καρτ-ων). An acronym or code under
    three letters must be the whole word, and a number the whole number, or 500
    matched 5000 and the Greek 500.000.
    """
    if word[-1].isdigit():
        return r"(?![.,]?\d)" + (r"(?!\w)" if len(word) < 3 else "")
    if len(word) < 3:
        return r"(?!\w)"
    return ""


def _word_start(word: str) -> str:
    """What must come before a query word: a non-word character, and not a
    digit and a separator before a number, or 500 is the end of 1.500."""
    return r"(?<!\w)" + (r"(?<!\d[.,])" if word[:1].isdigit() else "")


def _at_word_start(word: str) -> str:
    return _word_start(word) + re.escape(word) + _word_end(word)


@lru_cache(maxsize=64)
def _phrase_pattern(phrase: str) -> re.Pattern:
    """The phrase at the start of a word, ending as its last word must.

    A plain substring test let a one-word query match inside other words, 'AI'
    in 'email' and 'UX' in 'Luxembourg', and 'digital EU' run into 'digital
    Europe'.
    """
    last = phrase.rsplit(" ", 1)[-1]
    return re.compile(_word_start(phrase) + re.escape(phrase) + _word_end(last))


@lru_cache(maxsize=64)
def _word_patterns(joined_words: str) -> tuple[re.Pattern, ...]:
    """One regex per word, at the start of a word, in the order of _forms.

    Each word is looked for on its own. One alternation, longest first, let a
    longer word hide a shorter one inside it ('cards' hid 'card', 'e-banking'
    hid 'banking'), so a row holding every word counted short. A token used to
    count anywhere inside a word, so 'act' matched 'contract'.
    """
    return tuple(re.compile(_at_word_start(w)) for w in _forms(joined_words))


@lru_cache(maxsize=128)
def _forms(joined: str) -> tuple[str, ...]:
    """The non-empty parts of a U+001F-joined argument, split once per query."""
    return tuple(f for f in joined.split("\x1f") if f)


def _match_score(text: str | None, phrase: str, words: str, joined_tokens: str) -> int:
    """PHRASE_MATCH if the folded text holds the whole query, else how many tokens it holds.

    The whole query is the phrase, or every one of `words` in any order. One
    function, so a search that falls back from the whole query to its tokens
    folds each row once, in one pass over the table: the fold runs in Python and
    a second pass cost as much again. A plain substring test runs first, in C,
    and rules out most rows; the regexes, which place a hit at the start of a
    word, run on the rest. The regexes alone cost about a microsecond more per
    row, a third of a second per recall. The tokens are among the words, so the
    words are only looked for in a row that holds every token.
    """
    folded = search_fold(text)
    if phrase and phrase in folded and _phrase_pattern(phrase).search(folded):
        return PHRASE_MATCH
    contains = folded.__contains__
    tokens = _forms(joined_tokens)
    if tokens:
        if not any(map(contains, tokens)):
            return 0
        patterns = _word_patterns(joined_tokens)
        found = sum(
            1 for t, p in zip(tokens, patterns, strict=True) if t in folded and p.search(folded)
        )
        if found < len(tokens):
            return found
    required = _forms(words)
    if required and all(map(contains, required)):
        if all(p.search(folded) for p in _word_patterns(words)):
            return PHRASE_MATCH
    return len(tokens)


def register_sql_functions(conn) -> None:
    """sb_fold(text) and sb_match(text, phrase, words, tokens) for the LIKE-based lookups.

    Registered by every connection the store opens (schema.create_database and
    get_connection), and again, lazily, by the entry points that use them. A
    connection that has them is left alone: create_function fails while any of
    its statements is mid-iteration. Unlike the generated FTS columns, which must
    stay pure SQL, these only ever run inside a query issued by this code.
    """
    try:
        conn.execute("SELECT sb_match('', '', '', '')").fetchone()
        return
    except sqlite3.OperationalError:
        pass
    conn.create_function("sb_fold", 1, search_fold, deterministic=True)
    conn.create_function("sb_match", 4, _match_score, deterministic=True)


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
