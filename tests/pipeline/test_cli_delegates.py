"""The CLI keeps the argument parsing; each job's command hands its namespace to the
pipeline module that runs it, and returns that module's exit code."""

import importlib
import types

import pytest

from src import cli

JOBS = [
    ("cmd_sync", "sync"),
    ("cmd_teams_sync", "teams_sync"),
    ("cmd_whatsapp_sync", "whatsapp_sync"),
    ("cmd_calendar_sync", "calendar_sync"),
    ("cmd_process_sharepoint", "sharepoint"),
]


@pytest.mark.parametrize(("command", "module"), JOBS)
def test_the_command_runs_its_pipeline_module(command, module, monkeypatch):
    pipeline = importlib.import_module(f"src.pipeline.{module}")
    handed = []
    monkeypatch.setattr(pipeline, "run", lambda args: handed.append(args) or 75)
    args = types.SimpleNamespace(db="brain.db")

    assert getattr(cli, command)(args) == 75

    assert handed == [args]
