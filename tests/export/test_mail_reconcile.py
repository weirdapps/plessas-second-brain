"""mail-reconcile: which Outlook messages the store lacks, and staging them again.

An ID reconciliation on 2026-10-07 found 190 Outlook messages missing from the
store, 142 of them received 2026-05-02..05, the rest scattered: nothing had ever
compared what Outlook holds with what the store holds. The listing goes through
outlook-cli; the store is read only, so the report runs on a replica too. A
message counts as held by its Graph id (or an alias), by its RFC822 Message-ID,
or, for rows stored before the Message-ID was captured, by its subject and time.
"""

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from src.export import mail_reconcile as mr
from src.store import sql_readonly
from src.store.schema import create_database


@pytest.fixture(autouse=True)
def _no_pull_stamp(tmp_path, monkeypatch):
    """Open the store the producer's way on any host (see tests/test_sql_readonly.py)."""
    monkeypatch.setattr(sql_readonly, "REPLICA_STAMP", tmp_path / "absent-db-pull.stamp")


def _when(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# ---------------------------------------------------------------- normalising


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("RE: FW: Budget 2027", "budget 2027"),
        ("Απ: Πρ: Προϋπολογισμός", "προυπολογισμοσ"),  # casefold folds the final sigma
        ("ΣΧΕΤ: ΑΠ: θέμα", "θεμα"),
        ("[External] Re: Budget", "budget"),
        ("RE: [EXTERNAL] Budget", "budget"),
        ("[WARNING: ATTACHMENT(S) MAY CONTAIN MALWARE] Invoice", "invoice"),
        ("[SUSPECTED SPAM] Offer", "offer"),
        ("  Budget\t  2027 ", "budget 2027"),
        # Seen on the replica: a tag moves among the reply prefixes between the two
        # sides, and a zero-width space trails one side only.
        ("RE: [External Email] RE: STE package", "ste package"),
        ("RE: RE: [External Email] STE package", "ste package"),
        ("Re: [support] Re: Εγγραφή", "εγγραφη"),
        ("Re: Re: [support] Εγγραφή", "εγγραφη"),
        ("Institutions in the AI Era​", "institutions in the ai era"),
        ("[VPS] nightly backup", "nightly backup"),
        ("Budget [External] 2027", "budget 2027"),
        (None, ""),
    ],
)
def test_subjects_are_normalised_on_both_sides(subject, expected):
    assert mr.normalize_subject(subject) == expected


def test_rfc822_ids_compare_without_brackets_case_or_padding():
    assert mr.normalize_imid(" <Abc@Example.COM> ") == mr.normalize_imid("abc@example.com")
    assert mr.normalize_imid(None) == ""


def test_times_without_a_zone_are_read_as_utc():
    assert mr.parse_when("2026-03-01T09:16:41") == _when("2026-03-01T09:16:41")
    assert mr.parse_when("2026-10-07T12:12:01Z") == _when("2026-10-07T12:12:01")
    assert mr.parse_when("not a date") is None


def test_folder_names_take_the_exports_spelling():
    assert mr.canonical_folder("SentItems") == "Sent Items"
    assert mr.canonical_folder("sent items") == "Sent Items"
    assert mr.canonical_folder("archive") == "Archive"
    assert mr.canonical_folder("Projects/2026") == "Projects/2026"


# ---------------------------------------------------------------- matching


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    rows = [
        # message_id, date_received, subject, mailbox, internet_message_id
        ("AAMk-stored", "2026-05-04T08:00:00Z", "Stored by id", "Inbox", "<a@x>"),
        ("AAMk-inbox-b", "2026-05-04T09:00:00Z", "Moved", "Archive", "b@x"),
        ("4711", "2026-03-01T11:16:41", "RE: Old thread", "Archive", None),
        ("AAMk-c", "2026-03-01T09:30:00Z", "Same subject", "Inbox", "<c@x>"),
        ("news:1", "2026-05-04T10:00:00Z", "News subject", "News", None),
        ("-12", "2026-05-04T10:00:00Z", "Document subject", "External", None),
    ]
    for message_id, received, subject, mailbox, imid in rows:
        conn.execute(
            "INSERT INTO emails (message_id, date_received, subject, mailbox_name,"
            " internet_message_id) VALUES (?, ?, ?, ?, ?)",
            (message_id, received, subject, mailbox, imid),
        )
    stored_id = conn.execute("SELECT id FROM emails WHERE message_id = 'AAMk-stored'").fetchone()[0]
    conn.execute(
        "INSERT INTO email_aliases (message_id, email_id, recorded_at) VALUES ('AAMk-alias', ?, 'now')",
        (stored_id,),
    )
    conn.commit()
    conn.close()
    return path


def _msg(message_id, received, subject="s", imid=None):
    return {
        "Id": message_id,
        "ReceivedDateTime": received,
        "Subject": subject,
        "InternetMessageId": imid,
    }


def _kinds(store_path, messages, staged=(set(), set()), cutoff=None):
    conn = sql_readonly.connect_read_only(store_path)
    try:
        held = mr.read_store(conn)
    finally:
        conn.close()
    result = mr.classify(messages, held, staged, cutoff)
    return {m["Id"]: kind for kind, items in result.items() for m in items}


def test_each_way_a_message_is_held(store):
    kinds = _kinds(
        store,
        [
            _msg("AAMk-stored", "2026-05-04T08:00:00Z"),
            _msg("AAMk-alias", "2026-05-04T08:00:00Z"),
            _msg("AAMk-archive-b", "2026-05-04T09:00:00Z", imid="<B@X>"),
            # Stored before Message-IDs were captured, in local time, three hours off.
            _msg("AAMk-old", "2026-03-01T08:16:41Z", "[External] Old thread"),
        ],
    )
    assert kinds == {
        "AAMk-stored": "by_id",
        "AAMk-alias": "by_id",
        "AAMk-archive-b": "by_rfc822_id",
        "AAMk-old": "by_subject_time",
    }


def test_what_is_missing(store):
    kinds = _kinds(
        store,
        [
            # Same subject as a stored row without a Message-ID, but five hours off.
            _msg("AAMk-far", "2026-03-01T06:00:00Z", "Old thread"),
            # A stored row that has a Message-ID is matched by it alone: a reply in
            # the same thread an hour later is another message.
            _msg("AAMk-reply", "2026-03-01T10:30:00Z", "RE: Same subject", "<d@x>"),
            # News and documents are never mail.
            _msg("AAMk-news", "2026-05-04T10:00:00Z", "News subject"),
            _msg("AAMk-doc", "2026-05-04T10:00:00Z", "Document subject"),
        ],
    )
    assert set(kinds.values()) == {"missing"}


def test_staged_and_newer_messages_are_not_missing(store):
    kinds = _kinds(
        store,
        [
            _msg("AAMk-staged", "2026-05-05T08:00:00Z", imid="<e@x>"),
            _msg("AAMk-staged-copy", "2026-05-05T08:00:00Z", imid="<f@x>"),
            _msg("AAMk-new", "2026-10-07T12:00:00Z", imid="<g@x>"),
        ],
        staged=({"AAMk-staged"}, {"f@x"}),
        cutoff=_when("2026-10-07T11:00:00"),
    )
    assert kinds == {
        "AAMk-staged": "staged",
        "AAMk-staged-copy": "staged",
        "AAMk-new": "newer",
    }


def test_the_store_reads_its_newest_mail_leaving_news_and_documents_out(store):
    conn = sql_readonly.connect_read_only(store)
    try:
        held = mr.read_store(conn)
    finally:
        conn.close()
    assert held.newest == _when("2026-05-04T09:00:00")


# ---------------------------------------------------------------- listing


class FakeOutlook:
    """list-mail over a fixed set of messages per folder, honouring --since/--until."""

    def __init__(self, folders, auth_fails=False):
        self.folders = folders
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout_sec=60):
        self.calls.append(list(args))
        assert args[0] == "list-mail"
        opts = dict(zip(args[1::2], args[2::2], strict=False))
        since = mr.parse_when(opts["--since"])
        until = mr.parse_when(opts["--until"])
        return [
            m
            for m in self.folders.get(opts["--folder"], [])
            if since <= mr.parse_when(m["ReceivedDateTime"]) < until
        ]


def test_a_folder_is_listed_a_window_at_a_time(monkeypatch):
    fake = FakeOutlook(
        {"Archive": [_msg("a", "2026-01-05T00:00:00Z"), _msg("b", "2026-03-20T00:00:00Z")]}
    )
    monkeypatch.setattr(mr, "run_outlook_cli", fake)

    items = mr.list_folder("Archive", _when("2026-01-01T00:00:00"), _when("2026-04-01T00:00:00"))

    assert [m["Id"] for m in items] == ["a", "b"]
    assert len(fake.calls) == 3, "31-day windows over three months"
    first = fake.calls[0]
    for flag in ("--all", "--max", "--top", "--select", "--since", "--until"):
        assert flag in first
    assert first[first.index("--select") + 1] == "Id,InternetMessageId,ReceivedDateTime,Subject"
    assert first[first.index("--since") + 1] == "2026-01-01T00:00:00Z"


def test_a_window_at_the_listing_cap_is_refused(monkeypatch):
    monkeypatch.setattr(mr, "LIST_MAX", 2)
    fake = FakeOutlook({"Archive": [_msg(f"m{i}", "2026-01-05T00:00:00Z") for i in range(2)]})
    monkeypatch.setattr(mr, "run_outlook_cli", fake)

    with pytest.raises(mr.TruncatedListing):
        mr.list_folder("Archive", _when("2026-01-01T00:00:00"), _when("2026-01-10T00:00:00"))


def test_a_listing_wrapped_in_value_is_read(monkeypatch):
    monkeypatch.setattr(
        mr,
        "run_outlook_cli",
        lambda args, timeout_sec=60: {"value": [_msg("a", "2026-01-05T00:00:00Z")]},
    )
    items = mr.list_folder("Archive", _when("2026-01-01T00:00:00"), _when("2026-01-10T00:00:00"))
    assert [m["Id"] for m in items] == ["a"]


# ---------------------------------------------------------------- end to end


def test_reconcile_reports_what_each_folder_lacks(store, tmp_path, monkeypatch):
    fake = FakeOutlook(
        {
            "Archive": [
                _msg("AAMk-archive-b", "2026-05-04T09:00:00Z", imid="<b@x>"),
                _msg("AAMk-gone", "2026-05-03T09:00:00Z", "Lost one", "<lost@x>"),
            ],
            "Sent Items": [_msg("AAMk-stored", "2026-05-04T08:00:00Z", imid="<a@x>")],
        }
    )
    monkeypatch.setattr(mr, "run_outlook_cli", fake)

    report = mr.reconcile(
        store,
        ["Archive", "SentItems"],
        _when("2026-05-01T00:00:00"),
        _when("2026-05-06T00:00:00"),
        staging_dir=tmp_path / "no-staging",
    )

    assert report.counts["Archive"]["listed"] == 2
    assert report.counts["Archive"]["by_rfc822_id"] == 1
    assert report.counts["Archive"]["missing"] == 1
    assert report.counts["Sent Items"]["by_id"] == 1
    assert [(m["folder"], m["Id"]) for m in report.missing] == [("Archive", "AAMk-gone")]
    assert [copy[0] for copy in report.copies] == ["AAMk-archive-b"]
    assert {call[2] for call in fake.calls} == {"Archive", "Sent Items"}


def test_staged_batches_count_as_on_their_way(store, tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "batch-50001.json").write_text(
        json.dumps({"emails": [{"message_id": "AAMk-gone", "internet_message_id": "<lost@x>"}]})
    )
    (staging / "batch-50002.json").write_text("{not json")
    fake = FakeOutlook({"Archive": [_msg("AAMk-gone", "2026-05-03T09:00:00Z", imid="<lost@x>")]})
    monkeypatch.setattr(mr, "run_outlook_cli", fake)

    report = mr.reconcile(
        store, ["Archive"], _when("2026-05-01T00:00:00"), _when("2026-05-06T00:00:00"), staging
    )

    assert report.counts["Archive"]["staged"] == 1
    assert report.missing == []
    assert (staging / "batch-50002.json").exists(), "a read-only run quarantines nothing"


# ---------------------------------------------------------------- the producer's writes


def test_refetch_stages_the_missing_through_the_export(monkeypatch):
    staged: list[tuple[str, list[str]]] = []
    downloads: list[str] = []
    seen_concurrency: list[int] = []

    def fetch(ids, concurrency=5, body_mode="html"):
        seen_concurrency.append(concurrency)
        for message_id in ids:
            yield (
                None
                if message_id == "bad"
                else {
                    "Id": message_id,
                    "ReceivedDateTime": "2026-05-03T09:00:00Z",
                    "HasAttachments": message_id == "a1",
                }
            )

    monkeypatch.setattr(mr, "fetch_bodies_concurrent", fetch)
    monkeypatch.setattr(
        mr,
        "commit_messages_to_db",
        lambda messages, folder: staged.append((folder, [m["Id"] for m in messages])),
    )

    def download(messages):
        downloads.extend(m["Id"] for m in messages)
        return {"scanned": 1, "downloaded_messages": 1, "failed_messages": 0}

    monkeypatch.setattr(mr, "download_attachments_for_messages", download)
    missing = [
        {"folder": "Archive", "Id": "a1"},
        {"folder": "Archive", "Id": "bad"},
        {"folder": "Sent Items", "Id": "s1"},
        {"folder": "Sent Items", "Id": "s2"},
    ]

    stats = mr.refetch(missing, concurrency=8, limit=3)

    assert staged == [("Archive", ["a1"]), ("Sent Items", ["s1"])]
    assert stats["staged"] == 2
    assert stats["failed"] == ["bad"]
    assert stats["requested"] == 3
    assert max(seen_concurrency) <= 2, "M365 is shared: never more than two at once"
    assert downloads == ["a1", "s1"]


def test_record_aliases_notes_each_copy_once(store):
    from src.store.schema import get_connection

    conn = get_connection(str(store))
    email_id = conn.execute("SELECT id FROM emails WHERE message_id = 'AAMk-inbox-b'").fetchone()[0]
    copies = [("AAMk-archive-b", email_id)]

    assert mr.record_aliases(conn, copies) == 1
    assert mr.record_aliases(conn, copies) == 0
    assert (
        conn.execute(
            "SELECT email_id FROM email_aliases WHERE message_id = 'AAMk-archive-b'"
        ).fetchone()[0]
        == email_id
    )
    conn.close()


def test_the_report_runs_against_a_store_it_cannot_write(store, tmp_path, monkeypatch):
    """A replica: the report must only read."""
    monkeypatch.setattr(mr, "run_outlook_cli", FakeOutlook({"Archive": []}))
    before = store.read_bytes()
    store.chmod(0o444)
    try:
        with patch("src.store.schema.get_connection", side_effect=AssertionError("no writes")):
            report = mr.reconcile(
                store,
                ["Archive"],
                _when("2026-05-01T00:00:00"),
                _when("2026-05-06T00:00:00"),
                tmp_path / "no-staging",
            )
    finally:
        store.chmod(0o644)
    assert report.missing == []
    assert store.read_bytes() == before
