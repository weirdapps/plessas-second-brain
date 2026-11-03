"""A new email's thread updates, written as consolidation labels when it is loaded (issue 164).

The extraction of a new email is shown its thread's stored state (src/extract/thread_state.py)
and reports what the newest message changes about it: a decision, action or commitment
`superseded` by an item of the same email, or an action or commitment `done`. Each update is
a claim from third-party text that would hide an item from every default read, so it is
written only as the consolidation pass writes a model's label (src/store/consolidate.py):

- translated to that pass's labels (superseded_by an item of this email, completed_by this
  email) on handles this module builds from the store, never from the reply: a handle the
  state did not show, an item no longer current or of another thread, and a replacement
  number with no item behind it name nothing;
- run through `check_labels`, so every rule of the pass holds: the new email strictly after
  the item's own, its quote in the email (or the new item), only the owner closing an
  action, only someone already on the thread changing anything, only a decision made
  superseding a decision made, the owner's own item `contested_by` rather than hidden;
- applied not at all past `over_cap`, and logged in item_labels with method 'extraction',
  under the thread's key, so `brain maint consolidate --revert` puts a load back.
"""

import sqlite3
import sys
from collections import defaultdict
from dataclasses import replace

from src.store import consolidate

_CHANGES = {"done": "completed_by", "superseded": "superseded_by"}


def apply(conn: sqlite3.Connection, email_id: int, conversation_id: str, extraction: dict) -> int:
    """Write the checked thread updates of the email just stored as `email_id`; the labels
    written. The caller commits."""
    updates = extraction.get("thread_updates") or []
    ids = extraction.get("thread_state_ids") or {}
    if extraction.get("extraction_schema") != 2 or not updates or not ids:
        return 0
    if not consolidate.has_state(conn, "decisions"):
        return 0
    key = f"email:{conversation_id}"
    thread = consolidate.load_thread(conn, key)
    ref = f"email:{email_id}"
    message = thread.post(ref) if thread is not None else None
    if thread is None or message is None:
        return 0
    stored = {(item.table, item.id): item for item in thread.items}
    items: dict[str, consolidate.Item] = {}
    for handle, value in ids.items():
        if not (isinstance(value, list | tuple) and len(value) == 2):
            continue
        item = stored.get((str(value[0]), value[1]))
        if item is not None and item.current and item.message != ref:
            items[str(handle)] = item
    new: dict[str, list[consolidate.Item]] = defaultdict(list)
    for item in thread.items:
        if item.message == ref:
            new[item.table].append(item)
    for table, listed in new.items():
        for n, item in enumerate(listed, start=1):
            items[f"new:{consolidate._HANDLE[table]}{n}"] = item

    labels = []
    for update in updates:
        handle = str(update.get("item", "")).strip()
        label = _CHANGES.get(update.get("change"))
        if label is None or handle not in items or handle.startswith("new:"):
            continue
        if not update.get("evidence_found"):
            continue
        target = "M"
        if label == "superseded_by":
            target = f"new:{handle[:1]}{str(update.get('replaced_by', '')).strip()}"
        labels.append(
            {"item": handle, "label": label, "target": target, "evidence": update.get("evidence")}
        )
    if not labels:
        return 0
    prompt = consolidate.Prompt("", items, {"M": message})
    checked = [
        replace(label, method="extraction")
        for label in consolidate.check_labels({"labels": labels}, prompt, thread)
    ]
    if consolidate.over_cap(checked, prompt):
        print(
            f"  thread updates of email {email_id} not applied: {len(checked)} labels is too"
            " many at once",
            file=sys.stderr,
        )
        return 0
    run_id = consolidate.new_run_id()
    return sum(consolidate.write_label(conn, label, key, run_id) for label in checked)
