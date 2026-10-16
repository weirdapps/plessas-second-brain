"""Run a test in a given local time zone, the producer's (Europe/Athens), whatever the host's.

A stamp stored with no offset is read as local time, as the producer wrote it, by Python
(datetime.astimezone) and by SQLite (the 'utc' modifier). Both take the zone from the C library,
so the test sets TZ and calls time.tzset(), and sets them back afterwards. CI runs in UTC, where
local time and UTC agree and a test of the conversion would prove nothing.
"""

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def local_zone(name: str = "Europe/Athens") -> Iterator[None]:
    before = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()
