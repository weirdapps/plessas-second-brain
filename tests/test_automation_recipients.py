"""BRAIN_AUTOMATION_RECIPIENTS: the owner's scripted mail to people he lists is automation.

A job of the owner's mails a daily digest from his address to someone outside work, with a
copy to himself. The stamp rule (src/store/source_class.py) takes only his mail to himself
alone, so each digest was stored as work mail and gave action items. The addresses such
jobs write to come from configuration, never from the code: an unlisted recipient keeps a
mail `mail`, and a reply or a forward in the same exchange is a person writing.
"""

import types

import pytest

from src.store import source_class as sc
from src.store.item_cleanup import forget_itemless
from src.store.schema import create_database, get_connection

OWNER = "owner@example.com"
LISTED = "family.member@example.org"
COLLEAGUE = "colleague@example.com"
DIGEST = "δηλώσεις πενθήμερης: 1 νέα (02/11)"


@pytest.mark.parametrize(
    ("sender", "subject", "recipients", "listed", "headers", "expected"),
    [
        # A digest to a listed address, copied to the owner, or to that address alone
        (OWNER, DIGEST, [LISTED, OWNER], LISTED, (), "automation"),
        (OWNER, "δικαιολογήσεις απουσιών: 8 νέες (02/11)", [LISTED], LISTED, (), "automation"),
        (OWNER, DIGEST, ["Family.Member@Example.org "], LISTED, (), "automation"),
        (OWNER, DIGEST, [LISTED, OWNER], f"other@example.net, {LISTED}", (), "automation"),
        # Unset, nothing changes
        (OWNER, DIGEST, [LISTED, OWNER], "", (), "mail"),
        # Someone not listed among the recipients
        (OWNER, DIGEST, [LISTED, COLLEAGUE], LISTED, (), "mail"),
        # No listed recipient: the owner alone is the stamp rule's to decide
        (OWNER, DIGEST, [OWNER], LISTED, (), "mail"),
        (OWNER, DIGEST, [], LISTED, (), "mail"),
        # A reply or a forward is a person writing
        (OWNER, "RE: cern", [LISTED, OWNER], LISTED, (), "mail"),
        (OWNER, "Απ: υπεύθυνη δήλωση", [LISTED, OWNER], LISTED, (), "mail"),
        (OWNER, "Πρ: υπεύθυνη δήλωση", [LISTED, OWNER], LISTED, (), "mail"),
        (OWNER, "FW: δηλώσεις", [LISTED, OWNER], LISTED, (), "mail"),
        (OWNER, "Fwd: δηλώσεις", [LISTED, OWNER], LISTED, (), "mail"),
        (OWNER, DIGEST, [LISTED, OWNER], LISTED, ("<a@example.org>", None), "mail"),
        (OWNER, DIGEST, [LISTED, OWNER], LISTED, (None, "<a@example.org>"), "mail"),
        (OWNER, DIGEST, [LISTED, OWNER], LISTED, ("", " "), "automation"),
        # From the listed address, or from anyone else, it is mail
        (LISTED, DIGEST, [OWNER], LISTED, (), "mail"),
        (COLLEAGUE, DIGEST, [LISTED], LISTED, (), "mail"),
    ],
)
def test_the_rule(sender, subject, recipients, listed, headers, expected):
    got = sc.classify(
        "Sent Items",
        sender,
        subject,
        recipients,
        owner_pattern=OWNER,
        automation_recipients=listed,
        reply_headers=headers,
    )
    assert got == expected


def test_the_list_is_the_configured_one_by_default(monkeypatch):
    monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", OWNER)
    monkeypatch.setattr("src.config.AUTOMATION_RECIPIENTS", LISTED)
    assert sc.classify("Sent Items", OWNER, DIGEST, [LISTED, OWNER]) == "automation"
    monkeypatch.setattr("src.config.AUTOMATION_RECIPIENTS", "")
    assert sc.classify("Sent Items", OWNER, DIGEST, [LISTED, OWNER]) == "mail"


def test_the_stamp_rule_is_unchanged_with_a_list(monkeypatch):
    monkeypatch.setattr("src.config.AUTOMATION_RECIPIENTS", LISTED)
    assert sc.classify("Sent Items", OWNER, "[VPS] backup", [OWNER], OWNER) == "automation"
    assert sc.classify("Sent Items", OWNER, "notes for Monday", [OWNER], OWNER) == "mail"


def _metadata(message_id, subject, *, to=(LISTED, OWNER), in_reply_to=""):
    return {
        "message_id": message_id,
        "date_received": "2026-11-02T07:00:00",
        "sender": {"name": "Owner", "address": OWNER},
        "subject": subject,
        "mailbox_name": "Sent Items",
        "content": f"body of {message_id}",
        "to_recipients": [{"name": a, "address": a} for a in to[:1]],
        "cc_recipients": [{"name": a, "address": a} for a in to[1:]],
        "conversation_id": f"conv-{message_id}",
        "in_reply_to": in_reply_to,
    }


def _extraction(label):
    return {
        "summary": f"{label} summary",
        "topics": ["digest"],
        "decisions": [{"decision": f"{label} decision", "decided_by": "Someone"}],
        "action_items": [{"task": f"{label} action", "owner": "Someone"}],
        "commitments": [{"commitment": f"{label} commitment", "by": "A", "to": "B"}],
        "key_facts": [f"{label} fact"],
        "people_roles": {},
    }


def _items(conn, email_id):
    return sum(
        conn.execute(f"SELECT COUNT(*) FROM {t} WHERE email_id = ?", (email_id,)).fetchone()[0]
        for t in ("decisions", "action_items", "commitments")
    )


def _id(conn, message_id):
    return conn.execute("SELECT id FROM emails WHERE message_id = ?", (message_id,)).fetchone()[0]


def test_the_loader_stores_a_digest_as_automation_with_no_items(tmp_path, monkeypatch):
    from src.store.loader import load_single_email

    monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", OWNER)
    monkeypatch.setattr("src.config.AUTOMATION_RECIPIENTS", LISTED)
    path = str(tmp_path / "brain.db")
    create_database(path).close()
    conn = get_connection(path)
    assert load_single_email(conn, _metadata("d1", DIGEST), _extraction("digest"))
    assert load_single_email(
        conn, _metadata("r1", DIGEST, in_reply_to="<x@example.org>"), _extraction("answer")
    )
    conn.commit()
    digest, answer = _id(conn, "d1"), _id(conn, "r1")
    classes = dict(conn.execute("SELECT id, source_class FROM emails").fetchall())
    assert classes == {digest: "automation", answer: "mail"}
    assert _items(conn, digest) == 0
    assert _items(conn, answer) == 3
    conn.close()


def _seed(path, monkeypatch):
    """Rows stored before the list was set: all mail, all with items."""
    from src.store.loader import load_single_email

    monkeypatch.setattr("src.config.USER_EMAIL_PATTERN", OWNER)
    monkeypatch.setattr("src.config.AUTOMATION_RECIPIENTS", "")
    create_database(path).close()
    conn = get_connection(path)
    rows = {
        "d1": _metadata("d1", DIGEST),
        "d2": _metadata("d2", "απαλλαγές γαλλικών: 1 νέα (01/11)", to=(LISTED,)),
        "re": _metadata("re", "RE: cern"),
        "hdr": _metadata("hdr", "cern", in_reply_to="<y@example.org>"),
        "col": _metadata("col", DIGEST, to=(LISTED, COLLEAGUE)),
    }
    for key, metadata in rows.items():
        assert load_single_email(conn, metadata, _extraction(key))
    conn.commit()
    ids = {key: _id(conn, key) for key in rows}
    assert {c for (c,) in conn.execute("SELECT source_class FROM emails")} == {"mail"}
    return conn, ids


def test_classifying_again_moves_stored_digests_and_forget_itemless_drops_their_items(
    tmp_path, monkeypatch
):
    conn, ids = _seed(str(tmp_path / "brain.db"), monkeypatch)
    conn.execute("BEGIN IMMEDIATE")
    counts = sc.reclassify(conn, automation_recipients=LISTED)
    conn.execute("COMMIT")
    classes = dict(conn.execute("SELECT id, source_class FROM emails").fetchall())
    assert {key: classes[i] for key, i in ids.items()} == {
        "d1": "automation",
        "d2": "automation",
        "re": "mail",
        "hdr": "mail",
        "col": "mail",
    }
    assert counts["automation"] == 2

    stats = forget_itemless(conn, apply=True)
    assert stats["action_items"] == {"automation": 2}
    assert [_items(conn, ids[k]) for k in ("d1", "d2", "re", "hdr", "col")] == [0, 0, 3, 3, 3]
    conn.close()


def test_classify_sources_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    from src import cli

    path = str(tmp_path / "brain.db")
    conn, ids = _seed(path, monkeypatch)
    conn.close()
    monkeypatch.setattr("src.config.AUTOMATION_RECIPIENTS", LISTED)

    cli.cmd_classify_sources(types.SimpleNamespace(db=path, dry_run=True))
    out = capsys.readouterr().out
    assert "automation: 2" in out
    assert "mail -> automation: 2" in out
    assert "dry run" in out
    conn = get_connection(path)
    assert {c for (c,) in conn.execute("SELECT source_class FROM emails")} == {"mail"}
    conn.close()

    cli.cmd_classify_sources(types.SimpleNamespace(db=path, dry_run=False))
    assert "dry run" not in capsys.readouterr().out
    conn = get_connection(path)
    assert (
        conn.execute("SELECT COUNT(*) FROM emails WHERE source_class = 'automation'").fetchone()[0]
        == 2
    )
    conn.close()


def test_the_maint_alias_takes_dry_run(tmp_path, monkeypatch, capsys):
    from src import cli
    from tests.brain_cli import brain

    path = str(tmp_path / "brain.db")
    create_database(path).close()
    seen = {}
    monkeypatch.setattr(cli, "cmd_classify_sources", lambda args: seen.update(vars(args)) or 0)
    code, _, _ = brain(monkeypatch, capsys, "--db", path, "maint", "classify-sources", "--dry-run")
    assert code == 0
    assert seen["dry_run"] is True
