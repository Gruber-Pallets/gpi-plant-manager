"""Auto salaried punch worker: writes four hr.attendance punches per weekday
for every active fixed-wage (wage_type='monthly') employee, mimicking an
hourly employee's two-block day. Skips weekends, company holidays, and days
with approved leave; a reconciler cleans up when leave is approved after
punches exist.

The decision logic (due_slots / skip_reason / scheduled_at) is pure and
unit-testable. run_tick() / run_reconcile() wire the I/O around it.

See docs/superpowers/specs/2026-08-26-auto-salaried-punch-design.md.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timedelta

from . import company_holidays, db, shift_config, timeclock_sync

_log = logging.getLogger(__name__)

SUSTAINING_WC = "Sustaining"

PUNCH_TIMES: dict[str, time] = {
    "morning_in": time(6, 0),
    "lunch_out": time(11, 0),
    "lunch_in": time(11, 30),
    "day_out": time(15, 30),
}
SLOT_ORDER: tuple[str, ...] = ("morning_in", "lunch_out", "lunch_in", "day_out")
SLOT_ACTION: dict[str, str] = {
    "morning_in": "clock_in",
    "lunch_out": "clock_out",
    "lunch_in": "clock_in",
    "day_out": "clock_out",
}

# Dry-run mode marks a slot done with this sentinel instead of a real
# timeclock_punches_log id, so simulation advances the scoreboard (no log
# spam every 60s) without writing punches.
SIMULATED_PUNCH_ID = 0

RECONCILE_LOOKBACK_DAYS = 7


def mode() -> str:
    """'dry_run' | 'live' | 'off'. Dry-run wins so a deploy with both vars
    set can never write real punches by accident."""
    if os.environ.get("AUTO_SALARIED_DRY_RUN") == "1":
        return "dry_run"
    if os.environ.get("AUTO_SALARIED_ENABLED") == "1":
        return "live"
    return "off"


def scheduled_at(day: date, slot: str) -> datetime:
    return datetime.combine(day, PUNCH_TIMES[slot], tzinfo=shift_config.SITE_TZ)


def skip_reason(day: date, *, is_company_holiday: bool,
                has_approved_leave: bool) -> str | None:
    if day.weekday() >= 5:
        return "weekend"
    if is_company_holiday:
        return "holiday"
    if has_approved_leave:
        return "approved_leave"
    return None


def due_slots(now: datetime, day: date, run: dict | None) -> list[str]:
    """Slots whose scheduled time has arrived and that haven't punched yet,
    in punch order. Catch-up after downtime falls out naturally: every
    overdue slot is returned at once, each backdated to its scheduled time
    by the caller."""
    done = run or {}
    return [
        s for s in SLOT_ORDER
        if done.get(f"{s}_punch_id") is None and now >= scheduled_at(day, s)
    ]
