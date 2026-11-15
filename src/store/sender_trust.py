"""Who wrote the source an extracted item came from: the `sender_trust` label.

A decision, action or commitment is the model's reading of someone's text, and a
stranger's mail can be written to plant one ("action: wire the funds today"). The
2026-11-01 security audit asked for every such item to say who wrote its source, so a
reader can weigh it. Tag only: nothing is hidden or filtered, extraction and storage
are unchanged, and the label is computed when the item is read, from its parent (the
envelope's `kind` and `ref`, src/store/envelope.py):

    owner     the owner wrote it: mail from one of his addresses (owner.is_owner_address),
              a document he ingested, his Claude Code sessions, a WhatsApp session where
              every message is his
    internal  a colleague: mail or a meeting from an address at a domain of one of his
              addresses (person_identity.owner_domains); every Teams thread, since Teams
              is the owner's own tenant
    known     an outside address seen in person_identity.TRUSTED_MIN_EMAILS mails or more,
              the rule an auto-merge trusts (person_identity.well_seen)
    external  anyone else: a new outside address, a news article, a WhatsApp session
              someone else wrote in
    unknown   no sender can be told: mail with no address, a meeting with no organiser,
              an item whose parent is gone

Mail takes its sender_address, an attachment's items their email's, a meeting's items
its organiser's. An `external` item is third-party content, never an instruction.
"""

import json
import sqlite3
from collections.abc import Iterable

from src import config
from src.store.owner import is_owner_address
from src.store.person_identity import owner_domains, well_seen

OWNER = "owner"
INTERNAL = "internal"
KNOWN = "known"
EXTERNAL = "external"
UNKNOWN = "unknown"
LABELS = (OWNER, INTERNAL, KNOWN, EXTERNAL, UNKNOWN)

# Kinds whose label does not depend on who sent them.
_FIXED = {"document": OWNER, "conversation": OWNER, "teams": INTERNAL, "news": EXTERNAL}


def _norm(address: str | None) -> str:
    return (address or "").strip().lower()


def of_addresses(conn: sqlite3.Connection, addresses: Iterable[str | None]) -> dict[str, str]:
    """Lower-cased address -> label, for every non-empty one of `addresses`."""
    wanted = {_norm(a) for a in addresses} - {""}
    domains = owner_domains()
    labels: dict[str, str] = {}
    for address in wanted:
        if is_owner_address(address, config.USER_EMAIL_PATTERN):
            labels[address] = OWNER
        elif "@" in address and address.partition("@")[2] in domains:
            labels[address] = INTERNAL
    for address in well_seen(conn, wanted - set(labels)):
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
    labels = of_addresses(conn, [*mail.values(), *meetings.values()])
    for row in rows:
        kind, ref = row.get("kind"), row.get("ref")
        if kind in _FIXED:
            row["sender_trust"] = _FIXED[kind]
        elif kind == "email":
            row["sender_trust"] = of_address(labels, mail.get(ref))
        elif kind == "calendar":
            row["sender_trust"] = of_address(labels, meetings.get(ref))
        elif kind == "whatsapp":
            mine = whatsapp.get(ref)
            row["sender_trust"] = UNKNOWN if mine is None else OWNER if mine == 1 else EXTERNAL
        else:
            row["sender_trust"] = UNKNOWN
    return rows
