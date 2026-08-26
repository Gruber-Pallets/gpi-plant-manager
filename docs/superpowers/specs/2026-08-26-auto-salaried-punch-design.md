# Auto Salaried Punch — Design

**Date:** 2026-08-26
**Status:** Approved design, pending implementation plan

## Problem

Some employees were promoted to salary (Odoo `hr.employee.wage_type = 'monthly'`,
"Fixed Wage"). Their pay no longer depends on punches, but we still want their
hours in Odoo Attendance so downstream apps can transfer time (e.g., to
Maintenance) and we can report sustaining hours. Salaried employees are blocked
from the kiosk today, so nothing punches them at all.

## Solution overview

A background robot ("auto salaried punch"), modeled on the existing
`auto_lunch.py` machinery, writes four punches per weekday for every active
fixed-wage employee, mimicking an hourly employee's attendance shape (two
blocks per day, lunch gap unpaid, short breaks not punched):

| Plant time (America/Chicago) | Action |
|---|---|
| 6:00 AM | Clock in — department **Sustaining**, no work center |
| 11:00 AM | Clock out (lunch) — close the open Odoo record, remember its department |
| 11:30 AM | Clock in — same department the employee was in before lunch |
| 3:30 PM | Clock out — close whatever record is open, regardless of who opened it |

Punches are written live at their scheduled times so other apps always find an
open `hr.attendance` record to transfer against during the day. Punch times are
exact (no rounding): `rounded_at = occurred_at`, same as auto-lunch.

Transfers themselves are explicitly **out of scope** — other apps handle them.

## Decisions (all confirmed with Luke)

1. **Scope:** ALL active fixed-wage employees. No include/exclude list. New
   salaried employees are picked up automatically via the existing roster sync.
2. **Breaks:** Mimic hourly employees exactly — only the lunch gap is punched.
   Morning break, afternoon break, and cleanup are not punched.
3. **Days:** Monday–Friday only. Weekends and company holidays are skipped
   entirely. Saturday work by salaried employees is recorded by hand in Odoo.
4. **Leave skip rule:** At punch time, skip the whole day if the employee has
   **approved** leave (PTO, bereavement, any `hr.leave`) overlapping that day.
   Half-day leave skips the whole day (v1 simplification; punch-around is a
   possible future upgrade). Pending/unapproved requests do NOT block.
5. **Reconciliation (sick-day rule):** If approved leave appears for a day the
   robot already punched, a cleanup watcher deletes the robot's **own** punches
   for that day — never human punches, and never on a day where a transfer
   occurred (real work happened; flag instead). Anything the robot can't safely
   fix lands on a "needs a human" flag list.
6. **Lunch return department:** The 11:30 clock-in returns to the department of
   the record closed at 11:00, read from **Odoo** (not the local log), because
   salaried transfers come from outside apps that may bypass this app's punch
   log. Odoo is the referee. (Hourly auto-lunch already preserves work center
   via the local log — unchanged.)
7. **Kiosk:** Salaried employees remain blocked from manual kiosk punching.
8. **Times:** 6:00–15:30 is intentional (9-hour salaried day; plant hourly
   shift is 7:00–15:30). Punch times live as named constants in one place.

## Architecture

### New module: `src/zira_dashboard/auto_salaried.py`

Follows the `auto_lunch.py` split:

- **Pure decision core** — given plant-local now, the roster of fixed-wage
  people, per-person day state, holiday/leave lookups: return the list of due
  punch actions. No I/O; heavily unit-tested.
- **I/O wrapper `run_tick()`** — driven by a new 60-second entry in the
  `_WARMERS` registry in `app.py`. Performs skip checks, writes punches, and
  updates state.

### State table: `auto_salaried_runs`

One row per person per day (added via `_schema.py` bootstrap, following
`auto_lunch_runs`): which of the four punches are done, their punch-log row
ids, and the remembered pre-lunch department. Each punch insert and its state
update commit in **one transaction** — a crash can never leave the scoreboard
disagreeing with the log.

Duplicate protection: each of the four punch slots can be written at most once
per person per day, enforced by the scoreboard row inside the transaction.
Restarts and overlapping ticks cannot double-punch.

### Punch pipeline (reused, not rebuilt)

The robot inserts rows into `timeclock_punches_log` with
`source='auto_salaried'` and lets the existing `timeclock_sync` machinery ship
them to Odoo `hr.attendance` (`in_mode='kiosk'`, `overtime_status='approved'`,
department field per `ODOO_KIOSK_DEPARTMENT_FIELD`). Failed syncs retry every
60s via the existing `retry_unsynced_punches` warmer. The `source` tag is how
the cleanup watcher identifies punches it owns.

### Skip checks (all local, no Odoo calls at punch time)

- Weekday check: plant-local date via `plant_day` / `shift_config.SITE_TZ`.
- Holiday check: `company_holidays` local mirror.
- Approved-leave check: local `time_off_requests` mirror (kept current by the
  existing time-off poll warmer).

### Odoo-direct reads (only two)

- At 11:00, after closing the open record, read its department off that record
  (respects outside-app transfers).
- Resolve "Sustaining" → Odoo `hr.department` id via the existing
  case-insensitive name matcher (`_department_id_for_wc`-style `ilike` match,
  cached), so "Sustaining" matches e.g. "05 Sustaining".

### Cleanup watcher

A second, slower warmer (~600s). Looks back over the last N robot-punched days
(N ≈ 7):

- Approved leave now overlaps a punched day AND the day is clean (only
  `auto_salaried` punches, no transfers, no human punches) → delete the robot's
  Odoo attendance records (unlink) and mark the log rows.
- Day is messy → insert a flag row.

Flags: a small table (who, day, reason) surfaced on a plain admin list page.

### Catch-up after downtime

The decision core computes due-but-unwritten punches from the schedule vs. the
scoreboard, so a tick after restart writes missed punches **backdated to their
scheduled times**. If an entire lunch pair was straddled inconsistently, the
day is flagged rather than guessed.

## Failure handling

| Failure | Behavior |
|---|---|
| Odoo down at punch time | Local log row persists; existing retry loop re-sends until it lands. |
| App down over a punch time | Next tick writes the missed punch, backdated (same plant day only; downtime crossing midnight leaves the day incomplete → flagged by the reconciler). |
| App down over a whole inconsistent stretch | Flag the day for a human. |
| Can't read department off the closed lunch record | Punch back in at 11:30 anyway, default Sustaining, flag the day. |
| "Sustaining" department not found in Odoo | Loud warning + flag; punches proceed department-less (matches kiosk behavior with the field unset). |
| Leave appears after punches AND a transfer happened | Never auto-deleted — flagged. |
| Wage type changes mid-day | Enrollment is evaluated at the 6:00 punch only; changes take effect next morning. |

## Configuration

- `ODOO_KIOSK_DEPARTMENT_FIELD` must be set in the deployed environment
  (verify on Railway — required for departments to be written at all).
- A "Sustaining" `hr.department` must exist in Odoo (probe once credentials
  are available; not yet verified — no local `.env` at design time).
- `AUTO_SALARIED_DRY_RUN=1` — ships enabled first: the robot logs every action
  it would take without writing punches. After a few days of verified logs,
  flip it live.
- Punch times (6:00 / 11:00 / 11:30 / 15:30) as named module constants.

## Testing

- **Decision core units:** every skip rule (weekend, holiday, approved leave;
  pending leave does NOT skip), four-punch sequencing, already-done guards,
  catch-up scenarios, DST transition days (punch times stay 6:00 *local*).
- **Cleanup watcher units:** clean day deletes; transfer day flags; human-punch
  day flags; pending leave does nothing.
- **Fake-Odoo tests:** inject a fake `execute` callable (existing test seam)
  and assert exact writes/deletes.
- **Dry run in production** before going live.

## Out of scope

- Transfers between departments/work centers (other apps own this).
- Punching around half-day leave (possible v2).
- Saturday auto-punching.
- Any change to hourly employees' auto-lunch behavior.
