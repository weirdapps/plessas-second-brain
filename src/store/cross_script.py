"""A name typed in Latin letters is searched in Greek letters too, and the reverse.

Most of the mail is Greek and most queries are not: on the usage log of 2026-09-05
to 10-16, 53% of query text was English or Latin only and 27% mixed, and about 60
retries were an agent translating a surname from Latin into Greek letters by hand.
Full-text search folds case and accents but cannot cross scripts, so 'Okapidis'
missed every email that writes 'Οκαπίδης'. Transliteration served only person
resolution (src/store/person_resolve.py).

So a query word of four letters or more, in one script, that is a word of a name the
people table holds in the other script (the same spelling key, transliterate.py) is
searched in those forms too, as alternatives of the word: '("Okapidis" OR "οκαπιδ"*)'
(src/store/query.py). A Greek form is searched by its stem where it has one, as a
Greek query word is (greek.query_stem); a Latin form as it is written. At most
MAX_FORMS forms a word, the most frequent among the names first. A word no name holds
in the other script is searched as before, which is nearly every word that is not a
name: of 180 common query words on one store, 3 had a form, all three the word's own
transliteration (a system's name, an English loan word and a first name).

The forms are built once per store and process from the people index person_resolve
keeps (about 0.2 s for 34K people), and again when the people table changes.
BRAIN_QUERY_NAMES=0 turns it off for one deployment.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass

from src.store.transliterate import collapse_spelling, name_tokens, transliterate_greek

# The most other-script forms a query word is searched by.
MAX_FORMS = 3
# The fewest letters a word must have to be looked up, or a form to be used.
MIN_LETTERS = 4

_GREEK = re.compile(r"[α-ω]+")
_LATIN = re.compile(r"[a-z]+")


def cross_script_names() -> bool:
    """Whether name words are searched in the other script: unless BRAIN_QUERY_NAMES is 0.
    Read at each call, so a test, an eval or a process can change it."""
    return os.environ.get("BRAIN_QUERY_NAMES", "").strip() != "0"


def _key(token: str) -> str:
    return collapse_spelling(transliterate_greek(token))


def _script(token: str) -> str | None:
    if len(token) < MIN_LETTERS:
        return None
    if _GREEK.fullmatch(token):
        return "greek"
    if _LATIN.fullmatch(token):
        return "latin"
    return None


@dataclass(frozen=True)
class NameForms:
    """Spelling key -> the name words in each script, most frequent first."""

    greek: dict[str, tuple[str, ...]]
    latin: dict[str, tuple[str, ...]]

    def alternatives(self, word: str) -> tuple[str, ...]:
        """The other script's forms of a query word, folded; () for a word no name holds
        in the other script, a stopword, a short word, or one mixing scripts or digits."""
        from src.store.greek import is_stopword

        if is_stopword(word):
            return ()
        tokens = name_tokens(word)
        if len(tokens) != 1:
            return ()
        script = _script(tokens[0])
        if script is None:
            return ()
        other = self.latin if script == "greek" else self.greek
        return other.get(_key(tokens[0]), ())


def _build(names) -> NameForms:
    counts: dict[str, dict[str, Counter]] = {"greek": {}, "latin": {}}
    for name in names:
        for token in name_tokens(name):
            script = _script(token)
            if script:
                counts[script].setdefault(_key(token), Counter())[token] += 1

    def ranked(by_key: dict[str, Counter]) -> dict[str, tuple[str, ...]]:
        return {
            key: tuple(word for word, _n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))[
                :MAX_FORMS
            ]
            for key, c in by_key.items()
        }

    return NameForms(greek=ranked(counts["greek"]), latin=ranked(counts["latin"]))


# Store path -> (the people index the forms were built from, the forms), for the last
# few stores a process read, as person_resolve keeps its indexes.
_CACHE: dict[str, tuple[object, NameForms]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_SIZE = 4


def name_forms(conn: sqlite3.Connection) -> NameForms | None:
    """The name forms of conn's store, or None when BRAIN_QUERY_NAMES is 0.

    Built from person_resolve.people_index, which a process keeps per store until the
    people table changes; the forms are kept as long as that index is. A store with no
    people table has empty forms.
    """
    if not cross_script_names():
        return None
    from src.store.person_resolve import _store_path, people_index

    try:
        index = people_index(conn)
    except sqlite3.OperationalError:  # a store without a people table
        return NameForms(greek={}, latin={})
    path = _store_path(conn)
    with _CACHE_LOCK:
        hit = _CACHE.get(path) if path else None
    if hit is not None and hit[0] is index:
        return hit[1]
    forms = _build(index.names.values())
    if path:
        with _CACHE_LOCK:
            _CACHE.pop(path, None)
            _CACHE[path] = (index, forms)
            while len(_CACHE) > _CACHE_SIZE:
                _CACHE.pop(next(iter(_CACHE)))
    return forms
