# Odoo Work-Schedule Conflict Diagnostic — Design

Date: 2026-06-27

## Context

Declaring an employee absent syncs the absence to Odoo Time Off. Odoo rejects
the leave with *"The following employees are not supposed to work during that
period"* when the employee's Odoo work-schedule (`resource.calendar`) has no
working hours on that day — even when the plant schedule had them on. PR #7
made the sync best-effort so this no longer blocks the manager, but the
underlying calendar mismatches remain and those absences never reach Odoo Time
Off. This diagnostic finds every such employee in one pass so HR can correct
the calendars in Odoo.

## Goal

A read-only CLI script that lists active, non-reserve employees whose Odoo
work-schedule would cause an absence/leave to be rejected on a plant workday.

## Non-goals

- No writes/fixes to Odoo or Postgres; no test leaves are created.
- No modeling of public holidays, global leaves, or rotating two-week
  calendars — plain weekday coverage only, which is enough to surface the
  systematic mismatches.
- Not an in-app page; a CLI script run on Railway, matching the existing
  `scripts/diagnose_*.py` diagnostics.

## Run path (Postgres is optional)

The script must be runnable from a laptop via `railway run`, which injects
Odoo creds but **cannot reach the internal Railway Postgres**. So Odoo is the
required data source and Postgres is optional enrichment, degrading gracefully
(matching the existing `diagnose_*.py` scripts):

- **Population:** active employees from Odoo (`fetch_employees()`).
- **Plant work-week:** read from `schedule_store.current().work_weekdays` when
  Postgres is reachable; otherwise default Mon–Fri and print a NOTE.
- **Reserve filter:** when the local roster (`staffing.load_roster()`) is
  reachable, restrict to rostered people and drop reserves (joined by Odoo
  `employee_id`, **not by name** — display names differ from Odoo full names,
  e.g. "Gerardo Vergara" vs "Gerardo Vergara Quintero"). When Postgres isn't
  reachable, list all active Odoo employees and print a NOTE that reserves
  aren't filtered.

## Detection

- **Plant operating weekdays:** `schedule_store.current().work_weekdays`
  (fallback Mon–Fri = `{0,1,2,3,4}`; 0=Mon..6=Sun, Python `weekday()`).
- **Odoo reads (read-only):**
  - `fetch_employees()` → `{id: resource_calendar_id}`
  - `fetch_work_schedules()` → `{cal_id: (name, is_flexible)}`
  - `fetch_calendar_hours(cal_ids)` → `{cal_id: {weekday_str: [from, to]}}`
    (the weekdays a calendar has fixed hours on)
- **Classification** per person — pure function
  `classify_conflict(plant_weekdays, covered_weekdays, is_flexible, has_calendar)`:
  - `has_calendar` is False → `"no_calendar"`
  - `is_flexible` True, OR the calendar has no covered weekdays → `"flexible"`
  - `plant_weekdays - covered_weekdays` is non-empty → `"missing_days"`
  - otherwise → `"ok"`
- **Conflicts** = `{no_calendar, flexible, missing_days}`.

## Output

- Plain text to stdout. Summary line, e.g. *"3 of 28 active non-reserve
  employees have an Odoo work-schedule conflict (plant runs Mon–Fri)."*
- One section per issue type, each sorted by name:
  `Gerardo Vergara Quintero (id 1234) · calendar "Day Shift M-Th" · covers
  Mon–Thu · missing Fri`.
- `--all` flag also lists the employees who are fine. Exit code is always 0
  (diagnostic, never a failure signal).

## Run

`railway run python scripts/diagnose_odoo_calendar_conflicts.py` — read-only
but needs `ODOO_*` creds, so it runs on Railway like the other diagnostics.

## Testing

The classification is a pure function with no Odoo/DB dependency, unit-tested
in `tests/test_odoo_calendar_conflict_diagnostic.py`:
- covers every plant weekday → `ok`
- missing Friday → `missing_days`
- flexible flag → `flexible`
- calendar with no covered weekdays → `flexible`
- no calendar → `no_calendar`

Runs locally with `ZIRA_API_KEY=test .venv/bin/python -m pytest` (no Odoo
needed for the pure function).
