"""Greek->Latin transliteration + name canonicalization for entity resolution.

Bridges the two failure modes exact-match dedup misses:
  * name-order reversal — "Παπαδόπουλος Νίκος" vs "Νίκος Παπαδόπουλος"
  * Greek<->Latin script variants — "Νίκος Παπαδόπουλος" vs "Nikos Papadopoulos"

Transliteration is used ONLY to build a matching key. Imperfect transliteration
reduces recall (a missed merge) but never precision (a wrong merge), because
merges stay gated on token-multiset equality *and* email compatibility upstream.
"""

import re
import unicodedata
from typing import NamedTuple

# Digraphs first (longest-match) on already-lowercased, accent-stripped text.
# The av/af and ev/ef distinctions collapse (fine for a matching key).
_GREEK_DIGRAPHS = [
    ("ου", "ou"),
    ("αυ", "av"),
    ("ευ", "ev"),
    ("γγ", "ng"),
    ("γκ", "gk"),
    ("γχ", "nch"),
    ("μπ", "b"),
    ("ντ", "nt"),
    ("τσ", "ts"),
    ("τζ", "tz"),
]

_GREEK_SINGLE = {
    "α": "a",
    "β": "v",
    "γ": "g",
    "δ": "d",
    "ε": "e",
    "ζ": "z",
    "η": "i",
    "θ": "th",
    "ι": "i",
    "κ": "k",
    "λ": "l",
    "μ": "m",
    "ν": "n",
    "ξ": "x",
    "ο": "o",
    "π": "p",
    "ρ": "r",
    "σ": "s",
    "ς": "s",
    "τ": "t",
    "υ": "y",
    "φ": "f",
    "χ": "ch",
    "ψ": "ps",
    "ω": "o",
}


def _strip_accents(s: str) -> str:
    """Drop combining marks (Greek tonos, Latin diacritics) via NFD."""
    if s.isascii():  # no mark to drop, and half the names in a store are plain Latin
        return s
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return unicodedata.normalize("NFC", s)


def transliterate_greek(text: str) -> str:
    """Best-effort Greek->Latin for matching keys (lowercased, accent-stripped).

    Latin input passes through unchanged (its Greek-letter set is empty).
    """
    s = _strip_accents(text.lower())
    for gr, la in _GREEK_DIGRAPHS:
        s = s.replace(gr, la)
    return "".join(_GREEK_SINGLE.get(ch, ch) for ch in s)


def canonical_name(name: str) -> str:
    """Order- and script-independent key for a person name.

    lowercase -> strip accents -> transliterate Greek -> keep [a-z0-9] tokens ->
    drop 1-char tokens (initials/noise) -> sort -> join. So "Παπαδόπουλος Νίκος",
    "Νίκος Παπαδόπουλος" and "Nikos Papadopoulos" all yield "nikos papadopoulos".
    """
    tokens = re.findall(r"[a-z0-9]+", transliterate_greek(name))
    tokens = [t for t in tokens if len(t) > 1]
    return " ".join(sorted(tokens))


# The Latin spellings a Greek letter or pair takes, each collapsed to one form, in
# this order, so a name romanised by hand ('Mprostopoulou', 'Brostopulu') and the
# same name transliterated from the Greek meet on one key: μπ is B or Mp, ου is Ou
# or U, η, ι and υ are I or Y, χ is Ch, Kh or H, φ is F or Ph, ξ is X or Ks, ντ is
# Nt or D, γκ is Gk or G, and γγ is Ng or Gg. θ (Th) and ψ (Ps) have one spelling
# already. 'nt' before an 'h' is ν and θ, not ντ. A matching key only, like
# transliterate_greek: two names it joins are two candidates a resolver ranks,
# never a merge.
_SPELLING_VARIANTS = (
    (re.compile(r"m[pb]"), "b"),
    (re.compile(r"gg"), "ng"),
    (re.compile(r"gk"), "g"),
    (re.compile(r"nt(?!h)"), "d"),
    (re.compile(r"ou"), "u"),
    (re.compile(r"[ck]h"), "h"),
    (re.compile(r"ph"), "f"),
    (re.compile(r"ks"), "x"),
    (re.compile(r"y"), "i"),
    (re.compile(r"[eo]i"), "i"),
    (re.compile(r"([a-z])\1+"), r"\1"),  # doubled letters: Anna, Kallas, Pappas
)

# What a key keeps of a transliterated word: Latin letters and digits, nothing else.
_NOT_IN_KEY = re.compile(r"[^a-z0-9]+")

# Titles written before a name, folded (no accents, σ for ς): they are no part of it.
_TITLES = frozenset(
    {"κ", "κα", "κοσ", "κκ", "κυρ", "κυρια", "κυριοσ", "δρ", "mr", "mrs", "ms", "miss", "dr"}
)


def spelling_key(word: str) -> str:
    """One word's key across scripts and romanisations: 'Μπροστοπούλου' and 'Brostopulu' share one.

    Transliterated as transliterate_greek does, then the common Latin variants of
    each Greek letter collapsed (_SPELLING_VARIANTS). A word of two letters is
    left as it is, since 'AI' collapsed would be 'e', an initial; a single letter
    is collapsed, so the initial 'Χ.' is 'h', as the key of 'Χαρίλαος' starts.
    """
    return collapse_spelling(transliterate_greek(word), single_letter=len(word) == 1)


def collapse_spelling(transliterated: str, *, single_letter: bool = False) -> str:
    """spelling_key of a word already put through transliterate_greek, for a caller that
    needs both and would otherwise transliterate twice."""
    key = _NOT_IN_KEY.sub("", transliterated)
    if len(key) > 2 or single_letter:
        for pattern, replacement in _SPELLING_VARIANTS:
            key = pattern.sub(replacement, key)
    return key


class NameWord(NamedTuple):
    """A word of a name: its spelling key, and whether it was written as an initial."""

    key: str
    initial: bool


def name_tokens(name: str | None) -> list[str]:
    """The words of a name in lower case, accents off and σ for ς, titles left out."""
    folded = _strip_accents((name or "").lower()).replace("ς", "σ")
    return [word for word in re.findall(r"\w+", folded) if word not in _TITLES]


def name_words(name: str | None) -> list[NameWord]:
    """The words of a name as spelling keys, titles left out: 'κα Οκαπίδου' is one word."""
    words = []
    for token in name_tokens(name):
        key = spelling_key(token)
        if key:
            words.append(NameWord(key, len(token) == 1))
    return words


def name_keys(name: str | None) -> list[str]:
    """name_words' keys alone."""
    return [w.key for w in name_words(name)]


def _stem(key: str) -> str:
    """A key without the final s of a nominative: 'Okapidis' and 'Okapidi' share 'okapidi'."""
    return key[:-1] if len(key) >= 4 and key.endswith("s") else key


def same_word(a: NameWord, b: NameWord) -> bool:
    """Whether two whole words are one word, case endings aside, at four letters or more."""
    stem = _stem(a.key)
    return len(stem) >= 4 and not a.initial and not b.initial and stem == _stem(b.key)


def words_agree(a: NameWord, b: NameWord) -> bool:
    """Whether two words of names can be the same word of one person's name.

    The same key; an initial and a word it starts; the same stem ('Okapidis' and its
    vocative 'Okapidi'); or one stem starting the other at six letters or more, the
    colloquial and formal forms of a forename ('Grigoris', 'Grigorios'). Shorter
    prefixes are other names ('Maria', 'Marianna'), and so is a feminine form,
    whose stem does not start the masculine one ('Grigoria').
    """
    if a.key == b.key:
        return True
    if a.initial or b.initial:
        initial, word = (a, b) if a.initial else (b, a)
        return word.key.startswith(initial.key)
    x, y = sorted((_stem(a.key), _stem(b.key)), key=len)
    return x == y or (len(x) >= 6 and y.startswith(x))


def _covered(words: list[NameWord], by: list[NameWord]) -> bool:
    """Whether every word of `words` agrees with a word of `by`.

    An initial in `by` stands for a word only when `by` spells out no word with its
    letter: in 'E. Surname (Evanthia)', or at a.surname@ for 'Aikaterini Surname', the
    initial is that word already and stands for no other, so 'Eleni Surname' and
    'Andreadou Aikaterini' are someone else. names_agree also tries the other way
    round, where plain 'E. Surname' is covered by 'Eva Surname'.
    """
    full = [b for b in by if not b.initial]
    for w in words:
        if any(words_agree(w, b) for b in full):
            continue
        if not any(
            b.initial and words_agree(w, b) and not any(x.key.startswith(b.key) for x in full)
            for b in by
        ):
            return False
    return True


def names_agree(
    a: list[NameWord], b: list[NameWord], b_extra: list[NameWord] | tuple[NameWord, ...] = ()
) -> bool:
    """Whether two names can be one person's: a shared surname and no word against it.

    They share a whole word of four letters or more (same_word), and every word of
    one has an agreeing word in the other (words_agree): 'Evi Brostopoulou' and
    'E. Brostopoulou (Evanthia)', or 'Brostopoulou Evanthia Maria' and
    'E. Brostopoulou'. 'Maria Brostopoulou' and 'E. Brostopoulou' do not.
    `b_extra` are words that may stand for b without being its name, such as the
    words of its address ('brostopoulou.evi').
    """
    a = [w for w in a if not w.key.isdigit()]
    b = [w for w in b if not w.key.isdigit()]
    extra = [w for w in b_extra if not w.key.isdigit()]
    if not a or not b or not any(same_word(x, y) for x in a for y in [*b, *extra]):
        return False
    return _covered(a, [*b, *extra]) or _covered(b, a)
