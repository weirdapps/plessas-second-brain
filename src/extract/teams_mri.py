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
