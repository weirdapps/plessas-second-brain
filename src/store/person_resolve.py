"""Who a name or an address means: one identity, how it was found, and who else it could be.

The people table mixes identities, rows with an address, a Teams MRI or a WhatsApp JID,
with the mentions extraction made of names written in text, which hold none (78% of the
rows of the store this was measured on). A name matched by folded substring, and won by
the most-emailed row, landed on a mention as often as not: a colleague's dossier said no
Teams messages and no meetings because the mention 'Firstname Surname' matched while
the addressed row, written 'F. Surname', held the history; the Latin and the Greek
spelling of one person attached different dossiers; and by display name 43% of a
month's meeting attendees resolved to no one, against 26% by address
(retrieval-quality-02, consumers-03, code-store-11). So, in order:

1. An address resolves exactly (resolved_by 'email', or 'jid' for a WhatsApp JID), and
   so does a Teams MRI ('mri'). Every address BRAIN_USER_EMAIL_PATTERN lists and every
   spelling of BRAIN_USER_NAME resolves to the owner's one row (src/store/owner.py), by
   'email' or by 'alias'. That row is one at a listed address, never one found by name,
   and only address-less rows join it by name: a display name is anyone's to write.
2. A name finds every row whose words it starts, in either script and in the common
   romanisations of Greek (transliterate.spelling_key), in any order. With local_part,
   one Latin word of four letters or more also finds the addresses whose local part it
   starts, as it always has.
3. A row the alias map points elsewhere stands for its target: sync_metadata rows
   'person_alias:<people id>' -> '<people id>', recorded after review by
   src/store/person_aliases.py. A table would be a migration, and the identity split of
   issue 170 is where one belongs; the rows reach a replica with its next pull.
4. Every identity whose name shares the query's surname and contradicts none of its
   words is a candidate too, matched or not, its address's words counting as its own:
   'Evi Surname' fits 'E. Surname (Evanthia)' at evi.surname@, 'Maria Surname' never
   does (transliterate.names_agree).
5. Identities outrank mentions, then the most-emailed wins: resolved_by 'name', or
   'alias' when the winner was reached only through the map or the owner rule.

match_count is how many people the name can mean, rows the map or the owner rule join
counting once, and other_candidates the next three, each with its identifier kind
('email', 'mri', 'jid', or None for a mention).

The words of every name are indexed once per store and process (about 0.2 s for 34K
people) and reused until the people table changes, so a lookup takes milliseconds
where the folded scan took 45-55 ms.
"""

import json
import re
import sqlite3
import threading
from bisect import bisect_left
from dataclasses import dataclass, field
from functools import lru_cache

from src import config
from src.store.greek import register_sql_functions, search_fold
from src.store.owner import is_owner_address, is_owner_name, owner_person_id
from src.store.transliterate import (
    NameWord,
    collapse_spelling,
    name_tokens,
    name_words,
    names_agree,
    spelling_key,
    transliterate_greek,
)

ALIAS_PREFIX = "person_alias:"

# The kinds of resolution that are an identifier, not a name.
IDENTIFIER_KINDS = frozenset({"email", "mri", "jid"})

# A WhatsApp sender's JID, which the people table may hold where an address goes.
_JID_DOMAINS = ("@s.whatsapp.net", "@c.us", "@lid")
# A Teams MRI: '8:orgid:<object id>', '8:live:<name>'.
_MRI = re.compile(r"\d+:\S+")
# One Latin word of four letters or more may start an address's local part.
_LOCAL_QUERY = re.compile(r"[a-z0-9]{4,}")
# Where a word starts inside a local part: after any of these ('jane.roe', 'j-roe').
_LOCAL_SEPARATORS = "._-"
# How many stores' indexes a process keeps.
_CACHE_SIZE = 4


@dataclass(frozen=True)
class Resolution:
    """The person a query means, how many people it could mean, and how it was decided."""

    row: sqlite3.Row | None = None
    match_count: int = 0
    other_candidates: list[dict] = field(default_factory=list)
    resolved_by: str | None = None


@dataclass(frozen=True)
class _Index:
    names: dict[int, str]
    emails: dict[int, str]
    by_email: dict[str, int]
    kinds: dict[int, str]
    forms: list[str]
    rows_by_form: dict[str, tuple[int, ...]]
    local_heads: list[str]
    rows_by_local: dict[str, tuple[int, ...]]
    owner_id: int | None
    owner_rows: frozenset[int]
    # An identity's name and address words, filled in as lookups first need them.
    identity_words: dict[int, tuple[list[NameWord], list[NameWord]]] = field(default_factory=dict)


@lru_cache(maxsize=16384)
def _forms(token: str) -> tuple[str, ...]:
    """A name word as it is matched: as typed (folded), transliterated, and by spelling key.

    A query word in any form matches a name word in any form, so 'Μπροστοπ' starts
    'Brostopoulou' and 'Brostopulu' is 'Μπροστοπούλου'. Greek and Latin forms never
    meet by accident: they share no letter.
    """
    transliterated = transliterate_greek(token)  # a \w+ word: only '_' is not a letter
    key = collapse_spelling(transliterated, single_letter=len(token) == 1)
    return tuple(dict.fromkeys(f for f in (token, transliterated.replace("_", ""), key) if f))


@lru_cache(maxsize=8192)
def _row_forms(name: str) -> frozenset[str]:
    return frozenset(f for token in name_tokens(name) for f in _forms(token))


def _local_heads(local: str) -> set[str]:
    """The local part from each place a word may start, as resolve_person's LIKEs read it:
    the start, after one letter (an initial glued on: 'jroe'), and after a separator."""
    starts = {0, 1} | {i + 1 for i, ch in enumerate(local) if ch in _LOCAL_SEPARATORS}
    return {local[s:] for s in starts if s < len(local)}


def _local_words(address: str | None, name: list[NameWord]) -> list[NameWord]:
    """The words an address spells, as name words: 'brostopoulou.evi' is a surname and a
    forename, and 'ebrostopoulou' an initial glued to the surname the row's `name` holds."""
    local = (address or "").partition("@")[0]
    keys = {w.key for w in name if not w.initial}
    words = []
    for part in re.split(r"[._\-+]", local):
        part = "".join(ch for ch in part if ch.isalpha())
        if not part:
            continue
        key = spelling_key(part)
        words.append(NameWord(key, len(part) == 1))
        rest = spelling_key(part[1:])
        if len(part) >= 5 and key not in keys and rest in keys:
            words.append(NameWord(spelling_key(part[0]), True))
    return words


def _mri_people(conn: sqlite3.Connection) -> set[int]:
    """People a Teams MRI resolved to, or Teams messages were linked to."""
    ids: set[int] = set()
    for sql in (
        "SELECT DISTINCT person_id FROM teams_mri_resolution WHERE person_id IS NOT NULL",
        "SELECT DISTINCT sender_person_id FROM teams_messages WHERE sender_person_id IS NOT NULL",
    ):
        try:
            ids.update(row[0] for row in conn.execute(sql))
        except sqlite3.OperationalError:  # a store without the Teams tables
            pass
    return ids


def _build(conn: sqlite3.Connection) -> _Index:
    """Index every people row's words, address and identifier kind, and find the owner."""
    mri = _mri_people(conn)
    names: dict[int, str] = {}
    emails: dict[int, str] = {}
    by_email: dict[str, int] = {}
    kinds: dict[int, str] = {}
    by_form: dict[str, list[int]] = {}
    by_local: dict[str, list[int]] = {}
    pattern = config.USER_EMAIL_PATTERN
    owner_by_address: list[int] = []
    for pid, name, email in conn.execute("SELECT id, name, email FROM people ORDER BY id"):
        names[pid] = name = name or ""
        for token in name_tokens(name):
            for form in _forms(token):
                by_form.setdefault(form, []).append(pid)
        address = (email or "").strip().lower()
        if address:
            emails[pid] = address
            by_email.setdefault(address, pid)
            kinds[pid] = "jid" if address.endswith(_JID_DOMAINS) else "email"
            if "@" in address:
                for head in _local_heads(address.partition("@")[0]):
                    by_local.setdefault(head, []).append(pid)
            if pattern and is_owner_address(address, pattern):
                owner_by_address.append(pid)
        elif pid in mri:
            kinds[pid] = "mri"
    rows_by_form = {form: tuple(dict.fromkeys(ids)) for form, ids in by_form.items()}
    # The owner's row is one at an address he lists, never one found by name: a display
    # name is anyone's to write. Rows named as him join it only as mentions, with no
    # address, MRI or JID of their own; one at another address is someone else's.
    owner_id = owner_person_id(conn, owner_by_address)
    owner_by_name = [pid for pid in _owner_named(names, rows_by_form) if pid not in kinds]
    return _Index(
        names=names,
        emails=emails,
        by_email=by_email,
        kinds=kinds,
        forms=sorted(rows_by_form),
        rows_by_form=rows_by_form,
        local_heads=sorted(by_local),
        rows_by_local={head: tuple(dict.fromkeys(ids)) for head, ids in by_local.items()},
        owner_id=owner_id,
        owner_rows=frozenset(owner_by_address + owner_by_name)
        if owner_id is not None
        else frozenset(),
    )


def _owner_named(names: dict[int, str], rows_by_form: dict[str, tuple[int, ...]]) -> list[int]:
    """The rows whose name is a spelling of BRAIN_USER_NAME. Only rows holding the owner's
    surname are read: testing every name cost a third of a second."""
    user_name = config.USER_NAME
    owner = name_words(user_name)
    if not owner or owner[-1].initial:
        return []
    surname = owner[-1].key
    stem = surname[:-1] if len(surname) >= 4 and surname.endswith("s") else surname
    rows = {pid for form in (stem, stem + "s") for pid in rows_by_form.get(form, ())}
    return sorted(pid for pid in rows if is_owner_name(names[pid], user_name))


_CACHE: dict[str, tuple[tuple, _Index]] = {}
_CACHE_LOCK = threading.Lock()


def _signature(conn: sqlite3.Connection) -> tuple:
    """What the index depends on, cheaply (2 ms on 34K people): rows added, removed,
    renamed or given an address; MRIs linked; and the owner's settings."""
    people = tuple(
        conn.execute(
            "SELECT COUNT(*), MAX(id), TOTAL(LENGTH(name)), TOTAL(LENGTH(email)) FROM people"
        ).fetchone()
    )
    try:
        mri = tuple(
            conn.execute(
                "SELECT COUNT(*), TOTAL(person_id) FROM teams_mri_resolution "
                "WHERE person_id IS NOT NULL"
            ).fetchone()
        )
    except sqlite3.OperationalError:
        mri = ()
    return (people, mri, config.USER_NAME, config.USER_EMAIL_PATTERN)


def _store_path(conn: sqlite3.Connection) -> str:
    """The main database's file, or '' for an in-memory one, which is never cached."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return row[2] or ""
    return ""


def people_index(conn: sqlite3.Connection) -> _Index:
    """The people index of conn's store, built once and reused while it still holds."""
    path = _store_path(conn)
    signature = _signature(conn)
    if path:
        with _CACHE_LOCK:
            hit = _CACHE.get(path)
        if hit is not None and hit[0] == signature:
            return hit[1]
    index = _build(conn)
    if path:
        with _CACHE_LOCK:
            _CACHE.pop(path, None)
            _CACHE[path] = (signature, index)
            while len(_CACHE) > _CACHE_SIZE:
                _CACHE.pop(next(iter(_CACHE)))
    return index


def alias_targets(conn: sqlite3.Connection, ids) -> dict[int, int]:
    """The row each of `ids` stands for: by the reviewed alias map, else by its entity
    (src/store/entities.py: the row the entity stands for), where it has one."""
    ids = sorted(set(ids))
    if not ids:
        return {}
    targets = {}
    try:
        rows = conn.execute(
            "SELECT key, value FROM sync_metadata "
            "WHERE key IN (SELECT ? || value FROM json_each(?))",
            (ALIAS_PREFIX, json.dumps(ids)),
        ).fetchall()
    except sqlite3.OperationalError:  # a store without sync_metadata
        rows = []
    for key, value in rows:
        try:
            targets[int(key[len(ALIAS_PREFIX) :])] = int(str(value).strip())
        except ValueError:
            continue
    for pid, target in entity_targets(conn, [i for i in ids if i not in targets]).items():
        targets[pid] = target
    return targets


def entity_targets(conn: sqlite3.Connection, ids) -> dict[int, int]:
    """For people rows of `ids` that belong to an entity standing for another row (its
    person_id, src/store/entities.py), that row. Empty on a store before v38."""
    ids = sorted({int(i) for i in ids})
    if not ids:
        return {}
    try:
        rows = conn.execute(
            "SELECT CAST(ea.value AS INTEGER), e.person_id FROM entity_aliases ea"
            " JOIN entities e ON e.id = ea.entity_id"
            " WHERE ea.kind = 'person' AND e.person_id IS NOT NULL"
            " AND ea.value IN (SELECT CAST(value AS TEXT) FROM json_each(?))",
            (json.dumps(ids),),
        ).fetchall()
    except sqlite3.OperationalError:  # a store without the entity tables
        return {}
    return {pid: target for pid, target in rows if pid != target}


def _email_counts(conn: sqlite3.Connection, ids) -> dict[int, int]:
    ids = sorted(set(ids))
    if not ids:
        return {}
    return dict(
        conn.execute(
            "SELECT person_id, COUNT(*) FROM email_people "
            "WHERE person_id IN (SELECT value FROM json_each(?)) GROUP BY person_id",
            (json.dumps(ids),),
        ).fetchall()
    )


def _row(conn: sqlite3.Connection, pid: int):
    return conn.execute(
        "SELECT id, name, email, role, department FROM people WHERE id = ?", (pid,)
    ).fetchone()


def _candidates(conn: sqlite3.Connection, index: _Index, ids: list[int]) -> list[dict]:
    """other_candidates: name and address as stored now, and the identifier kind."""
    if not ids:
        return []
    rows = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "SELECT id, name, email FROM people WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(ids),),
        )
    }
    return [
        {"name": rows[pid][0], "email": rows[pid][1], "identifier": index.kinds.get(pid)}
        for pid in ids
        if pid in rows
    ]


def _with_prefix(index: _Index, prefixes, *, whole: bool) -> set[int]:
    """Rows with a word form equal to (whole) or starting with one of `prefixes`. A word of
    four letters or more ending in s also finds the word without it: the vocative."""
    found: set[int] = set()
    for prefix in prefixes:
        if len(prefix) >= 4 and prefix.endswith("s"):
            found.update(index.rows_by_form.get(prefix[:-1], ()))
        if whole:
            found.update(index.rows_by_form.get(prefix, ()))
            continue
        i = bisect_left(index.forms, prefix)
        while i < len(index.forms) and index.forms[i].startswith(prefix):
            found.update(index.rows_by_form[index.forms[i]])
            i += 1
    return found


def _textual(index: _Index, tokens: list[str]) -> set[int]:
    """Rows holding every query word at the start of one of their words, in any order.

    A word of three letters or more matches the start of a word ('Theo' finds
    Theodoros); shorter ones are whole words ('AI' is not in 'Michail'), and a
    number is a whole number. Beside a word of three letters or more, a single
    letter is an initial, and so are two letters at the end ('Surname Th'): they
    start a word too.
    """
    has_long = any(len(t) >= 3 and not t.isdigit() for t in tokens)
    lookups: list[tuple[str, bool]] = []
    initials: list[str] = []
    for i, token in enumerate(tokens):
        last = i == len(tokens) - 1
        if token.isdigit():
            lookups.append((token, True))
        elif len(token) >= 3:
            lookups.append((token, False))
        elif has_long and (len(token) == 1 or (last and len(tokens) > 1)):
            initials.append(token)
        else:
            lookups.append((token, True))
    rows: set[int] | None = None
    for token, whole in sorted(lookups, key=lambda lk: -len(lk[0])):
        found = _with_prefix(index, _forms(token), whole=whole)
        rows = found if rows is None else rows & found
        if not rows:
            return set()
    for token in initials:
        prefixes = _forms(token)
        rows = {
            pid
            for pid in rows or ()
            if any(f.startswith(p) for f in _row_forms(index.names[pid]) for p in prefixes)
        }
    return rows or set()


def _by_local_part(index: _Index, word: str) -> set[int]:
    found: set[int] = set()
    i = bisect_left(index.local_heads, word)
    while i < len(index.local_heads) and index.local_heads[i].startswith(word):
        found.update(index.rows_by_local[index.local_heads[i]])
        i += 1
    return found


def surname_fits(index: _Index, words: list[NameWord]) -> set[int]:
    """Identities whose name can be the one `words` spell (transliterate.names_agree):
    a shared surname in their name, in any script or romanisation, and no word against
    it. The words of an identity's address count as its own ('brostopoulou.evi' says
    what 'E.' stands for); it is still found only by its name."""
    keys: set[str] = set()
    for word in words:
        if word.initial or len(word.key) < 4 or word.key.isdigit():
            continue
        stem = word.key[:-1] if word.key.endswith("s") else word.key
        keys.update((stem, stem + "s"))
    fits = set()
    for pid in {pid for key in keys for pid in index.rows_by_form.get(key, ())}:
        if pid not in index.kinds:
            continue
        words_of = index.identity_words.get(pid)
        if words_of is None:  # read once per index: a common forename fits hundreds
            name = name_words(index.names[pid])
            words_of = index.identity_words[pid] = (name, _local_words(index.emails.get(pid), name))
        if names_agree(words, *words_of):
            fits.add(pid)
    return fits


def resolve_identity(
    conn: sqlite3.Connection, name_or_email: str, *, local_part: bool = True
) -> Resolution:
    """The person `name_or_email` means, how sure that is, and who else it could be.

    See the module docstring for the order. A query with no letter or digit is no one:
    '%' or a trailing comma in a list of attendees used to resolve to the most-emailed
    person. local_part off is for recall, where a topic word ('data', 'info') started some
    mailbox's local part and brought it in.
    """
    register_sql_functions(conn)  # for callers that go on to sb_fold et al.
    text = (name_or_email or "").strip()
    if not any(ch.isalnum() for ch in text):
        return Resolution()
    index = people_index(conn)
    if "@" in text:
        return _by_address(conn, index, text.lower())
    if _MRI.fullmatch(text):
        return _by_mri(conn, index, text)
    return _by_name(conn, index, text, local_part)


def _stands_for(index: _Index, aliases: dict[int, int], pid: int) -> int:
    """The entity a row stands for: the owner's row for any of his, else its alias target."""
    target = aliases.get(pid, pid)
    if target not in index.names:  # an alias to a row since merged away
        target = pid
    if index.owner_id is not None and (pid in index.owner_rows or target in index.owner_rows):
        return index.owner_id
    return target


def _by_address(conn: sqlite3.Connection, index: _Index, address: str) -> Resolution:
    pid = index.by_email.get(address)
    if index.owner_id is not None and (
        pid in index.owner_rows or is_owner_address(address, config.USER_EMAIL_PATTERN)
    ):
        pid = index.owner_id
    elif pid is None:
        return Resolution()
    else:
        pid = _stands_for(index, alias_targets(conn, [pid]), pid)
    row = _row(conn, pid)
    if row is None:
        return Resolution()
    return Resolution(row, 1, [], "jid" if address.endswith(_JID_DOMAINS) else "email")


def _by_mri(conn: sqlite3.Connection, index: _Index, mri: str) -> Resolution:
    try:
        found = conn.execute(
            "SELECT person_id FROM teams_mri_resolution WHERE mri = ? AND person_id IS NOT NULL",
            (mri,),
        ).fetchone()
    except sqlite3.OperationalError:  # a store without the Teams tables
        found = None
    if found is None or found[0] not in index.names:
        return Resolution()
    pid = _stands_for(index, alias_targets(conn, [found[0]]), found[0])
    row = _row(conn, pid)
    return Resolution(row, 1, [], "mri") if row is not None else Resolution()


def _by_name(conn: sqlite3.Connection, index: _Index, text: str, local_part: bool) -> Resolution:
    tokens = name_tokens(text)
    if not tokens:
        return Resolution()
    matched = _textual(index, tokens)
    phrase = " ".join(search_fold(text).split())
    if local_part and _LOCAL_QUERY.fullmatch(phrase):
        matched |= _by_local_part(index, phrase)
    aliases = alias_targets(conn, matched)
    stands_for = {pid: _stands_for(index, aliases, pid) for pid in matched}
    entities = set(stands_for.values())
    # Reached through the alias map or the owner rule from a row that matched.
    mapped = {entity for pid, entity in stands_for.items() if entity != pid}
    owner = index.owner_id if is_owner_name(text, config.USER_NAME) else None
    if owner is not None:
        entities.add(owner)
    else:
        # The identities whose names agree with the query are candidates too, matched
        # or not: 'Firstname Surname' is often spelled out only by mentions and by a
        # stray identity (an Exchange-wrapped address with one email), while the
        # person's own row reads 'F. Surname'. Ranked with the rest, the busiest wins.
        fits = surname_fits(index, name_words(text))
        entities |= {_stands_for(index, {}, pid) for pid in fits}
    if not entities:
        return Resolution()
    counts = _email_counts(conn, entities)
    ranked = sorted(entities, key=lambda e: (e not in index.kinds, -counts.get(e, 0), e))
    chosen = owner if owner is not None else ranked[0]
    # 'alias' when the reviewed map or the owner rule led here and the row's own
    # name did not match: that is the stronger evidence of the two.
    how = "alias" if owner is not None or (chosen in mapped and chosen not in matched) else "name"
    row = _row(conn, chosen)
    if row is None:
        return Resolution()
    others = [e for e in ranked if e != chosen][:3]
    return Resolution(row, len(entities), _candidates(conn, index, others), how)
