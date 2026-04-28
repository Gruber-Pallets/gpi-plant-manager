#!/usr/bin/env python3
"""One-time migration: rename people in schedules/ to match Odoo names.

Pulls Odoo employees, builds a best-effort mapping from current local
roster names to Odoo names (exact match -> fuzzy match), prints a preview,
and on confirmation:
  1. Backs up schedules/ to schedules.bak/
  2. Rewrites every JSON file under schedules/ using the mapping
  3. Replaces roster.json with the Odoo-derived version

Run from the project root (with venv active and ODOO_* env vars set):
  python -m scripts.migrate_schedule_names_to_odoo
"""

from __future__ import annotations

import json
import shutil
import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from zira_dashboard import odoo_sync  # noqa: E402

ROSTER_PATH = ROOT / "roster.json"
ROSTER_NEXT = ROOT / "roster.json.next"
SCHEDULES = ROOT / "schedules"
SCHEDULES_BAK = ROOT / "schedules.bak"


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _build_mapping(local_names: list[str], odoo_names: list[str]) -> dict[str, str]:
    """Return {local_name: odoo_name} via exact-then-fuzzy match. Locals
    with no plausible Odoo match are absent from the result."""
    odoo_lower = {n.lower().strip(): n for n in odoo_names}
    mapping: dict[str, str] = {}
    for local in local_names:
        key = local.lower().strip()
        if key in odoo_lower:
            mapping[local] = odoo_lower[key]
            continue
        # Fuzzy: find best match if similarity >= 0.7 OR shared 5-prefix
        best = max(
            ((n, _similarity(local, n)) for n in odoo_names),
            key=lambda p: p[1], default=(None, 0.0),
        )
        if best[0] and (best[1] >= 0.7 or local[:5].lower() == best[0][:5].lower()):
            mapping[local] = best[0]
    return mapping


def main() -> int:
    if SCHEDULES_BAK.exists():
        print(f"ERROR: {SCHEDULES_BAK} already exists. The migration has already been run.")
        print("       Remove it first if you really want to re-run (you'll lose the original backup).")
        return 1
    if not SCHEDULES.exists():
        print(f"ERROR: {SCHEDULES} does not exist. Nothing to migrate.")
        return 1

    print("Running fresh sync to roster.json.next ...")
    # Redirect sync's roster path temporarily to roster.json.next.
    odoo_sync.ROSTER_PATH = ROSTER_NEXT
    odoo_sync.LAST_SYNC_PATH = ROOT / ".odoo_last_sync_migrate"
    result = odoo_sync.sync(force=True)
    if not result.ok:
        print(f"ERROR: Odoo sync failed: {result.error}")
        return 1
    print(f"OK: pulled {result.employee_count} employees, {result.skill_column_count} skill columns.")

    odoo_roster = json.loads(ROSTER_NEXT.read_text())
    odoo_names = [r["name"] for r in odoo_roster]
    local_roster = []
    if ROSTER_PATH.exists():
        local_roster = json.loads(ROSTER_PATH.read_text())
    local_names = [r["name"] for r in local_roster]
    mapping = _build_mapping(local_names, odoo_names)

    matched = sorted(mapping.items())
    new_in_odoo = sorted(n for n in odoo_names if n not in mapping.values())
    missing_local = sorted(n for n in local_names if n not in mapping)

    print("\n=== Proposed mapping ===")
    for old, new in matched:
        print(f"  {old!r} -> {new!r}")
    print(f"\n=== New in Odoo ({len(new_in_odoo)}) ===")
    for n in new_in_odoo:
        print(f"  + {n}")
    print(f"\n=== Local-only (not in Odoo, will not appear in future schedules) ({len(missing_local)}) ===")
    for n in missing_local:
        print(f"  - {n}")

    print("\nProceed with migration? Type 'yes' to continue.")
    if input("> ").strip().lower() != "yes":
        print("Aborted.")
        ROSTER_NEXT.unlink(missing_ok=True)
        return 1

    print(f"Backing up {SCHEDULES} -> {SCHEDULES_BAK} ...")
    shutil.copytree(SCHEDULES, SCHEDULES_BAK)

    rewritten = 0
    for path in SCHEDULES.rglob("*.json"):
        content = path.read_text()
        new_content = content
        for old, new in mapping.items():
            # JSON-escaped name occurrences. Names appear as JSON strings.
            new_content = new_content.replace(f'"{old}"', f'"{new}"')
        if new_content != content:
            path.write_text(new_content)
            rewritten += 1

    print(f"Rewrote {rewritten} schedule files.")
    ROSTER_NEXT.replace(ROSTER_PATH)
    print(f"Replaced {ROSTER_PATH}.")
    print("\nMigration complete. Review schedules/ and commit when ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
