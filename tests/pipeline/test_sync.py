"""`brain sync` stage by stage: what each stage is handed, and when the run stops.

Every stage is a fake that records its turn and its arguments, so these tests read
the orchestrator alone (src/pipeline/sync.py): no data directory beyond a temporary
store, no model, no network.
"""

import fcntl
import types

import pytest

from src.config import IMAGE_CLASSIFY_BUDGET_S
from src.pipeline import sync
from src.store.schema import create_database, get_connection

BEFORE = "2026-01-01T00:00:00"


class Stages:
    """The sync's stages as fakes: `calls` is their order, `given` what each was handed."""

    def __init__(self, db, monkeypatch):
        self.db = db
        self.calls: list[str] = []
        self.given: dict[str, dict] = {}
        self.extraction: dict = {"extracted": 1, "model_successes": 1}
        self.mail = 1  # emails the load adds
        self.conversations = 1  # conversations the export finds and the load adds
        self.stops: list[bool] = []  # what stop_requested answers, in turn
        self.text_pass_recent = False
        self.embed_error: Exception | None = None
        self._loaded = 0

        def record(name, given, result):
            self.calls.append(name)
            self.given[name] = given
            return result

        def load(db_path, extracted_dir, staging_dir, touched_people):
            conn = get_connection(db_path)
            for _ in range(self.mail):
                self._loaded += 1
                conn.execute(
                    "INSERT INTO emails (message_id, date_received, subject) VALUES (?, ?, ?)",
                    (f"m-{self._loaded}", "2026-10-11T08:00:00Z", "okapi"),
                )
            conn.commit()
            conn.close()
            if self.mail:
                touched_people.update({7, 9})
            return record("load", {"db_path": db_path}, self.mail)

        def embed(conn):
            record("embed", {}, None)
            if self.embed_error is not None:
                raise self.embed_error
            return 1

        fakes = {
            "src.extract.local.run_extraction": lambda **kw: record("extract", kw, self.extraction),
            "src.extract.local.stop_requested": lambda: self.stops.pop(0) if self.stops else False,
            "src.store.loader.load_extractions": load,
            "src.export.outlook_attachments.register_new_downloads": lambda conn, root, ids, **kw: (
                record(
                    "register",
                    {"message_ids": ids, **kw},
                    {"registered": 0, "scanned": 0, "deferred": 0},
                )
            ),
            "src.store.dedup_people.run_dedup": lambda **kw: record(
                "dedup", kw, {"total_reduced": 0}
            ),
            "src.extract.attachment_pipeline.text_pass_is_recent": lambda conn: (
                self.text_pass_recent
            ),
            "src.extract.attachment_pipeline.run_phase1": lambda db_path, **kw: record(
                "phase1", kw, {"processed": 2, "extracted": 2, "ids": [11, 12]}
            ),
            "src.extract.attachment_pipeline.run_phase2": lambda db_path, **kw: record(
                "phase2", kw, {"processed": 2, "extracted": 2}
            ),
            "src.export.conversation_export.export_conversations": lambda **kw: record(
                "export_conversations", kw, {"exported": self.conversations, "pending": 0}
            ),
            "src.extract.local.run_conversation_extraction": lambda **kw: record(
                "extract_conversations", kw, None
            ),
            "src.store.loader.load_conversations": lambda db_path: record(
                "load_conversations", {}, self.conversations
            ),
            "src.extract.image_pipeline.run_backfill": lambda **kw: record(
                "images", kw, {"classified": 0}
            ),
            "src.store.embeddings.build_index": embed,
        }
        for target, fake in fakes.items():
            monkeypatch.setattr(target, fake)

    def run(self, **options):
        args = types.SimpleNamespace(
            db=self.db,
            limit=None,
            engine="claude",
            workers=1,
            lock_wait=0,
            skip_conversations=False,
            full_dedup=False,
            **options,
        )
        return sync.run(args)

    def metadata(self) -> dict:
        conn = get_connection(str(self.db))
        rows = dict(conn.execute("SELECT key, value FROM sync_metadata").fetchall())
        conn.close()
        return rows


@pytest.fixture
def stages(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    conn = create_database(str(db))
    conn.execute("INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', ?)", (BEFORE,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(sync, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(sync, "_extract_deadline_s", lambda: 123.0)
    return Stages(db, monkeypatch)


def test_each_stage_is_handed_what_the_one_before_it_produced(stages):
    assert stages.run() == 0

    assert stages.calls == [
        "extract",
        "load",
        "register",
        "dedup",
        "phase1",
        "phase2",
        "export_conversations",
        "extract_conversations",
        "load_conversations",
        "images",
        "embed",
    ]
    assert stages.given["extract"]["deadline_s"] == 123.0
    assert stages.given["register"]["message_ids"] == ["m-1"]
    assert stages.given["register"]["limit"] == sync.ATTACHMENT_REGISTER_LIMIT
    assert stages.given["dedup"]["touched"] == {7, 9}
    assert stages.given["phase1"]["deadline_s"] == sync.PHASE1_SYNC_DEADLINE_S
    assert stages.given["phase2"]["first_ids"] == [11, 12]
    assert stages.given["phase2"]["deadline_s"] == sync.PHASE2_SYNC_DEADLINE_S
    assert stages.given["extract_conversations"]["deadline_s"] == sync.CONVERSATION_SYNC_DEADLINE_S
    assert stages.given["images"]["deadline_s"] == min(
        IMAGE_CLASSIFY_BUDGET_S, sync.IMAGE_CLASSIFY_SYNC_BUDGET_S
    )
    assert stages.metadata()["last_sync_date"] != BEFORE
    assert stages.metadata()["last_sync_count"] == "1"


def test_a_run_that_loaded_nothing_registers_dedups_and_embeds_nothing(stages):
    stages.mail = 0
    stages.conversations = 0

    assert stages.run() == 0

    assert stages.calls == ["extract", "load", "phase1", "phase2", "export_conversations", "images"]


def test_conversations_loaded_alone_are_still_embedded(stages):
    stages.mail = 0

    assert stages.run() == 0

    assert "dedup" not in stages.calls
    assert stages.calls[-1] == "embed"


def test_heavy_attachments_wait_while_a_nightly_pass_reads_them(stages, monkeypatch):
    stages.text_pass_recent = True
    stages.run()
    assert stages.given["phase1"]["defer_heavy"] is True

    monkeypatch.setenv(sync.SYNC_HEAVY_ATTACHMENTS_ENV, "1")
    stages.run()
    assert stages.given["phase1"]["defer_heavy"] is False


def test_a_stop_during_extraction_ends_the_run_before_the_load(stages):
    stages.stops = [True]

    assert stages.run() == 143

    assert stages.calls == ["extract"]


def test_a_stop_during_conversation_extraction_ends_it_before_the_images(stages):
    stages.stops = [False, True]

    assert stages.run() == 143

    assert stages.calls[-1] == "extract_conversations"


def test_a_model_that_extracted_nothing_holds_the_sync_date_back(stages):
    stages.extraction = {"model_successes": 0, "model_failures": 3}

    assert stages.run() == 75

    assert stages.calls[-1] == "embed"
    assert stages.metadata()["last_sync_date"] == BEFORE
    assert stages.metadata()["last_sync_count"] == "1"


def test_a_quota_pause_returns_75_once_the_other_stages_ran(stages):
    stages.extraction = {"model_successes": 1, "quota_paused": True}

    assert stages.run() == 75

    assert stages.calls[-1] == "embed"
    assert stages.metadata()["last_sync_date"] != BEFORE


def test_an_embedding_failure_fails_a_run_that_loaded_mail(stages):
    stages.embed_error = RuntimeError("index unavailable")

    with pytest.raises(RuntimeError):
        stages.run()


def test_an_embedding_failure_after_conversations_alone_lets_the_run_finish(stages, capsys):
    stages.mail = 0
    stages.embed_error = RuntimeError("index unavailable")

    assert stages.run() == 0

    assert "index unavailable" in capsys.readouterr().err


def test_an_empty_store_stops_before_extracting(stages):
    conn = get_connection(str(stages.db))
    conn.execute("DELETE FROM sync_metadata")
    conn.commit()
    conn.close()

    assert stages.run() is None

    assert stages.calls == []


def test_a_store_that_is_not_there_exits_1(stages, tmp_path):
    stages.db = tmp_path / "absent.db"

    with pytest.raises(SystemExit) as exc:
        stages.run()

    assert exc.value.code == 1
    assert stages.calls == []


def test_a_sync_already_running_makes_this_one_skip(stages, tmp_path):
    lock = tmp_path / "state" / "sync.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)

    with open(lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert stages.run() == 0

    assert stages.calls == []
