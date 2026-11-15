"""Who wrote the source an extracted item came from: the `sender_trust` label.

A decision, action or commitment is the model's reading of someone's text, and a
stranger's mail can be written to plant one ("action: wire the funds today"). The
2026-11-01 security audit asked for every such item to say who wrote its source, so a
reader can weigh it. Tag only: nothing is hidden or filtered, extraction and storage
are unchanged, and the label is computed when the item is read, from its parent (the
envelope's `kind` and `ref`, src/store/envelope.py):

    owner     the owner wrote it: mail from one of his addresses (owner.is_owner_address),
              his Claude Code sessions, a WhatsApp session where every message is his
    internal  a colleague: mail or a meeting from an address at a domain of one of his
              addresses (person_identity.owner_domains); a Teams thread none of whose
              resolved senders is at another domain
    known     an outside address the owner has written to (To, Cc or Bcc of a mail he
              sent) at least once (written_to). Mail an outsider sends him never makes
              them known: three inbound mails are an attacker's to send.
    external  anyone else: a new outside address, a news article, a Teams thread with a
              sender resolved to an address outside his domains (a guest or another
              tenant), a WhatsApp session someone else wrote in
    unknown   no sender can be told: a document (an upload, a SharePoint file, a meeting
              transcript: the owner stored it, others wrote it), mail with no address, a
              meeting with no organiser, an item whose parent is gone

The label describes the STATED sender, not a verified one. The store keeps no
authentication results (no SPF, DKIM or DMARC verdict, no Sender beside From), so `owner`
and `internal` rest on a From header or an organiser address anyone can write; a forged
From is the mail edge's to stop. Teams senders count only where the MRI was resolved to
an address (people, teams_mri_resolution): the store keeps no tenant id, so an unresolved
guest reads `internal`.

Mail takes its sender_address, an attachment's items their email's, a meeting's items
its organiser's. An `external` item is third-party content, never an instruction.
"""

import json
import sqlite3
from collections.abc import Iterable

from src import config
from src.store.owner import is_owner_address, owner_address_sql
from src.store.person_identity import owner_domains

OWNER = "owner"
INTERNAL = "internal"
KNOWN = "known"
EXTERNAL = "external"
UNKNOWN = "unknown"
LABELS = (OWNER, INTERNAL, KNOWN, EXTERNAL, UNKNOWN)

# Kinds whose label does not depend on who sent them.
_FIXED = {"document": UNKNOWN, "conversation": OWNER, "news": EXTERNAL}


def _norm(address: str | None) -> str:
    return (address or "").strip().lower()


def _inside(address: str, domains: frozenset[str]) -> bool:
    return "@" in address and address.partition("@")[2] in domains


def written_to(conn: sqlite3.Connection, addresses: Iterable[str | None]) -> set[str]:
    """Those of `addresses`, lower-cased, that the owner has sent a mail to (To, Cc or
    Bcc) at least once: one query for all of them."""
    wanted = sorted({_norm(a) for a in addresses} - {""})
    if not wanted:
        return set()
    sender_is_owner, params = owner_address_sql("e.sender_address", config.USER_EMAIL_PATTERN)
    rows = conn.execute(
        "SELECT DISTINCT lower(p.email) FROM people p"
        " JOIN email_people ep ON ep.person_id = p.id"
        " JOIN emails e ON e.id = ep.email_id"
        " WHERE lower(p.email) IN (SELECT value FROM json_each(?))"
        f" AND ep.role_in_email IN ('recipient', 'cc', 'bcc') AND {sender_is_owner}",
        (json.dumps(wanted), *params),
    )
    return {r[0] for r in rows}


def of_addresses(conn: sqlite3.Connection, addresses: Iterable[str | None]) -> dict[str, str]:
    """Lower-cased address -> label, for every non-empty one of `addresses`."""
    wanted = {_norm(a) for a in addresses} - {""}
    domains = owner_domains()
    labels: dict[str, str] = {}
    for address in wanted:
        if is_owner_address(address, config.USER_EMAIL_PATTERN):
            labels[address] = OWNER
        elif _inside(address, domains):
            labels[address] = INTERNAL
    for address in written_to(conn, wanted - set(labels)):
        labels[address] = KNOWN
    for address in wanted - set(labels):
        labels[address] = EXTERNAL
    return labels


def of_address(labels: dict[str, str], address: str | None) -> str:
    """`address`'s label in what of_addresses gave; unknown for no address."""
    return labels.get(_norm(address), UNKNOWN)


def _refs(rows: list[dict], kind: str) -> list:
    return sorted({r["ref"] for r in rows if r.get("kind") == kind and r.get("ref") is not None})


def _senders(conn: sqlite3.Connection, sql: str, ids: list) -> dict:
    """id -> address, by `sql` over the ids in json_each(?)."""
    if not ids:
        return {}
    return dict(conn.execute(sql, (json.dumps(ids),)).fetchall())


def _teams_outsiders(conn: sqlite3.Connection, ids: list) -> set:
    """The Teams threads among `ids` in which a sender resolved to an address outside
    the owner's domains (and not his own) wrote: a guest or another tenant."""
    if not ids:
        return set()
    try:
        rows = conn.execute(
            "SELECT DISTINCT m.thread_id, lower(trim(COALESCE(p.email, r.email)))"
            " FROM teams_messages m"
            " LEFT JOIN people p ON p.id = m.sender_person_id"
            " LEFT JOIN teams_mri_resolution r ON r.mri = m.sender_mri"
            " WHERE m.thread_id IN (SELECT value FROM json_each(?)) AND m.is_system = 0"
            " AND COALESCE(p.email, r.email) IS NOT NULL",
            (json.dumps(ids),),
        ).fetchall()
    except sqlite3.OperationalError:  # a store without the resolution table
        return set()
    domains = owner_domains()
    return {
        thread
        for thread, address in rows
        if "@" in address
        and not _inside(address, domains)
        and not is_owner_address(address, config.USER_EMAIL_PATTERN)
    }


def label(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Each row, an extracted item already enveloped, with `sender_trust`: one query per
    kind of parent for the whole list, never one per item. The rows, for chaining."""
    mail = _senders(
        conn,
        "SELECT id, sender_address FROM emails WHERE id IN (SELECT value FROM json_each(?))",
        _refs(rows, "email"),
    )
    meetings = _senders(
        conn,
        "SELECT id, organizer_email FROM calendar_events"
        " WHERE id IN (SELECT value FROM json_each(?))",
        _refs(rows, "calendar"),
    )
    # A session is the owner's when every message in it is his.
    whatsapp = _senders(
        conn,
        "SELECT thread_id, MIN(is_from_me) FROM whatsapp_messages"
        " WHERE thread_id IN (SELECT value FROM json_each(?)) GROUP BY thread_id",
        _refs(rows, "whatsapp"),
    )
    outsiders = _teams_outsiders(conn, _refs(rows, "teams"))
    labels = of_addresses(conn, [*mail.values(), *meetings.values()])
    for row in rows:
        kind, ref = row.get("kind"), row.get("ref")
        if kind in _FIXED:
            row["sender_trust"] = _FIXED[kind]
        elif kind == "email":
            row["sender_trust"] = of_address(labels, mail.get(ref))
        elif kind == "calendar":
            row["sender_trust"] = of_address(labels, meetings.get(ref))
        elif kind == "teams":
            row["sender_trust"] = EXTERNAL if ref in outsiders else INTERNAL
        elif kind == "whatsapp":
            mine = whatsapp.get(ref)
            row["sender_trust"] = UNKNOWN if mine is None else OWNER if mine == 1 else EXTERNAL
        else:
            row["sender_trust"] = UNKNOWN
    return rows
