"""`brain ARGV` run in this process, as a test reads it: the exit code, stdout and stderr."""

import sys


def brain(monkeypatch, capsys, *argv: str) -> tuple[int, str, str]:
    """Run `brain ARGV` through src.cli.main; a normal return is exit code 0."""
    from src import cli

    monkeypatch.setattr(sys, "argv", ["brain", *argv])
    try:
        cli.main()
        code = 0
    except SystemExit as exc:
        # As the interpreter reads it: None is 0, a message is 1.
        code = exc.code if isinstance(exc.code, int) else int(exc.code is not None)
    out, err = capsys.readouterr()
    return code, out, err
