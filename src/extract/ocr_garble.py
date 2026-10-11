"""Greek read as Latin look-alike letters: a detector for OCR text, and the mark it leaves.

Tesseract with English first reads many Greek letters as the Latin letters they look like:
«και» comes out as "kal", «του» as "tou", «την» as "tnv", the capitals «ΤΟΥ» as "TOY". About
800 images and scans in one store became such text, and the summaries made from it named the
wrong programme and the wrong figure (audit 2026-10-11, data-quality-03). A text is taken for
garbled Greek when at most GREEK_SHARE_MAX of its letters are Greek and Greek function words
appear in their Latin look-alike spellings: at least GARBLE_MIN_HITS of them, at least two
different ones, and at least GARBLE_MIN_DENSITY per 1,000 Latin letters. The last two keep a
name used three times ("Kai") and a few chance matches in a long English scan from counting.

Pure: the standard library only, no I/O. src/store/file_sweep.py builds its SQL from the mark, so
this module must not import the store or the extractors.
"""

import re
import unicodedata
from collections import Counter
from typing import NamedTuple

# A text at most this share Greek reads as Latin script.
GREEK_SHARE_MAX = 0.3
GARBLE_MIN_HITS = 3
GARBLE_MIN_DISTINCT = 2
GARBLE_MIN_DENSITY = 2.0

# What the mark says, in attachment_content.extraction_error. Rows carrying it are not
# summarised, and the sweep keeps their files (src/store/file_sweep.py OCR_GARBLED_SQL). It
# must not contain "left unread" or "file kept", which mean other things to the sweep.
OCR_GARBLED = "ocr_garbled"
OCR_GARBLED_NOTE = f"{OCR_GARBLED}: Greek read as Latin look-alike letters"

# Frequent Greek function words as an English-first OCR spells them, lower case and without
# accents. Forms that are common words in English or another Latin-script language ("to", "me",
# "the", "ton", "va") are left out on purpose: those Greek words are frequent, but the detector
# must not fire on English.
LOOKALIKE_FUNCTION_WORDS = frozenset(
    {
        "kai",  # και
        "kal",
        "tou",  # του
        "toy",  # ΤΟΥ in capitals
        "toug",  # τους
        "tng",  # της
        "tns",
        "tnc",
        "tnv",  # την
        "thn",
        "twv",  # των
        "tov",  # τον
        "tig",  # τις
        "yia",  # για
        "yla",
        "amo",  # από
        "oto",  # στο
        "otn",  # στη
        "otnv",  # στην
        "othn",
        "otov",  # στον
        "otig",  # στις
        "oti",  # ότι
        "eival",  # είναι
        "eivai",
        "elval",
        "elvai",
        "einai",
        "ntav",  # ήταν
        "exei",  # έχει
        "exel",
        "exouv",  # έχουν
        "npog",  # προς
        "mpog",
        "evw",  # ενώ
        "onwg",  # όπως
        "omwg",
        "onoia",  # οποία
        "omoia",
    }
)

_LATIN_WORD = re.compile(r"[a-z]+")


class ScriptProfile(NamedTuple):
    """How many of a text's letters are Greek and how many Latin."""

    greek: int
    latin: int

    @property
    def letters(self) -> int:
        return self.greek + self.latin

    @property
    def greek_share(self) -> float:
        return self.greek / self.letters if self.letters else 0.0

    @property
    def latin_share(self) -> float:
        return self.latin / self.letters if self.letters else 0.0


def _is_greek(c: str) -> bool:
    # The micro sign is what tesseract gives for «μ» in a Greek word.
    return "Ͱ" <= c <= "Ͽ" or "ἀ" <= c <= "῿" or c == "µ"


def _is_latin(c: str) -> bool:
    # Basic Latin, Latin-1, Latin Extended-A and -B, and Latin Extended Additional.
    return c <= "ɏ" or "Ḁ" <= c <= "ỿ"


def script_profile(text: str | None) -> ScriptProfile:
    """The Greek and Latin letters of `text`; digits, signs and other scripts are not counted."""
    greek = latin = 0
    for c in text or "":
        if not c.isalpha():
            continue
        if _is_greek(c):
            greek += 1
        elif _is_latin(c):
            latin += 1
    return ScriptProfile(greek, latin)


def lookalike_function_words(text: str | None) -> Counter[str]:
    """The Greek function words `text` holds in their Latin look-alike spellings, counted."""
    bare = "".join(
        c for c in unicodedata.normalize("NFD", text or "") if not unicodedata.combining(c)
    )
    return Counter(w for w in _LATIN_WORD.findall(bare.lower()) if w in LOOKALIKE_FUNCTION_WORDS)


def looks_like_garbled_greek(text: str | None) -> bool:
    """Whether `text` is Greek read as Latin look-alike letters. See the module docstring."""
    profile = script_profile(text)
    if not profile.latin or profile.greek_share > GREEK_SHARE_MAX:
        return False
    hits = lookalike_function_words(text)
    found = sum(hits.values())
    return (
        found >= GARBLE_MIN_HITS
        and len(hits) >= GARBLE_MIN_DISTINCT
        and found * 1000 >= GARBLE_MIN_DENSITY * profile.latin
    )


def is_garbled(error: str | None) -> bool:
    """Whether an extraction error carries the mark."""
    return OCR_GARBLED in (error or "")


def mark_garbled(error: str | None) -> str:
    """The error with the mark added once, after what it already said."""
    if is_garbled(error):
        return error or ""
    return f"{error}; {OCR_GARBLED_NOTE}" if error else OCR_GARBLED_NOTE
