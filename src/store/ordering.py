"""How decisions, key facts and action items are dated and ordered, wherever they are listed.

query_decisions, the person and topic dossiers (src/store/context.py), meeting_prep and the
dossiers recall attaches each sorted these rows their own way. Decisions went by decision_date,
which the model leaves NULL on 98% of them, so the order was whatever the index yielded: the top
three correspondents' dossiers held decisions from months back while each had decisions from the
day before. Key facts had no ORDER BY. Open actions went by deadline ascending, so a page opened
on deadlines years past. Every one of those lists now sorts by the rules below, and every row
says how it was dated:

    date         a decision's own date when it reads as an ISO date, else its parent's date;
                 for a fact or an action, its parent's date
    parent_date  the date of the item the row was extracted from: the email, the Teams or
                 WhatsApp session, the meeting or the Claude Code conversation

The SQL reads the parents under the aliases every caller joins them as: e (emails), tt
(teams_threads), wt (whatsapp_threads), ce (calendar_events) and c (conversations). A list built
on emails alone passes EMAIL_DATE as the parent's date.
"""

# An ISO date this store can sort and compare. The model also writes free text where a date
# belongs ('null', 'Q3 2026', '1 day before'), which sorted after every date.
ISO_DATE = "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"

# The parent's date with every parent kind LEFT JOINed, and with emails alone.
PARENT_DATE = "COALESCE(e.date_received, tt.started_at, wt.started_at, ce.start_at, c.started_at)"
EMAIL_DATE = "e.date_received"


def decision_date(parent_date: str = PARENT_DATE) -> str:
    """A decision's date (alias d): its own when it reads as an ISO date, else its parent's."""
    return f"COALESCE(CASE WHEN d.decision_date {ISO_DATE} THEN d.decision_date END, {parent_date})"


def decision_dates(parent_date: str = PARENT_DATE) -> str:
    """The `date` and `parent_date` columns of a decision row."""
    return f"{decision_date(parent_date)} AS date, {parent_date} AS parent_date"


def parent_dates(parent_date: str = PARENT_DATE) -> str:
    """The `date` and `parent_date` columns of a fact or an action row, dated by its parent."""
    return f"{parent_date} AS date, {parent_date} AS parent_date"


def decision_order(parent_date: str = PARENT_DATE) -> str:
    """ORDER BY terms for decisions (alias d): newest first, the newest row first on a tie."""
    return f"{decision_date(parent_date)} DESC, {parent_date} DESC, d.id DESC"


def fact_order(parent_date: str = PARENT_DATE) -> str:
    """ORDER BY terms for key facts (alias kf): newest parent first."""
    return f"{parent_date} DESC, kf.id DESC"


def action_order(parent_date: str = PARENT_DATE, alias: str = "a") -> str:
    """ORDER BY terms for action items: the actionable first, in three buckets.

        0  upcoming: a real deadline today or later, soonest first
        1  undated: no deadline, or free text, newest parent first
        2  overdue: a real deadline in the past, most recently missed first

    Nothing is hidden, since an overdue commitment is still one, but most dated open items are
    overdue (16,475 of 20,518 on 2026-09-09), and sorted by deadline they filled every page with
    items missed long ago.
    """
    deadline = f"{alias}.deadline"
    upcoming = f"{deadline} {ISO_DATE} AND {deadline} >= date('now')"
    return f"""CASE WHEN {upcoming} THEN 0 WHEN {deadline} {ISO_DATE} THEN 2 ELSE 1 END ASC,
               CASE WHEN {upcoming} THEN {deadline} END ASC,
               CASE WHEN {deadline} {ISO_DATE} AND {deadline} < date('now')
                    THEN {deadline} END DESC,
               {parent_date} DESC, {alias}.id DESC"""
