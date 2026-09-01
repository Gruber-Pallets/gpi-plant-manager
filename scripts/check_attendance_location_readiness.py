#!/usr/bin/env python3
"""Print local Odoo attendance-location readiness as JSON."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from zira_dashboard import attendance_readiness, db  # noqa: E402


def main() -> int:
    report = attendance_readiness.build_report(datetime.now(UTC))
    print(attendance_readiness.report_json(report))
    db.shutdown_pool()
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
