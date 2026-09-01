#!/usr/bin/env python3
"""Print the current Odoo attendance-location readiness report as JSON."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from zira_dashboard import attendance_readiness, db  # noqa: E402


def _utc_now() -> datetime:
    return datetime.now(UTC)


def main() -> int:
    """Read and print readiness without bootstrapping or changing source data."""
    load_dotenv(dotenv_path=ROOT / ".env", override=False)
    db.init_pool()
    report = attendance_readiness.build_report(_utc_now())
    print(attendance_readiness.report_json(report))
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
