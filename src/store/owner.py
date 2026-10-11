"""Whether an address or a name is the owner's, and which people row is the owner.

Shared by the calendar loader (whose meetings are the owner's) and the source classes (which
mail the owner's jobs sent him). Both used a case-blind substring test, which took
'owner@example.com.example.net' and 'notowner@example.com' for 'owner@example.com': anyone
holding such an address could pass mail off as the owner's own.

The person resolver (src/store/person_resolve.py) maps every spelling of the owner to one
row through here: the store held the owner as 64 people rows (his address, an alias
address, bounce rewrites, his name with and without titles, in both scripts), so a
question about him split across them.
"""

import sqlite3
from collections.abc import Iterable

from src.store.transliterate import name_words, same_word, words_agree

# What may stand before the pattern's local part in an address: 'first.owner@example.com'.
_SEPARATORS = ".-_+"


def is_owner_address(address: str | None, pattern: str | None) -> bool:
    """Whether `address` is the owner's, case-blind.

    The pattern's domain, when it names one, is the address's whole domain; its local part ends
    the address's local part, starting it or following a separator. So 'owner@example.com'
    matches 'owner@example.com' and 'first.owner@example.com', 'owner' and 'owner@' match an
    'owner' local part at any domain, and '@example.com' matches the whole domain, as the
    substring test did. An empty pattern matches no one: '' is inside every string.
    """
    pattern = (pattern or "").strip().lower()
    address = (address or "").strip().lower()
    if not pattern or not address:
        return False
    local, at, domain = pattern.partition("@")
    address_local, _, address_domain = address.partition("@")
    if domain and address_domain != domain:
        return False
    if not local:
        return bool(at)
    if not address_local.endswith(local):
        return False
    before = address_local[: -len(local)]
    return not before or before[-1] in _SEPARATORS


def is_owner_name(name: str | None, user_name: str | None) -> bool:
    """Whether `name` is a spelling of the owner's name, BRAIN_USER_NAME ('First Last').

    It holds the owner's surname, the last word of user_name, in either script, in
    any common romanisation and in its vocative ('Okapidi' for 'Okapidis'), and every
    other word agrees with one of the owner's forenames: an initial of one, its
    colloquial form ('Grigoris' for 'Grigorios'), or the same word. Titles ('κα',
    'Mrs') are no word. So 'Okapidou T.' and 'κα Οκαπίδου' are the owner Theano
    Okapidou, and 'Danae Okapidou' or 'Okapidou Team' are not. The surname alone is
    the owner's: in his own store that is who it means.
    """
    owner = name_words(user_name)
    if not owner or owner[-1].initial:
        return False
    surname, forenames = owner[-1], owner[:-1]
    words = name_words(name)
    rest = [w for w in words if not same_word(w, surname)]
    if len(rest) == len(words):
        return False
    return all(any(words_agree(w, f) for f in forenames) for w in rest)


def owner_person_id(
    conn: sqlite3.Connection, by_address: Iterable[int], by_name: Iterable[int]
) -> int | None:
    """The one people row every spelling of the owner resolves to, or None.

    The most-emailed of the rows holding an owner address (`by_address`, by
    is_owner_address); failing those, the most-emailed of the rows named as the
    owner (`by_name`, by is_owner_name) that hold an address. Ties go to the lower
    id, so the choice is stable from one call to the next.
    """
    for ids in (list(by_address), list(by_name)):
        if not ids:
            continue
        marks = ",".join("?" * len(ids))
        row = conn.execute(
            f"""
            SELECT p.id FROM people p
            WHERE p.id IN ({marks}) AND p.email IS NOT NULL
            ORDER BY (SELECT COUNT(*) FROM email_people ep WHERE ep.person_id = p.id) DESC, p.id
            LIMIT 1
            """,
            ids,
        ).fetchone()
        if row:
            return row[0]
    return None
