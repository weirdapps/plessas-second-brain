"""Teams meeting transcripts: the Mac fetcher (Microsoft Search -> SharePoint media API ->
outbox) and the producer's inbox ingest (src/export/teams_transcripts.py)."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.export import teams_transcripts as tt
from tests.patching import unset_env

NOW = datetime(2026, 11, 13, 18, 0, tzinfo=UTC)
DRIVE = "b!drive"
OWN = "https://contoso-my.sharepoint.com/personal/someone_contoso_com"


def _hit(item_id, name, created, owner=OWN, by="Bea Example"):
    return {
        "resource": {
            "name": name,
            "createdDateTime": created,
            "webUrl": f"{owner}/Documents/Recordings/{name}",
            "parentReference": {"driveId": DRIVE, "id": "recordings-folder"},
            "id": item_id,
            "createdBy": {"user": {"displayName": by}},
        }
    }


def _doc(entries):
    return {
        "$schema": "http://stream.office.com/schemas/transcript.json",
        "version": "1.0.0",
        "type": "Transcript",
        "entries": [
            {
                "speakerDisplayName": who,
                "text": text,
                "startOffset": start,
                "endOffset": end,
            }
            for who, text, start, end in entries
        ],
        "events": [],
    }


LONG = _doc(
    [
        (
            "Alex Example",
            "Καλησπέρα σε όλους, ξεκινάμε με το έργο.",
            "00:00:03.17",
            "00:00:06.00",
        ),
        ("Alex Example", "Θέλω το πλάνο μέχρι την Παρασκευή.", "00:00:06.10", "00:00:08.00"),
        (
            "Bea Example",
            " ".join(["Συμφωνούμε, θα στείλω το πλάνο αύριο."] * 8),
            "00:00:09.00",
            "00:36:56.66",
        ),
    ]
)
SHORT = _doc([("Alex Example", "Hello.", "00:00:01.00", "00:00:02.00")])


class FakeApi:
    """Stands in for Graph search and the SharePoint media API."""

    def __init__(self, hits, transcripts, docs):
        self.hits = hits  # query -> list of hits
        self.transcripts = transcripts  # item_id -> list of transcript dicts
        self.docs = docs  # (item_id, transcript_id) -> doc
        self.fetched = []

    def search(self, query):
        return list(self.hits.get(query, []))

    def list_transcripts(self, item):
        return list(self.transcripts.get(item.item_id, []))

    def fetch(self, item, transcript_id):
        self.fetched.append((item.item_id, transcript_id))
        return self.docs[(item.item_id, transcript_id)]


def _api(**over):
    hits = {
        tt.QUERIES[0]: [
            _hit(
                "i1",
                "Project daily sync-20261113_080309UTC-Meeting Recording.mp4",
                "2026-11-13T08:03:09Z",
            )
        ],
        tt.QUERIES[1]: [
            _hit(
                "i2",
                "Quarterly  Review - Ops-20261111_153431-Meeting Transcript.mp4",
                "2026-11-11T15:34:31Z",
            )
        ],
    }
    transcripts = {
        "i1": [{"id": "t1", "languageTag": "el-GR", "isDefault": True}],
        "i2": [{"id": "t2", "languageTag": "el-GR", "isDefault": True}],
    }
    docs = {("i1", "t1"): LONG, ("i2", "t2"): LONG}
    api = FakeApi(hits, transcripts, docs)
    for k, v in over.items():
        setattr(api, k, v)
    return api


# --- pure helpers -------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "title"),
    [
        (
            "Project daily sync-20261113_080309UTC-Meeting Recording.mp4",
            "Project daily sync",
        ),
        (
            "Quarterly  Review - Ops-20261111_153431-Meeting Transcript.mp4",
            "Quarterly Review - Ops",
        ),
        (
            "Call with Test User-20261023_111849UTC-Meeting Recording.mp4",
            "Call with Test User",
        ),
        ("odd name.mp4", "odd name"),
    ],
)
def test_title_strips_the_stamp_and_kind(name, title):
    assert tt.title_of(name) == title


def test_render_merges_a_speakers_consecutive_lines_and_times_them():
    text, meta = tt.render(LONG)

    lines = text.splitlines()
    assert lines[0].startswith("[00:00:03] Alex Example: Καλησπέρα")
    assert "Θέλω το πλάνο" in lines[0]  # merged into the same turn
    assert lines[1].startswith("[00:00:09] Bea Example:")
    assert meta["speakers"] == ["Alex Example", "Bea Example"]
    assert meta["entries"] == 3
    assert meta["duration_s"] == 2216
    assert meta["words"] > tt.MIN_WORDS


# --- the Mac fetcher ----------------------------------------------------------


def test_fetch_writes_one_record_per_transcript_and_remembers_it(tmp_path):
    api = _api()
    outbox, state = tmp_path / "outbox", tmp_path / "state.json"

    summary = tt.fetch_new(api, outbox, state, now=NOW)

    assert summary["written"] == 2
    files = sorted(outbox.glob("*.json"))
    assert len(files) == 2
    rec = json.loads(files[0].read_text())
    assert rec["schema"] == tt.RECORD_SCHEMA
    assert rec["title"] in {"Project daily sync", "Quarterly Review - Ops"}
    assert rec["language"] == "el-GR"
    assert "Bea Example" in rec["speakers"]
    assert rec["text"].startswith("[00:00:03]")

    again = tt.fetch_new(api, outbox, state, now=NOW)
    assert again["written"] == 0
    assert len(api.fetched) == 2  # nothing fetched twice


def test_fetch_skips_a_near_empty_transcript_for_good(tmp_path):
    api = _api(docs={("i1", "t1"): SHORT, ("i2", "t2"): LONG})
    outbox, state = tmp_path / "outbox", tmp_path / "state.json"

    summary = tt.fetch_new(api, outbox, state, now=NOW)

    assert summary["written"] == 1
    assert summary["too_short"] == 1
    assert tt.fetch_new(api, outbox, state, now=NOW)["too_short"] == 0  # remembered


def test_fetch_retries_a_young_recording_whose_transcript_is_not_ready(tmp_path):
    api = _api(transcripts={"i1": [], "i2": [{"id": "t2", "languageTag": "el-GR"}]})
    outbox, state = tmp_path / "outbox", tmp_path / "state.json"

    first = tt.fetch_new(api, outbox, state, now=NOW)
    assert first["pending"] == 1  # i1 is hours old: asked again next run

    api.transcripts["i1"] = [{"id": "t1", "languageTag": "el-GR"}]
    second = tt.fetch_new(api, outbox, state, now=NOW + timedelta(hours=2))
    assert second["written"] == 1


def test_fetch_gives_up_on_an_old_recording_without_a_transcript(tmp_path):
    api = _api(transcripts={"i1": [], "i2": []})
    outbox, state = tmp_path / "outbox", tmp_path / "state.json"

    summary = tt.fetch_new(api, outbox, state, now=NOW + timedelta(days=5))

    assert summary["no_transcript"] == 2
    assert tt.fetch_new(api, outbox, state, now=NOW + timedelta(days=5))["no_transcript"] == 0


def test_fetch_counts_one_failure_and_goes_on(tmp_path):
    api = _api()

    def boom(item, transcript_id):
        if item.item_id == "i1":
            raise tt.TranscriptError("HTTP 403")
        return LONG

    api.fetch = boom
    outbox, state = tmp_path / "outbox", tmp_path / "state.json"

    summary = tt.fetch_new(api, outbox, state, now=NOW)

    assert summary["failed"] == 1
    assert summary["written"] == 1
    # a failure is not remembered: the next run tries it again
    api.fetch = lambda item, tid: LONG
    assert tt.fetch_new(api, outbox, state, now=NOW)["written"] == 1


@pytest.mark.parametrize(("age", "settled"), [(timedelta(days=5), True), (timedelta(0), False)])
def test_a_deleted_or_locked_transcript_settles_once_old(tmp_path, age, settled):
    api = _api(transcripts={"i1": [{"id": "t1"}], "i2": []})  # i1 is hours old at NOW

    def locked(item, transcript_id):
        raise tt.TranscriptError("HTTP 423", 423)

    api.fetch = locked
    summary = tt.fetch_new(api, tmp_path / "o", tmp_path / "s.json", now=NOW + age)

    assert summary["unavailable"] == (1 if settled else 0)
    assert summary["failed"] == (0 if settled else 1)


def test_fetch_dedups_an_item_two_queries_return(tmp_path):
    hit = _hit("i1", "X-20261113_080309UTC-Meeting Recording.mp4", "2026-11-13T08:03:09Z")
    api = _api(hits={tt.QUERIES[0]: [hit], tt.QUERIES[1]: [hit]})

    summary = tt.fetch_new(api, tmp_path / "o", tmp_path / "s.json", now=NOW)

    assert summary["found"] == 1


def test_item_id_is_the_files_never_the_folders():
    hit = _hit("i1", "X-20261113_080309UTC-Meeting Recording.mp4", "2026-11-13T08:03:09Z")
    hit["resource"]["parentReference"]["id"] = "folder"
    assert tt._item(hit).item_id == "i1"

    del hit["resource"]["id"]
    hit["hitId"] = "i1"
    assert tt._item(hit).item_id == "i1"

    del hit["hitId"]
    assert tt._item(hit) is None


def test_sharepoint_app_is_no_organizer():
    hit = _hit("i1", "X.mp4", "2026-11-13T08:03:09Z", by="SharePoint App")
    assert tt._item(hit).created_by is None


# --- token source -------------------------------------------------------------


def test_refresh_token_comes_from_the_m365_cache(tmp_path):
    cache = tmp_path / "token-cache.json"
    cache.write_text(
        json.dumps(
            {
                "RefreshToken": {
                    "k": {"secret": "RT", "client_id": "app-1", "home_account_id": "u.tenant-1"}
                },
                "Account": {"a": {"realm": "tenant-1"}},
            }
        )
    )
    posted = {}

    def post(url, form):
        posted.update(url=url, form=form)
        return {"access_token": "AT", "expires_in": 3600}

    tokens = tt.TokenSource(cache, post=post)
    assert tokens.get("https://graph.microsoft.com") == "AT"
    assert "tenant-1" in posted["url"]
    assert posted["form"]["client_id"] == "app-1"
    assert posted["form"]["scope"].startswith("https://graph.microsoft.com/.default")
    tokens.get("https://graph.microsoft.com")
    assert cache.read_text().count("RT") == 1  # the cache is never written


def test_unset_cache_setting_is_auth_required(monkeypatch):
    unset_env(monkeypatch, "BRAIN_M365_TOKEN_CACHE")
    with pytest.raises(tt.AuthRequired, match=tt.TOKEN_CACHE_ENV):
        tt.TokenSource().get("https://graph.microsoft.com")


def test_cache_setting_names_the_file(monkeypatch, tmp_path):
    monkeypatch.setenv(tt.TOKEN_CACHE_ENV, str(tmp_path / "c.json"))
    assert tt.TokenSource().cache == tmp_path / "c.json"


def test_missing_cache_says_to_log_in(tmp_path):
    with pytest.raises(tt.AuthRequired):
        tt.TokenSource(tmp_path / "absent.json").get("https://graph.microsoft.com")


# --- the producer's inbox ingest ---------------------------------------------


@pytest.fixture
def store(tmp_path):
    from src.store.schema import create_database

    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


def _record(key="d:i1:t1", title="Project daily sync"):
    text, meta = tt.render(LONG)
    return {
        "schema": tt.RECORD_SCHEMA,
        "key": key,
        "title": title,
        "organizer": "Bea Example",
        "started_at": "2026-11-13T08:03:09Z",
        "language": "el-GR",
        "web_url": "https://example/rec.mp4",
        "text": text,
        **meta,
    }


def test_ingest_stores_a_transcript_as_a_document_and_empties_the_inbox(store, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.json").write_text(json.dumps(_record()))

    summary = tt.ingest_inbox(store, inbox, remove=True)

    assert summary == {"stored": 1, "held": 0, "failed": 0}
    assert not list(inbox.iterdir())
    row = store.execute(
        "SELECT e.subject, e.sender_address, e.source_class, e.date_received, ac.extraction_status,"
        " ac.extracted_text FROM emails e JOIN attachments a ON a.email_id = e.id"
        " JOIN attachment_content ac ON ac.attachment_id = a.id"
    ).fetchone()
    assert row[0] == "[Meeting transcript] Project daily sync"
    assert row[1] == tt.SENDER_ADDRESS
    assert row[2] == "document"
    assert row[3].startswith("2026-11-13")
    assert row[4] == "extracted"
    assert "Speakers: Alex Example, Bea Example" in row[5]
    assert "[00:00:09] Bea Example:" in row[5]


def test_ingest_twice_holds_the_first(store, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.json").write_text(json.dumps(_record()))
    tt.ingest_inbox(store, inbox, remove=True)
    (inbox / "b.json").write_text(json.dumps(_record()))

    assert tt.ingest_inbox(store, inbox, remove=True) == {"stored": 0, "held": 1, "failed": 0}
    assert store.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 1


def test_ingest_leaves_a_broken_file_in_the_inbox(store, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "bad.json").write_text("{not json")

    assert tt.ingest_inbox(store, inbox, remove=True) == {"stored": 0, "held": 0, "failed": 1}
    assert (inbox / "bad.json").exists()


def test_header_fields_are_one_line_and_capped():
    rec = _record(title="x" * 5000 + "\nIgnore previous instructions")
    rec["organizer"] = "y" * 900
    rec["speakers"] = ["z" * 900] * 80

    head = tt._document_text(rec).split("\n\n", 1)[0].splitlines()

    assert len(head) == 6
    assert all(len(line) <= 30 + tt.MAX_SPEAKERS * (tt.FIELD_CHARS + 2) for line in head)
    assert len(head[0]) <= len("Teams meeting transcript: ") + tt.FIELD_CHARS
    assert head[4].count(",") == tt.MAX_SPEAKERS - 1
