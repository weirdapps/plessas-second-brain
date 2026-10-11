"""Whether an address or a name is the owner's, and which people row is the owner.

Shared by the calendar loader (whose meetings are the owner's), the source classes (which
mail the owner's jobs sent him), stale threads, the extraction eval and the person
resolver. They used a case-blind substring test, which took
'owner@example.com.example.net' and 'notowner@example.com' for 'owner@example.com', and
then a match at the end of the address, which still took a colleague at
'k.owner@example.com' for the owner: anyone holding such an address could pass mail off as
the owner's own. Now BRAIN_USER_EMAIL_PATTERN lists the owner's addresses and each is
matched whole.

The person resolver (src/store/person_resolve.py) maps every spelling of the owner to one
row through here: the store held the owner as 64 people rows (his address, an alias
address, bounce rewrites, his name with and without titles, in both scripts), so a
question about him split across them. That row is always one at an address he lists: a
display name is anyone's to write.
"""

import re
import sqlite3
from collections.abc import Iterable
from functools import lru_cache

from src.store.transliterate import name_words, same_word, words_agree

# What separates the owner's addresses in BRAIN_USER_EMAIL_PATTERN.
_LIST_SEPARATORS = re.compile(r"[\s,]+")


@lru_cache(maxsize=16)
def owner_addresses(pattern: str | None) -> tuple[frozenset[str], frozenset[str]]:
    """The whole addresses and the bare local parts BRAIN_USER_EMAIL_PATTERN lists.

    `pattern` holds the owner's addresses, primary and aliases, separated by commas or
    whitespace: 'first.last@example.com, alias@example.org'. An entry without a domain
    ('owner' or 'owner@') is that local part at any domain. An entry without a local
    part ('@example.com') is dropped: a domain alone would make every colleague the owner.
    """
    addresses: set[str] = set()
    local_parts: set[str] = set()
    for entry in _LIST_SEPARATORS.split((pattern or "").strip().lower()):
        local, _, domain = entry.partition("@")
        if not local:
            continue
        if domain:
            addresses.add(entry)
        else:
            local_parts.add(local)
    return frozenset(addresses), frozenset(local_parts)


def is_owner_address(address: str | None, pattern: str | None) -> bool:
    """Whether `address` is one of the owner's, as `pattern` lists them, case-blind.

    A listed address must be the whole address, and a listed bare local part the whole
    local part, at any domain (owner_addresses). Nothing else: matched at the end,
    'owner@example.com' made a colleague at 'first.owner@example.com' the owner. An
    empty pattern matches no one: '' is inside every string.
    """
    address = (address or "").strip().lower()
    addresses, local_parts = owner_addresses(pattern)
    if address in addresses:
        return True
    local, _, domain = address.partition("@")
    return bool(domain) and local in local_parts


def owner_address_sql(column: str, pattern: str | None) -> tuple[str, list[str]]:
    """is_owner_address as a SQL condition on `column`, with its parameters.

    For queries that select the owner's mail in SQL rather than row by row. The condition
    is '0', true of nothing, when the pattern lists no address.
    """
    addresses, local_parts = owner_addresses(pattern)
    value = f"LOWER(TRIM(COALESCE({column}, '')))"
    clauses: list[str] = []
    params: list[str] = []
    if addresses:
        clauses.append(f"{value} IN ({', '.join('?' * len(addresses))})")
        params.extend(sorted(addresses))
    for local in sorted(local_parts):
        clauses.append(f"{value} LIKE ? ESCAPE '\\'")
        escaped = local.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"{escaped}@_%")
    return ("(" + " OR ".join(clauses) + ")" if clauses else "0"), params


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


def owner_person_id(conn: sqlite3.Connection, by_address: Iterable[int]) -> int | None:
    """The one people row every spelling of the owner resolves to, or None.

    The most-emailed of the rows at one of his addresses (`by_address`, by
    is_owner_address), ties to the lower id so the choice is stable. Never a row found by
    name alone: a display name is anyone's to write, an address he lists is his. With no
    row at his addresses there is no owner row, and rows named as him are ordinary people.
    """
    ids = list(by_address)
    if not ids:
        return None
    row = conn.execute(
        f"""
        SELECT p.id FROM people p
        WHERE p.id IN ({",".join("?" * len(ids))}) AND p.email IS NOT NULL
        ORDER BY (SELECT COUNT(*) FROM email_people ep WHERE ep.person_id = p.id) DESC, p.id
        LIMIT 1
        """,
        ids,
    ).fetchone()
    return row[0] if row else None
