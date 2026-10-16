"""`brain maint sharepoint-shells`: the hollow documents an old SharePoint fetch left.

A "SharePoint page shell" is a text-only document with no text (src/extract/sharepoint_ingest.py
files a fetched file as one). Before 2026-09-30 every link was fetched as a file, and a page, a
home page, a notification-settings link or a viewer answered with its page frame: an .aspx file,
or "sharepoint-download (N)". The backlog pass of 2026-09-30 (`sharepoint-ingest-fetched`)
stored each frame left on disk as a document, with no text, and a one-off script, since retired,
labelled them with SHELL_REASON. Then the repair of that week (src/export/sharepoint_repair.py)
read the pages again, into documents of their own, and pointed their links at those, and left the
shells: 1,106 of the producer's 3,680 standalone documents, findable by title only (audit
2026-10-16). Today's fetch makes none: a page is read with `sharepoint-cli page`, and a fetched
file that yields no text makes no document (ingest_fetched_file's require_text).

Each shell is judged by its links, the sharepoint_links rows that name it as their document or
whose target is the one its subject names (sharepoint_ingest._label):

    held         a link to its target names another document that holds text: the page read
                 again, or the file fetched since. The shell is a hollow twin
    not_content  every link to it is to nothing readable (a home page, a settings page, a site
                 root: sharepoint_fetcher.link_kind), as the fetcher now settles them
    refetch      a link to it points at a file or a page, on a tenant we hold a session for,
                 and no document holds its content: the link is queued for the retry pass
                 (fetched_at and the attempts cleared, as ingest_fetched_backlog queues a link
                 that lost its file), and the shell waits for it. The next run finds it held
    orphan       no link names it (the frame was stored by its file name alone), or its only
                 links are to another tenant: nothing can fetch it again

With --apply the held, not_content and orphan shells are forgotten (src/store/forget.py: the
rows, and no vector, since a shell has no summary), a link that named one names none, and the
refetch links are queued. It fetches nothing: `brain process-sharepoint`, or the nightly pass,
reads what it queued. A dry run, the default, only counts.
"""

import sqlite3
from collections import defaultdict

from src.export.sharepoint_fetcher import link_kind
from src.extract.sharepoint_ingest import _label
from src.store.forget import forget_documents

# The label the retired relabel script gave a shell (the text is the row's extraction_error).
SHELL_REASON = (
    "SharePoint page shell: the fetch returned the page frame (an .aspx page, or a sign-in or"
    " viewer frame), not the document; the SharePoint pass fetches it again"
)
_SUBJECT = "[SharePoint] "
CLASSES = ("held", "not_content", "refetch", "orphan")


def _shells(conn: sqlite3.Connection) -> dict[int, str]:
    """{message id: the target its subject names, folded} of every shell."""
    return {
        message_id: subject[len(_SUBJECT) :].lower() if subject.startswith(_SUBJECT) else ""
        for message_id, subject in conn.execute(
            "SELECT e.message_id, e.subject FROM emails e"
            " JOIN attachments a ON a.email_id = e.id"
            " JOIN attachment_content ac ON ac.attachment_id = a.id"
            " WHERE e.mailbox_name = 'External' AND a.file_path LIKE 'text:sharepoint:%'"
            " AND ac.extraction_error LIKE 'SharePoint page shell%'"
            " AND COALESCE(ac.extracted_text, '') = ''"
        )
    }


def _with_text(conn: sqlite3.Connection, message_ids) -> set[int]:
    """Which of these documents hold text."""
    ids = sorted({int(m) for m in message_ids if m is not None})
    held: set[int] = set()
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        held.update(
            m
            for (m,) in conn.execute(
                "SELECT DISTINCT e.message_id FROM emails e"
                " JOIN attachments a ON a.email_id = e.id"
                " JOIN attachment_content ac ON ac.attachment_id = a.id"
                f" WHERE e.message_id IN ({','.join('?' * len(chunk))})"
                " AND COALESCE(ac.extracted_text, '') != ''",
                chunk,
            )
        )
    return held


def classify(conn: sqlite3.Connection) -> dict[str, list[int]]:
    """The shells by what their links say of them: {class: message ids}, CLASSES each."""
    shells = _shells(conn)
    links = conn.execute(
        "SELECT url, last_status, document_message_id FROM sharepoint_links"
    ).fetchall()
    by_target: dict[str, list[tuple]] = defaultdict(list)
    by_document: dict[int, list[tuple]] = defaultdict(list)
    for link in links:
        by_target[_label(link[0], "").lower()].append(link)
        if link[2] is not None:
            by_document[int(link[2])].append(link)
    with_text = _with_text(conn, (link[2] for link in links))

    out: dict[str, list[int]] = {name: [] for name in CLASSES}
    for message_id, target in sorted(shells.items()):
        mine = {link[0]: link for link in by_document[message_id] + by_target.get(target, [])}
        named = list(mine.values())
        if any(doc is not None and doc != message_id and doc in with_text for _, _, doc in named):
            out["held"].append(message_id)
        elif named and all(
            status == "not-content" or link_kind(url) == "not-content" for url, status, _ in named
        ):
            out["not_content"].append(message_id)
        elif any(
            link_kind(url) != "not-content" and status != "unsupported-host"
            for url, status, _ in named
        ):
            out["refetch"].append(message_id)
        else:
            out["orphan"].append(message_id)
    return out


def retire(conn: sqlite3.Connection, found: dict[str, list[int]]) -> dict:
    """Forget the held, not_content and orphan shells and queue the links of the refetch ones."""
    gone = found["held"] + found["not_content"] + found["orphan"]
    queued = 0
    refetch = set(found["refetch"])
    if refetch:
        shells = _shells(conn)
        targets = {shells[m] for m in refetch if m in shells}
        urls = [
            url
            for url, doc in conn.execute("SELECT url, document_message_id FROM sharepoint_links")
            if (doc is not None and int(doc) in refetch)
            or (_label(url, "").lower() in targets and link_kind(url) != "not-content")
        ]
        for url in urls:
            queued += conn.execute(
                "UPDATE sharepoint_links SET fetched_at = NULL, attempts = 0,"
                " document_message_id = CASE WHEN document_message_id IN"
                f" ({','.join('?' * len(refetch))}) THEN NULL ELSE document_message_id END"
                " WHERE url = ? AND last_status != 'unsupported-host'",
                [*sorted(refetch), url],
            ).rowcount
    for start in range(0, len(gone), 500):
        chunk = gone[start : start + 500]
        conn.execute(
            "UPDATE sharepoint_links SET document_message_id = NULL"
            f" WHERE document_message_id IN ({','.join('?' * len(chunk))})",
            chunk,
        )
    conn.commit()
    forgotten = forget_documents(conn, gone) if gone else {"emails": 0}
    return {"forgotten": forgotten["emails"], "links_queued": queued}


def cmd(args) -> int:
    """`brain maint sharepoint-shells [--apply]`: a dry run unless --apply."""
    from src.store.schema import get_connection, run_migrations

    conn = get_connection(str(args.db))
    try:
        if args.apply:
            run_migrations(conn)
        found = classify(conn)
        print(f"sharepoint-shells {'APPLIED' if args.apply else 'DRY RUN (nothing written)'}:")
        for name, label in (
            ("held", "held by another document (forgotten)"),
            ("not_content", "linked to nothing readable (forgotten)"),
            ("orphan", "no link names it, or only another tenant (forgotten)"),
            ("refetch", "content not held (link queued; kept until it is)"),
        ):
            print(f"  {label:<54}: {len(found[name]):,}")
        if args.apply:
            stats = retire(conn, found)
            print(f"  {'documents forgotten':<54}: {stats['forgotten']:,}")
            print(f"  {'links queued for the retry pass':<54}: {stats['links_queued']:,}")
            if stats["links_queued"]:
                print("Run 'brain process-sharepoint', or leave them to the nightly pass.")
        else:
            print("Run it again with --apply to forget them and queue the links.")
    finally:
        conn.close()
    return 0
