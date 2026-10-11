"""One sync run saves the embedding index at most once, however many stages add vectors.

Step 5 embedded the new mail right after the people dedup, and Step 7 embedded again
whenever it loaded conversations. Each pass loads the whole index (embeddings.npz, 1.7 GB
on the producer) and writes it out again: the hourly run of 2026-10-10 22:12 saved it twice
to load one email, and the mail job ran at its 6 GB MemoryMax. The attachment summaries
Step 6 wrote and the image descriptions Step 8 wrote, which Step 5 had already passed,
waited for the next run's pass besides.
"""

import types
from unittest.mock import patch

import numpy as np
import pytest

from src.store import embeddings
from src.store.schema import create_database


@pytest.fixture
def store(tmp_path, monkeypatch):
    from src import llm_deadline

    db_path = tmp_path / "brain.db"
    conn = create_database(str(db_path))
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("src.cli.DATA_ROOT", tmp_path)
    monkeypatch.setattr("src.cli.ATTACHMENTS_DIR", tmp_path / "attachments")
    monkeypatch.setattr(llm_deadline, "_detect_systemd_unit", lambda: None)
    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", tmp_path / "embeddings.npz")
    monkeypatch.setattr(embeddings, "LOG_FILE", tmp_path / "embeddings.log")
    monkeypatch.setattr(embeddings, "_get_client", lambda: None)
    monkeypatch.setattr(
        embeddings,
        "generate_embeddings",
        lambda texts, client=None: np.ones((len(texts), 4), dtype=np.float32),
    )
    return db_path


def _write(db_path, sql, params=()):
    from src.store.schema import get_connection

    conn = get_connection(str(db_path))
    cur = conn.execute(sql, params)
    conn.commit()
    conn.close()
    return cur.lastrowid


def _sync(db_path, *, conversations=True, embed_error=None):
    """cmd_sync over stages that each add something to embed: Step 3 an email, Step 6 an
    attachment summary, Step 7 a conversation. Returns what the run passed to the saver."""
    from src import cli

    added: dict[str, int] = {}

    def load_mail(*args, **kwargs):
        added["email"] = _write(
            db_path,
            "INSERT INTO emails (message_id, date_received, subject, summary) "
            "VALUES ('m-1', '2026-10-10T22:00:00Z', 'figures', 'the quarter closed')",
        )
        return 1

    def summarise_attachment(*args, **kwargs):
        attachment = _write(
            db_path,
            "INSERT INTO attachments (email_id, message_id, filename, file_path, exported_at) "
            "VALUES (?, 'm-1', 'q3.pdf', '/nowhere/q3.pdf', '2026-10-10')",
            (added["email"],),
        )
        added["attachment"] = _write(
            db_path,
            "INSERT INTO attachment_content (attachment_id, extraction_status, llm_status, "
            "summary) VALUES (?, 'extracted', 'extracted', 'the q3 deck')",
            (attachment,),
        )
        return {"extracted": 1, "failed": 0}

    def load_conversations(*args, **kwargs):
        added["conversation"] = _write(
            db_path,
            "INSERT INTO conversations (session_id, started_at, ended_at, project_name, "
            "summary, created_at) VALUES ('s-1', '2026-10-10T20:00:00Z', "
            "'2026-10-10T21:00:00Z', 'p', 'planned the release', '2026-10-10')",
        )
        return 1

    saves: list[list[int]] = []
    real_save = embeddings._atomic_savez

    def spy(path, ids, *rest, **kwargs):
        saves.append([int(i) for i in ids])
        real_save(path, ids, *rest, **kwargs)

    args = types.SimpleNamespace(
        db=db_path, limit=None, engine="claude", workers=1, skip_export=True
    )
    ok = {"extracted": 1, "failed": 0, "quota_paused": False}
    p1 = {"processed": 1, "extracted": 1, "failed": 0, "skipped": 0}
    registered = {"ids": [], "registered": 0, "scanned": 0, "deferred": 0}
    exported = {"exported": 1 if conversations else 0}
    with (
        patch("src.extract.local.run_extraction", return_value=ok),
        patch("src.store.loader.load_extractions", side_effect=load_mail),
        patch(
            "src.export.outlook_attachments.register_downloaded_attachments",
            return_value=registered,
        ),
        patch("src.store.dedup_people.run_dedup", return_value={"total_reduced": 0}),
        patch("src.extract.attachment_pipeline.run_phase1", return_value=p1),
        patch("src.extract.attachment_pipeline.run_phase2", side_effect=summarise_attachment),
        patch("src.export.conversation_export.export_conversations", return_value=exported),
        patch("src.extract.local.run_conversation_extraction"),
        patch("src.store.loader.load_conversations", side_effect=load_conversations),
        patch("src.extract.image_pipeline.run_backfill", return_value={}) as images,
        patch.object(embeddings, "_atomic_savez", side_effect=spy),
    ):
        if embed_error is not None:
            with patch.object(embeddings, "generate_embeddings", side_effect=embed_error):
                with pytest.raises(type(embed_error)):
                    cli.cmd_sync(args)
        else:
            assert cli.cmd_sync(args) == 0
    return saves, added, images


def test_a_run_whose_stages_all_add_vectors_saves_the_index_once(store):
    saves, added, _ = _sync(store)

    assert len(saves) == 1
    assert set(saves[0]) == {
        added["email"],
        -added["attachment"],
        embeddings.CONVERSATION_ID_OFFSET - added["conversation"],
    }


def test_the_one_save_comes_after_the_last_stage(store):
    """The summary Step 6 wrote is embedded in this run, not the next one."""
    saves, added, images = _sync(store, conversations=False)

    assert len(saves) == 1
    assert -added["attachment"] in saves[0]
    images.assert_called_once()


def test_a_run_that_loaded_mail_and_cannot_embed_it_still_fails_after_the_other_steps(store):
    """As Step 5 did, but the attachment, conversation and image steps ran first."""
    saves, _, images = _sync(store, embed_error=RuntimeError("embedding service down"))

    assert saves == []
    images.assert_called_once()
