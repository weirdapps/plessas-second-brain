"""sync stages no mail itself: outlook_export does that, on every host.

On a Mac, sync ran the Apple Mail exporter unless told --skip-export. It
force-quit Mail.app, and took attachments from Mail.app instead of registering
the ones outlook-cli had downloaded. The exporter had been frozen since April.
"""

import sys
import types
from unittest.mock import patch

import pytest


def _refusing_module(name, function):
    """A module whose one function fails the test if anything calls it."""
    module = types.ModuleType(name)

    def refuse(*args, **kwargs):
        raise AssertionError(f"sync called {name}.{function}")

    setattr(module, function, refuse)
    return module


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_sync_registers_outlook_downloads_and_never_drives_mail_app(
    tmp_path, monkeypatch, platform
):
    from src import cli
    from src.store.schema import create_database

    db_path = tmp_path / "brain.db"
    conn = create_database(str(db_path))
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr("src.cli.DATA_ROOT", tmp_path)
    # Stand-ins, so a sync that still reaches for Mail.app fails here instead of
    # driving the real one.
    for name, function in [
        ("src.export.apple_mail", "export_emails"),
        ("src.export.attachments", "export_sync_attachments"),
    ]:
        monkeypatch.setitem(sys.modules, name, _refusing_module(name, function))
    args = types.SimpleNamespace(
        db=db_path, limit=None, engine="claude", workers=1, skip_export=False
    )
    extracted = {"extracted": 1, "failed": 0, "quota_paused": False}
    registered = {"ids": [], "registered": 0, "scanned": 0, "deferred": 0}

    with (
        patch("src.extract.local.run_extraction", return_value=extracted),
        patch("src.store.loader.load_extractions", return_value=1),
        patch(
            "src.export.outlook_attachments.register_downloaded_attachments",
            return_value=registered,
        ) as register,
        patch("src.store.dedup_people.run_dedup", return_value={"total_reduced": 0}),
        patch("src.store.embeddings.build_index"),
        patch("src.extract.attachment_pipeline.run_phase1", return_value={"processed": 0}),
        patch("src.export.conversation_export.export_conversations", return_value={"exported": 0}),
        patch("src.extract.image_pipeline.run_backfill", return_value={}),
    ):
        cli.cmd_sync(args)

    register.assert_called_once()


def test_the_skip_export_flag_the_schedules_pass_still_reaches_sync(monkeypatch, tmp_path):
    """sb-outlook-sync and sb-noon-catchup pass --skip-export. Removing the ignored
    flag would make argparse exit 2 in both, every run."""
    from src import cli

    seen = []
    monkeypatch.setattr(cli, "cmd_sync", lambda args: seen.append(args.skip_export))
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setattr(
        sys, "argv", ["brain", "--db", str(tmp_path / "brain.db"), "sync", "--skip-export"]
    )

    cli.main()

    assert seen == [True]


def test_sync_on_an_empty_store_says_how_to_fill_it(tmp_path, capsys):
    """It skips an empty store, and used to send the reader to an export command
    that no longer exists."""
    from src import cli
    from src.store.schema import create_database

    db_path = tmp_path / "brain.db"
    create_database(str(db_path)).close()
    args = types.SimpleNamespace(
        db=db_path, limit=None, engine="claude", workers=1, skip_export=False
    )

    with patch("src.extract.local.run_extraction") as extraction:
        cli.cmd_sync(args)

    out = capsys.readouterr().out
    assert "python -m src.extract.local" in out
    assert "python -m src.cli load" in out
    extraction.assert_not_called()
