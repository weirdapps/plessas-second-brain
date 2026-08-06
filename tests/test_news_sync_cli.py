"""CLI wiring for the news source, plus the sync-path image backfill flag."""

import json
import sys
import types
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest

from src.cli import cmd_news_sync, cmd_sync, main
from src.export import news_export as real_news_export
from src.store.schema import create_database, run_migrations, set_schema_version

STUB_PIPELINES = ("digest", "monitor", "market", "stack")


@pytest.fixture
def news_export_stub(monkeypatch):
    """Stand-in for src.export.news_export (imported lazily inside the command).

    export_news is autospecced against the real function, so renaming one of its
    parameters fails the kwarg assertions instead of silently passing.
    """
    module = types.ModuleType("src.export.news_export")
    module.PIPELINES = STUB_PIPELINES
    module.DEFAULT_RELEVANCE_THRESHOLD = real_news_export.DEFAULT_RELEVANCE_THRESHOLD
    module.export_news = create_autospec(
        real_news_export.export_news,
        return_value={"syntheses": 2, "articles": 7, "skipped": 3, "batch_files": ["batch-70000"]},
    )
    monkeypatch.setitem(sys.modules, "src.export.news_export", module)
    return module


@pytest.fixture
def staging_root(tmp_path, monkeypatch):
    """Redirect DATA_ROOT so the staging scan never reads the real data/ tree."""
    monkeypatch.setattr("src.cli.DATA_ROOT", tmp_path)
    return tmp_path / "staging"


def _write_batch(staging_root, number, message_ids):
    """Write a staging batch file in the shape export_news emits."""
    staging_root.mkdir(parents=True, exist_ok=True)
    path = staging_root / f"batch-{number:05d}.json"
    path.write_text(
        json.dumps(
            {
                "batch_number": number,
                "source": "news-reader",
                "emails": [{"message_id": mid, "subject": "s"} for mid in message_ids],
            }
        ),
        encoding="utf-8",
    )
    return path


def _news_args(tmp_path, **overrides):
    defaults = {
        "db": tmp_path / "brain.db",
        "news_db": tmp_path / "news.db",
        "relevance": real_news_export.DEFAULT_RELEVANCE_THRESHOLD,
        "pipeline": None,
        "limit": 0,
        "dry_run": False,
    }
    defaults.update(overrides)
    (defaults["news_db"]).touch()
    return type("Args", (), defaults)()


# --- Task 1: sync must not re-scan already-processed images -------------------


def test_sync_passes_unprocessed_only_to_image_backfill(tmp_path):
    """Step 8 of sync must skip messages already processed, or every run re-reads
    the same 200 newest images and fights the hourly sync for the write lock."""
    db_path = tmp_path / "brain.db"
    conn = create_database(str(db_path))
    # create_database only builds the base tables; replay the migrations for the rest
    set_schema_version(conn, 0)
    run_migrations(conn)
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', ?)",
        ("2026-01-01T00:00:00",),
    )
    conn.commit()
    conn.close()

    args = type(
        "Args",
        (),
        {
            "db": db_path,
            "limit": None,
            "engine": "claude",
            "workers": 1,
            "skip_export": True,
        },
    )()

    with (
        patch("src.extract.local.run_extraction"),
        patch("src.store.loader.load_extractions", return_value=0),
        patch("src.extract.attachment_pipeline.run_phase1", return_value={"processed": 0}),
        patch("src.export.conversation_export.export_conversations", return_value={"exported": 0}),
        patch("src.extract.image_pipeline.run_backfill") as mock_backfill,
    ):
        mock_backfill.return_value = {"scanned": 0, "classified": 0, "missing": 0, "failed": 0}
        cmd_sync(args)

    mock_backfill.assert_called_once()
    assert mock_backfill.call_args.kwargs["unprocessed_only"] is True


# --- Task 2: news-sync argument parsing --------------------------------------


def test_news_sync_parser_defaults():
    """`brain news-sync` with no flags uses the configured news DB and default relevance."""
    from src.config import NEWS_DB_PATH

    with (
        patch("src.cli.cmd_news_sync") as mock_cmd,
        patch.object(sys, "argv", ["brain", "news-sync"]),
    ):
        main()

    args = mock_cmd.call_args[0][0]
    assert args.news_db == NEWS_DB_PATH
    assert args.relevance == real_news_export.DEFAULT_RELEVANCE_THRESHOLD
    assert args.pipeline is None
    assert args.limit == 0
    assert args.dry_run is False


def test_news_sync_relevance_default_tracks_exporter_constant(monkeypatch):
    """The parser default is DEFAULT_RELEVANCE_THRESHOLD, not a second copy of 60."""
    monkeypatch.setattr(real_news_export, "DEFAULT_RELEVANCE_THRESHOLD", 42)

    with (
        patch("src.cli.cmd_news_sync") as mock_cmd,
        patch.object(sys, "argv", ["brain", "news-sync"]),
    ):
        main()

    assert mock_cmd.call_args[0][0].relevance == 42


def test_news_sync_rejects_unknown_pipeline():
    """A wrong-case pipeline must fail loudly, not stage nothing and exit 0."""
    argv = ["brain", "news-sync", "--pipeline", "Digest"]
    with (
        patch("src.cli.cmd_news_sync") as mock_cmd,
        patch.object(sys, "argv", argv),
        pytest.raises(SystemExit) as excinfo,
    ):
        main()

    assert excinfo.value.code == 2
    mock_cmd.assert_not_called()


def test_news_sync_limit_help_describes_total_records(capsys):
    """--limit caps TOTAL records (syntheses drain first), so the help must say so."""
    with patch.object(sys, "argv", ["brain", "news-sync", "--help"]), pytest.raises(SystemExit):
        main()

    help_text = " ".join(capsys.readouterr().out.split())
    assert "Max articles to stage" not in help_text
    assert "total records" in help_text.lower()


def test_news_sync_parser_flags():
    """--pipeline is repeatable; the other flags override their defaults."""
    argv = [
        "brain",
        "news-sync",
        "--news-db",
        "/tmp/other-news.db",
        "--relevance",
        "80",
        "--pipeline",
        "digest",
        "--pipeline",
        "monitor",
        "--limit",
        "5",
        "--dry-run",
    ]
    with patch("src.cli.cmd_news_sync") as mock_cmd, patch.object(sys, "argv", argv):
        main()

    args = mock_cmd.call_args[0][0]
    assert args.news_db == Path("/tmp/other-news.db")
    assert args.relevance == 80
    assert args.pipeline == ["digest", "monitor"]
    assert args.limit == 5
    assert args.dry_run is True


# --- Task 2: news-sync behaviour ---------------------------------------------


def test_news_sync_calls_export_news_with_defaults(tmp_path, news_export_stub, staging_root):
    """All four pipelines by default, staging under the configured data root."""
    args = _news_args(tmp_path)
    create_database(str(args.db)).close()

    cmd_news_sync(args)

    kwargs = news_export_stub.export_news.call_args.kwargs
    assert kwargs["news_db"] == args.news_db
    assert kwargs["staging_dir"] == staging_root
    assert kwargs["relevance_threshold"] == real_news_export.DEFAULT_RELEVANCE_THRESHOLD
    assert kwargs["pipelines"] == list(STUB_PIPELINES)
    assert kwargs["exported_ids"] == set()
    assert kwargs["limit"] == 0


def test_news_sync_passes_selected_pipelines(tmp_path, news_export_stub, staging_root):
    """An explicit --pipeline selection replaces the default set."""
    args = _news_args(tmp_path, pipeline=["monitor"], relevance=75, limit=10)
    create_database(str(args.db)).close()

    cmd_news_sync(args)

    kwargs = news_export_stub.export_news.call_args.kwargs
    assert kwargs["pipelines"] == ["monitor"]
    assert kwargs["relevance_threshold"] == 75
    assert kwargs["limit"] == 10


def test_news_sync_collects_existing_news_message_ids(tmp_path, news_export_stub, staging_root):
    """exported_ids holds only the news:* message_ids already in the brain."""
    args = _news_args(tmp_path)
    conn = create_database(str(args.db))
    for message_id in ("news:article:abc", "news:synthesis:12", 987654321):
        conn.execute(
            "INSERT INTO emails (message_id, date_received, subject) VALUES (?, ?, ?)",
            (message_id, "2026-08-01T09:00:00", "s"),
        )
    conn.commit()
    conn.close()

    cmd_news_sync(args)

    kwargs = news_export_stub.export_news.call_args.kwargs
    assert kwargs["exported_ids"] == {"news:article:abc", "news:synthesis:12"}


def test_news_sync_tolerates_missing_brain_db(tmp_path, news_export_stub, staging_root):
    """No brain DB yet — export everything rather than crashing."""
    args = _news_args(tmp_path)

    cmd_news_sync(args)

    assert news_export_stub.export_news.call_args.kwargs["exported_ids"] == set()


def test_news_sync_skips_ids_already_staged(tmp_path, news_export_stub, staging_root):
    """Re-running before the next drain must not re-stage what is already staged."""
    args = _news_args(tmp_path)
    conn = create_database(str(args.db))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (?, ?, ?)",
        ("news:article:in-db", "2026-08-01T09:00:00", "s"),
    )
    conn.commit()
    conn.close()
    _write_batch(staging_root, 70000, ["news:synthesis:digest:1", "regular-outlook-id"])
    _write_batch(staging_root, 70001, ["news:article:already-staged"])

    cmd_news_sync(args)

    assert news_export_stub.export_news.call_args.kwargs["exported_ids"] == {
        "news:article:in-db",
        "news:synthesis:digest:1",
        "news:article:already-staged",
    }


def test_news_sync_tolerates_unreadable_batch_files(tmp_path, news_export_stub, staging_root):
    """Truncated or oddly shaped batch files are skipped, never fatal."""
    args = _news_args(tmp_path)
    _write_batch(staging_root, 70000, ["news:article:good"])
    (staging_root / "batch-70001.json").write_text("{not json", encoding="utf-8")
    (staging_root / "batch-70002.json").write_text('{"emails": "not-a-list"}', encoding="utf-8")
    (staging_root / "batch-70003.json").write_text("[]", encoding="utf-8")

    cmd_news_sync(args)

    assert news_export_stub.export_news.call_args.kwargs["exported_ids"] == {"news:article:good"}


def test_news_sync_dry_run_reports_counts_without_writing(
    tmp_path, news_export_stub, staging_root, capsys
):
    """--dry-run must report what would be staged, into a throwaway dir it discards."""
    args = _news_args(tmp_path, dry_run=True)
    create_database(str(args.db)).close()

    cmd_news_sync(args)

    kwargs = news_export_stub.export_news.call_args.kwargs
    assert kwargs["staging_dir"] != staging_root
    assert not Path(kwargs["staging_dir"]).exists()
    assert not staging_root.exists()
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "digest" in out
    assert "Would stage syntheses: 2" in out
    assert "Would stage articles: 7" in out
    assert "Would skip (already exported): 3" in out


def test_news_sync_prints_summary(tmp_path, news_export_stub, staging_root, capsys):
    """The terse summary reports syntheses, articles, skipped and batch files."""
    args = _news_args(tmp_path)
    create_database(str(args.db)).close()

    cmd_news_sync(args)

    out = capsys.readouterr().out
    assert "Syntheses: 2" in out
    assert "Articles: 7" in out
    assert "Skipped: 3" in out
    assert "batch-70000" in out


def test_news_sync_errors_when_news_db_missing(tmp_path, news_export_stub, staging_root):
    """A missing news database is a clear error, not a traceback."""
    args = _news_args(tmp_path)
    args.news_db.unlink()

    with pytest.raises(SystemExit):
        cmd_news_sync(args)

    news_export_stub.export_news.assert_not_called()
