"""The owner's reports stamped with a written date, not only a tag or an ISO date.

On the replica of 2026-10-16, 554 of the owner's own machine reports classified as mail:
news-monitor digests stamped like "<ORG> MONITOR | Tue 9 JUN 11:00 | 8 mentions" and
investment-committee reports like "investment committee -- May 27, 2026". The stamp rule
knew a bracketed tag and an ISO date only, so they passed every default filter with their
1,003 decisions. A date written with a month's name and its year, or a weekday, a day and a
month, is a job's stamp too. The rest of the rule holds: the owner sent it to himself alone,
it is not a reply or a forward, and it names something besides the stamp. Every subject here
is made up.
"""

import pytest

from src.store import source_class as sc

OWNER = "owner@example.com"
COLLEAGUE = "colleague@example.com"


def owner_to_self(subject: str) -> str:
    return sc.classify("Inbox", OWNER, subject, [OWNER], owner_pattern=OWNER)


@pytest.mark.parametrize(
    "subject",
    [
        "ACME MONITOR | Tue 9 JUN 11:00 | 12 mentions",
        "ACME Monitor | Wed 29 APR 16:00 | 11 mentions | ALERT",
        "News Digest | Sun 24 MAY 09:00 | 1520 articles",
        "Stack | 980 articles | Sat 9 JUN 21:00",
        "investment committee -- May 27, 2026",
        "Investment Committee - Mar 21, 2026 | DEFENSIVE | SELL 2, TRIM 5, ADD 8",
        "investment committee - may 06, 2026 | risk_on | sell 2, trim 11",
        "daily committee brief - June 14, 2026 (v2)",
        "daily intelligence report - 9 jun 2026",
        "theme and opportunity report - 9 June 2026",
        "weekly census draft, sat 23 may",
        "market post | wednesday 9 may",
        "Morning Briefing - Feb 12, 2026 - Mixed Sentiment",
        "Topic Brief | Mon 4 MAY 08:00 | agents",
    ],
)
def test_a_report_stamped_with_a_written_date_is_automation(subject):
    assert owner_to_self(subject) == "automation"


@pytest.mark.parametrize(
    "subject",
    [
        # Only the stamp: a person titles a note by its date as easily as a job.
        "May 27, 2026",
        "Tue 9 JUN",
        "Tue 9 JUN 11:00",
        "27 May 2026",
        # A month with no day, or a day and a month with neither a year nor a weekday: how a
        # person writes about a date, not how a job stamps a run.
        "call with Nikos about the May budget",
        "budget review March 2026",
        "notes for 27 May",
        "agenda for the meeting on 9/6",
        # Words that start like a month or a weekday.
        "Mayday drill plan 12, 2026 edition",
        "Marsh report 12, 2026",
        "Sunday 9 lessons",
        "Monitoring 4 May",
        # A reply or a forward is someone answering or passing it on.
        "RE: investment committee -- May 27, 2026",
        "FW: ACME MONITOR | Tue 9 JUN 11:00 | 12 mentions",
        "Απ: Investment Committee - Mar 21, 2026 | DEFENSIVE",
    ],
)
def test_what_is_not_a_written_date_stamp_stays_mail(subject):
    assert owner_to_self(subject) == "mail"


def test_a_stamped_report_from_someone_else_is_mail():
    subject = "Daily Journey Monitoring - Tue 30 SEP | 1 immediate focus"

    assert sc.classify("Inbox", COLLEAGUE, subject, [OWNER], owner_pattern=OWNER) == "mail"


def test_a_stamped_report_the_owner_also_sent_to_someone_else_is_mail():
    subject = "investment committee -- May 27, 2026"

    assert sc.classify("Sent Items", OWNER, subject, [OWNER, COLLEAGUE], owner_pattern=OWNER) == (
        "mail"
    )


def test_a_report_from_a_second_owner_address_is_automation():
    """The owner sends from more than one address: each must be in BRAIN_USER_EMAIL_PATTERN."""
    pattern = f"{OWNER}, owner.home@example.org"
    subject = "Investment Committee - Apr 20, 2026 | RISK-ON | TRIM 1"

    assert sc.classify("Inbox", "owner.home@example.org", subject, [OWNER], pattern) == (
        "automation"
    )
    assert sc.classify("Inbox", "owner.home@example.org", subject, [OWNER], OWNER) == "mail"


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("[nightly] health report", "automation"),
        ("backup report 2026-05-28", "automation"),
        ("2026-05-28", "mail"),
        ("[nightly]", "mail"),
    ],
)
def test_the_tag_and_iso_date_stamps_are_unchanged(subject, expected):
    assert owner_to_self(subject) == expected
