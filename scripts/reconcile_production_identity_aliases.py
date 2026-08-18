#!/usr/bin/env python3
"""Review and apply the fixed production-ID reconciliation allow-list.

Without ``--apply`` this command only reports the aliases currently supported
by the evidence.  ``--apply`` creates the alias table if needed, re-discovers
the allow-list through the reconciliation service, and writes only its safe
candidates.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from zira_dashboard import db  # noqa: E402
from zira_dashboard import production_identity_aliases as identity_aliases  # noqa: E402


def _day_evidence(days: tuple[date, ...]) -> str:
    if not days:
        return "no dated production evidence"
    if len(days) == 1:
        return f"production day: {days[0].isoformat()}"
    return (
        f"production days: {days[0].isoformat()} to {days[-1].isoformat()} "
        f"({len(days)} days)"
    )


def _print_result(result: identity_aliases.ReconciliationResult) -> None:
    print("Approved aliases:")
    if not result.candidates:
        print("  None")
    for candidate in result.candidates:
        print(
            f"  {candidate.confirmed_name}: {candidate.legacy_emp_id} -> "
            f"{candidate.canonical_emp_id} ({_day_evidence(candidate.production_days)})"
        )

    print("Skipped approved names:")
    if not result.skipped:
        print("  None")
    for skipped in result.skipped:
        canonical_id = skipped.canonical_emp_id or "none"
        observed_ids = ", ".join(repr(emp_id) for emp_id in skipped.observed_emp_ids) or "none"
        print(
            f"  {skipped.name}: SKIPPED ({skipped.reason}; "
            f"canonical ID: {canonical_id}; observed production IDs: {observed_ids}; "
            f"{_day_evidence(skipped.production_days)})"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Review or apply fixed production identity aliases."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create the alias table if needed and persist safe aliases",
    )
    args = parser.parse_args(argv)

    db.init_pool()
    if not args.apply:
        result = identity_aliases.find_confirmed_aliases()
        _print_result(result)
        print("\nDry run only. Re-run with --apply to write the approved aliases.")
        if result.skipped:
            print("One or more approved names were skipped; resolve them before applying.")
            return 1
        return 0

    db.bootstrap_schema()
    result = identity_aliases.apply_confirmed_aliases()
    _print_result(result)
    print(f"\nWritten aliases: {len(result.candidates)}")
    if result.skipped:
        print("Apply incomplete: one or more approved names were skipped and were not written.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
