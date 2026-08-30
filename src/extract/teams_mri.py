"""Step 4: resolve Teams MRIs to {email, displayName, person_id}.

MRIs (e.g. '8:orgid:<aad-oid>') are opaque IDs that need a Graph
/users/{aad-oid} lookup to map to a real email. The resolution is cached
in teams_mri_resolution; once cached, never re-queried (unless prior status
was retryable). When the resolved email matches a row in `people`, all
messages from that MRI get sender_person_id back-linked.
"""

import json
import sqlite3
from datetime import UTC, datetime

from src.export.teams_cli import TeamsCliAuthRequired, TeamsCliError, run_teams_cli


def resolve_mris(conn: sqlite3.Connection, max_per_run: int = 50) -> dict:
    """Resolve up to max_per_run unseen MRIs.

    Returns:
        {"resolved": <int>, "permanent_fail": <int>, "retryable_fail": <int>}
    """
    distinct = conn.execute(
        """
        SELECT DISTINCT m.sender_mri, m.sender_display_name, r.last_attempt_at AS prev_attempt
        FROM teams_messages m
        LEFT JOIN teams_mri_resolution r ON r.mri = m.sender_mri
        WHERE m.sender_mri IS NOT NULL
          AND (r.mri IS NULL OR r.status IN ('pending','failed'))
        -- Never-attempted MRIs first; among already-attempted, oldest-attempted first.
        ORDER BY (r.last_attempt_at IS NOT NULL), r.last_attempt_at
        LIMIT ?
        """,
        (max_per_run,),
    ).fetchall()

    resolved = 0
    perm_fail = 0
    retry_fail = 0

    for row in distinct:
        mri = row["sender_mri"]
        display = row["sender_display_name"]

        # Only 8:orgid:<aad-oid> MRIs map to a person via Graph /users/{oid}.
        # Channel/thread IDs (19:...@thread.v2) and bot/app MRIs (28:, 48:) are
        # not users — resolve-mri rejects them ("Invalid MRI"), so without this
        # guard they'd be re-attempted as retryable 'failed' on every sync
        # forever (observed: ~1,050 such rows). Mark them permanent_fail once and
        # skip the Graph call.
        if not mri.startswith("8:orgid:"):
            now = datetime.now(UTC).isoformat()
            conn.execute(
                """
                INSERT INTO teams_mri_resolution(mri, status, last_attempt_at)
                VALUES (?, 'permanent_fail', ?)
                ON CONFLICT(mri) DO UPDATE SET
                    status = 'permanent_fail',
                    last_attempt_at = excluded.last_attempt_at
                """,
                (mri, now),
            )
            perm_fail += 1
            continue

        try:
            payload = run_teams_cli(["resolve-mri", mri])
            email = payload.get("email")
            display_name = payload.get("displayName") or display

            person_id = None
            if email:
                pr = conn.execute(
                    "SELECT id FROM people WHERE LOWER(email) = LOWER(?)", (email,)
                ).fetchone()
                if pr:
                    person_id = pr["id"]

            now = datetime.now(UTC).isoformat()
            conn.execute(
                """
                INSERT INTO teams_mri_resolution(
                    mri, email, display_name, person_id, status,
                    resolved_at, last_attempt_at
                ) VALUES (?, ?, ?, ?, 'resolved', ?, ?)
                ON CONFLICT(mri) DO UPDATE SET
                    email = excluded.email,
                    display_name = excluded.display_name,
                    person_id = excluded.person_id,
                    status = 'resolved',
                    resolved_at = excluded.resolved_at,
                    last_attempt_at = excluded.last_attempt_at
                """,
                (mri, email, display_name, person_id, now, now),
            )
            if person_id is not None:
                conn.execute(
                    "UPDATE teams_messages SET sender_person_id = ? WHERE sender_mri = ?",
                    (person_id, mri),
                )
            resolved += 1
        except TeamsCliAuthRequired:
            # No point trying more this run; bubble up so the orchestrator
            # can flip the auth-watch sentinel.
            raise
        except TeamsCliError as e:
            is_404 = _is_404(e.stderr)
            status = "permanent_fail" if is_404 else "failed"
            now = datetime.now(UTC).isoformat()
            conn.execute(
                """
                INSERT INTO teams_mri_resolution(mri, status, last_attempt_at)
                VALUES (?, ?, ?)
                ON CONFLICT(mri) DO UPDATE SET
                    status = excluded.status,
                    last_attempt_at = excluded.last_attempt_at
                """,
                (mri, status, now),
            )
            if is_404:
                perm_fail += 1
            else:
                retry_fail += 1

    # Back-link every message whose sender is already in the cache, not just the
    # ones the loop above happened to resolve this run. The per-MRI UPDATE inside
    # the loop only saw the messages that existed at resolution time, and the
    # candidate query deliberately skips MRIs already at status='resolved', so
    # that MRI is never revisited; teams_export doesn't set the column at INSERT
    # time either. Every message arriving after its sender was resolved therefore
    # stayed NULL forever (measured: 10,210 rows, 42% of attributable traffic).
    # Set-based and idempotent, and OUTSIDE the loop on purpose: the common case
    # is a warm cache with nothing new to resolve, which is exactly when the
    # arrears pile up.
    #
    # The EXISTS on people(id) is not decoration. sender_person_id REFERENCES
    # people(id), and a teams_mri_resolution.person_id can outlive the people row
    # that dedup merged away, so without it this statement plants a dangling
    # foreign key. It guards both halves: the value written and the rows chosen.
    conn.execute(
        """
        UPDATE teams_messages
           SET sender_person_id = (
                 SELECT r.person_id
                   FROM teams_mri_resolution r
                  WHERE r.mri = teams_messages.sender_mri
                    AND r.status = 'resolved'
                    AND r.person_id IS NOT NULL
                    AND EXISTS (SELECT 1 FROM people p WHERE p.id = r.person_id)
               )
         WHERE sender_person_id IS NULL
           AND sender_mri IN (
                 SELECT r.mri
                   FROM teams_mri_resolution r
                  WHERE r.status = 'resolved'
                    AND r.person_id IS NOT NULL
                    AND EXISTS (SELECT 1 FROM people p WHERE p.id = r.person_id)
               )
        """
    )

    conn.commit()
    return {
        "resolved": resolved,
        "permanent_fail": perm_fail,
        "retryable_fail": retry_fail,
    }


def _is_404(stderr: str) -> bool:
    """teams-cli serialises {status: 404} into stderr JSON on Graph 404."""
    try:
        return json.loads(stderr).get("status") == 404
    except (json.JSONDecodeError, ValueError):
        return "404" in stderr
