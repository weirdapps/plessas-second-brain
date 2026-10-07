#!/usr/bin/env python3
"""Return the images Stage 1 filed wrongly to 'unclassified', so the image pass describes them.

Which images and why: src/extract/image_reset.py. Dry run by default; --apply resets them.
Then `python -m src.cli process-images --limit <attachment rows> --workers 4` takes them,
one vision call per image. Refuses a replica.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DEFAULT_DB, is_replica, replica_refusal  # noqa: E402
from src.extract.image_classifier import MIN_SIGNATURE_OCCURRENCES  # noqa: E402
from src.extract.image_reset import apply_reset, find_misfiled  # noqa: E402
from src.store.schema import get_connection  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="reset them")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args(argv)
    if args.apply and is_replica():
        print(replica_refusal("resetting misfiled images"), file=sys.stderr)
        return 2

    conn = get_connection(args.db)
    try:
        plan = find_misfiled(conn)
        images = len(plan.frequency) + len(plan.decode_failed)
        print("Signatures filed by sender frequency before the floor, no sender qualifying now:")
        print(f"  to reset (a file on disk)          : {len(plan.frequency):,}")
        print(
            f"    of which seen under {MIN_SIGNATURE_OCCURRENCES} times in all : "
            f"{plan.seen_under_floor:,}"
        )
        print(f"  left (no file on disk)             : {plan.frequency_no_file:,}")
        print("Images filed noise/decode_failed:")
        print(f"  to reset (opens under the limit)   : {len(plan.decode_failed):,}")
        print(f"  left (still undecodable)           : {plan.undecodable:,}")
        print(f"  left (no file on disk)             : {plan.decode_failed_no_file:,}")
        print(f"Image attachment rows taken again    : {plan.attachment_rows:,}")
        print(f"Vision calls, at most one per image  : {images:,}")
        if not args.apply:
            print("DRY RUN (pass --apply to reset them)")
            return 0
        changed = apply_reset(conn, plan)
    finally:
        conn.close()
    print(f"Reset {changed:,} images to unclassified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
