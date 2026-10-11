"""The jobs the timers run that go through several stages, one module each.

src/cli.py parses the arguments and calls each module's run(args); each module holds
its orchestrator and the stage budgets it is held to.
"""

# The shared "re-authenticate" exit code, the same as the three M365 CLIs'.
EXIT_REAUTH = 4
