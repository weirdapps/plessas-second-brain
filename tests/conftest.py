"""Session-wide test isolation.

Two things had to be got right by hand in every test file, and twice in the four
days before this file existed they were not, each time costing a red CI push:

  5afd052 "redirect LOG_FILE so the conversation tests do not need data/" -- six
  tests passed locally and failed all six on CI, because `log()` opens
  `data/extract.log` and this Mac has a `data/` tree while CI does not.

  12ad9c5 "patch _get_client_and_model where it is imported from" -- a test
  reached a real client path.

Both are the same class: a test that silently borrows the developer's machine.
The two autouse fixtures below make that impossible rather than reviewable. The
per-host settings file src/config.py reads is redirected for the same reason.

The data root is redirected at import of this file, NOT in a session fixture.
src/config.py resolves DATA_ROOT, DEFAULT_DB, ATTACHMENTS_DIR, RAW_BATCH_DIR,
CONVERSATION_STAGING_DIR and SHAREPOINT_DATA_DIR at import time, and a test
module that imports any of them at module level is imported during COLLECTION,
which happens before the first fixture runs. A session fixture would therefore
protect the lazily-importing tests and quietly miss the eager ones, which is the
worst of both. pytest_configure runs before collection but NOT before a child
directory's conftest, which can import src.config first; this file's own import
precedes both, so it catches every one. (See tests/test_config_paths.py.)

`no_network` blocks socket creation for the whole session. A test that genuinely
needs the network marks itself `@pytest.mark.allow_network`; there are none
today, and adding one should be a deliberate act.
"""

import os
import socket
import tempfile

import pytest

# At module level, not in pytest_configure. When a run targets a subdirectory
# (`pytest tests/teams/...`), pytest imports that directory's conftest as an
# initial conftest BEFORE pytest_configure runs, and tests/teams/conftest.py
# imports src.store.schema and therefore src.config, which resolves every path
# and applies the settings file at import. This file is imported before any
# child conftest, so setting the environment here is early enough in every case.
#
# Respect an explicit override so a developer can still point the suite at a
# real tree on purpose; otherwise nothing under the repo's own data/ is
# reachable from a test.
if not os.environ.get("BRAIN_DATA_DIR"):
    os.environ["BRAIN_DATA_DIR"] = tempfile.mkdtemp(prefix="brain-test-data-")
# src/config.py applies the per-host settings file at import. A developer's real
# one would hand every test this machine's identity and tenant, which CI never
# has, so point it at a path that cannot exist, and drop a tenant exported in the
# shell for the same reason. Unconditional, unlike the data root above: there is
# no legitimate reason to test against either.
os.environ["BRAIN_CONFIG_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="brain-test-config-"), "absent"
)
os.environ.pop("SHAREPOINT_HOST", None)
# A replica's pull job stamps ~/.second-brain/db-pull.stamp, and write commands
# refuse to run where it exists. CI has no stamp, so a test driving a write
# command would pass there and exit 2 on a replica. tests/test_replica_guard.py
# clears this to test the stamp itself.
os.environ["BRAIN_ROLE"] = "producer"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_network: let this test open real sockets (default is blocked)",
    )


class _BlockedSocket(socket.socket):
    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "This test tried to open a network socket. Mock the call, or mark the "
            "test @pytest.mark.allow_network if it genuinely needs the network."
        )


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Block real sockets unless the test opts out."""
    if request.node.get_closest_marker("allow_network"):
        return
    monkeypatch.setattr(socket, "socket", _BlockedSocket)
