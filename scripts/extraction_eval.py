#!/usr/bin/env python3
"""Extraction eval: the owner's own items, extracted through the production path and graded.

A frozen set of real items, labelled by the owner, puts a number on every prompt, effort
or model change before it ships; src/evals/extraction.py says how it is built and graded.
Only --run and --calibrate call the model, and only with --yes: without it they print what
the calls would cost and stop.

    python scripts/extraction_eval.py --build                # draft the set from the store, read-only
    python scripts/extraction_eval.py --dry-run [--judge]    # validate, test the grader, regrade, estimate
    python scripts/extraction_eval.py --run --judge --yes    # extract, grade, record
    python scripts/extraction_eval.py --calibrate --yes      # how far the judge agrees with the owner

The set and its results live in ${BRAIN_DATA_DIR:-~/.second-brain}/evals/extraction/, never
in the repository. README.md, section Evals, says how to label the set and read the results.
"""

import argparse
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import DEFAULT_DB  # noqa: E402
from src.evals import connect_read_only, file_sha  # noqa: E402
from src.evals import extraction as ev  # noqa: E402

EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _pct(value) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _build(args, files: ev.Files) -> int:
    if files.cases.exists() and not args.force:
        print(
            f"{files.cases} exists and may hold your labels; pass --force to replace it.",
            file=sys.stderr,
        )
        return 2
    try:
        conn = connect_read_only(args.db)
    except sqlite3.OperationalError as e:
        print(f"cannot open {args.db} read-only: {e}", file=sys.stderr)
        return 2
    pattern = os.environ.get("BRAIN_USER_EMAIL_PATTERN", "")
    try:
        cases, notes = ev.build_cases(conn, days=args.days, seed=args.seed, owner_pattern=pattern)
    finally:
        conn.close()
    ev.write_new_set(files, cases, owner_name=os.environ.get("BRAIN_USER_NAME", ""))
    strata = Counter(case["stratum"] for case in cases)
    print(f"{len(cases)} cases written to {files.cases}:")
    for stratum, count in strata.items():
        print(f"  {stratum:<20} {count}")
    for note in notes:
        print(f"  short: {note}")
    if not pattern:
        print(
            "  BRAIN_USER_EMAIL_PATTERN is unset: sent mail is the Sent mailboxes only, and an"
            " inbound ask is any mail with an action item."
        )
    print(
        f"Read the cases in {files.cases_md}, label each one's gold in {files.cases}, and list"
        f" the names you go by in {files.owner}."
    )
    return 0


def _load(files: ev.Files) -> tuple[list[dict] | None, int]:
    if not files.cases.exists():
        print(f"no set at {files.cases}: run --build first", file=sys.stderr)
        return None, 2
    try:
        return ev.load_cases(files.cases), 0
    except ValueError as e:
        print(e, file=sys.stderr)
        return None, 1


def _valid(cases: list[dict]) -> bool:
    problems = ev.validate_cases(cases)
    for problem in problems:
        print(f"  problem: {problem}")
    return not problems


def _describe(cases: list[dict]) -> None:
    labelled = sum(ev.is_labelled(c) for c in cases)
    strata = Counter(str(c.get("stratum")) for c in cases)
    print(
        f"{len(cases)} cases, {labelled} labelled: "
        + ", ".join(f"{name} {count}" for name, count in strata.items())
    )


def _print_estimate(estimate: dict, cases: int, reps: int) -> None:
    total = estimate["cost_usd"] + estimate["judge_cost_usd"]
    print(
        f"Estimate: {estimate['calls']} extraction calls ({cases} cases x {reps} reps), about"
        f" {estimate['input_tokens']:,} input and {estimate['output_tokens']:,} output tokens"
        f" at {estimate['model']} rates ({estimate['chars_per_token']} characters a token):"
        f" ${estimate['cost_usd']:.2f}"
    )
    if estimate["judge_calls"]:
        print(
            f"  plus at most {estimate['judge_calls']} judge calls: ${estimate['judge_cost_usd']:.2f};"
            f" ${total:.2f} in all"
        )


def _print_scores(scores: dict) -> None:
    print(
        f"pass rate {_pct(scores['pass_rate'])} of applicable checks"
        f" (about +/-{scores['noise_points']} points at this size);"
        f" every check passed on {_pct(scores['case_pass_rate'])} of rows"
    )
    for name, check in scores["checks"].items():
        if check["applicable"]:
            print(
                f"  {name:<18} {_pct(check['rate']):>7}  ({check['passed']}/{check['applicable']})"
            )
    for stratum, rate in scores["by_stratum"].items():
        print(f"  stratum {stratum:<18} {_pct(rate):>7}")


def _dry_run(args, files: ev.Files, cases: list[dict]) -> int:
    _describe(cases)
    if not _valid(cases):
        return 1
    aliases = ev.load_owner_aliases(files)
    test = ev.self_test(cases, aliases)
    print(
        f"grader self-test on {test['cases']} labelled cases: the gold itself"
        f" {_pct(test['oracle'])}, no answer {_pct(test['no_answer'])}, an empty extraction"
        f" {_pct(test['empty_answer'])}"
    )
    for failure in test["oracle_failures"]:
        print(f"  the gold fails its own check: {failure}")
    regraded = ev.regrade(files, cases, aliases)
    if regraded and regraded["rows"]:
        print(f"run {regraded['run_id']}, regraded against the gold as it stands:")
        _print_scores(regraded)
    _print_estimate(ev.estimate(cases, reps=args.reps, judge=args.judge), len(cases), args.reps)
    return 1 if test["oracle_failures"] else 0


def _selected(cases: list[dict], wanted: list[str]) -> list[dict] | None:
    if not wanted:
        return cases
    unknown = sorted(set(wanted) - {c["id"] for c in cases})
    if unknown:
        print(f"no case with id {', '.join(unknown)}", file=sys.stderr)
        return None
    return [c for c in cases if c["id"] in wanted]


def _run(args, files: ev.Files, cases: list[dict]) -> int:
    if not _valid(cases):
        return 1
    selected = _selected(cases, args.case)
    if selected is None:
        return 2
    _describe(selected)
    _print_estimate(
        ev.estimate(selected, reps=args.reps, judge=args.judge), len(selected), args.reps
    )
    if not args.yes:
        print("Nothing was called. Add --yes to run it.")
        return 2
    record = ev.run(
        files,
        selected,
        reps=args.reps,
        workers=args.workers,
        effort=args.effort,
        judge=args.judge,
        set_sha=file_sha(files.cases),
    )
    rows = record["rows"]
    print(
        f"run {record['run_id']}: {record['model']}, prompt {record['prompt_sha']}, effort"
        f" {record['effort']}; {rows['scored']} graded, {rows['truncated']} truncated,"
        f" {rows['refused']} refused, {rows['errors']} errors"
    )
    _print_scores(record)
    if record["judge"]:
        judge = record["judge"]
        print(
            f"judge over {judge['judged']} rows: decisions F1 {_pct(judge['decisions_f1'])},"
            f" actions F1 {_pct(judge['actions_f1'])}"
        )
    print(f"cost ${record['cost_usd']:.2f} extraction, ${record['judge_cost_usd']:.2f} judge")
    print(f"recorded in {files.results}; rows in {files.runs / (record['run_id'] + '.jsonl')}")
    if rows["errors"]:
        print(f"errors in {files.errors}")
    return 0


def _calibrate(args, files: ev.Files, cases: list[dict]) -> int:
    if not _valid(cases):
        return 1
    if not files.calibration.exists():
        written = ev.write_calibration_template(files, cases)
        if written:
            print(
                f"Wrote {files.calibration}: {written} outputs of the latest run. For each, fill"
                " decisions_matched and actions_matched with how many items you would pair."
            )
    by_id = {c["id"]: c for c in cases}
    judged = [p["case"] for p in ev.calibration_probes(cases)]
    judged += [by_id[line["case_id"]] for line in ev.owner_counted_lines(files, cases)]
    if not judged:
        print("Nothing to calibrate on: label cases with decisions or actions first.")
        return 2
    calls, cost = ev.judge_estimate(judged)
    print(f"Estimate: at most {calls} judge calls, ${cost:.2f}")
    if not args.yes:
        print("Nothing was called. Add --yes to run it.")
        return 2
    record = ev.calibrate(files, cases, workers=args.workers)
    owner = record["owner"]
    print(
        f"judge agreement {_pct(record['agreement'])}: probes"
        f" {record['probes']['agreed']}/{record['probes']['total']}"
        + (
            f", your counts {owner['agreed']}/{owner['total']}"
            if owner
            else ", no counts of yours yet"
        )
    )
    if record["calibrated"]:
        print("calibrated: the judge's scores can steer changes")
        return 0
    print(f"NOT calibrated: below {record['target']:.0%}; fix the judge prompt before trusting it")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--build", action="store_true", help="draft the set from the store, read-only"
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the set, test the grader, regrade the last run and estimate; no model call",
    )
    mode.add_argument("--run", action="store_true", help="extract and grade the set (needs --yes)")
    mode.add_argument(
        "--calibrate", action="store_true", help="check the judge against your labels (needs --yes)"
    )
    parser.add_argument(
        "--judge", action="store_true", help="also match decision and action sets with the judge"
    )
    parser.add_argument(
        "--yes", action="store_true", help="let --run or --calibrate call the model"
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="the eval directory (default ${BRAIN_DATA_DIR:-~/.second-brain}/evals/extraction)",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="the store --build reads")
    parser.add_argument("--days", type=int, default=180, help="--build draws from these last days")
    parser.add_argument("--seed", type=int, default=7, help="--build's sample seed")
    parser.add_argument("--force", action="store_true", help="let --build replace a set")
    parser.add_argument("--reps", type=int, default=2, help="extractions per case (default 2)")
    parser.add_argument("--workers", type=int, default=4, help="calls in flight (default 4)")
    parser.add_argument(
        "--effort",
        choices=EFFORTS,
        help="send output_config.effort with every extraction: a sweep, since production sets none",
    )
    parser.add_argument(
        "--case", action="append", default=[], metavar="ID", help="run only this case (repeatable)"
    )
    args = parser.parse_args(argv)

    files = ev.Files(args.dir or ev.default_root())
    if args.build:
        return _build(args, files)
    cases, code = _load(files)
    if cases is None:
        return code
    if args.dry_run:
        return _dry_run(args, files, cases)
    if args.run:
        return _run(args, files, cases)
    return _calibrate(args, files, cases)


if __name__ == "__main__":
    sys.exit(main())
