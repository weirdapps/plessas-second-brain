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
