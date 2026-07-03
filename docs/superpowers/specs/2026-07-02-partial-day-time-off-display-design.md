# Partial-day time off: correct shapes, times, and colors on every screen

**Date:** 2026-07-02
**Status:** Approved (autonomous session — user asked: "I want the different
screens to clearly show when people are working a partial day and also know
when the partial hours are (arrive at x, leave at x, gone from x to x)")

## Problem

The time-off calendar (`/staffing/time-off`), the Plant Scheduler
(`/staffing`), and the kiosk Who's Out calendar all *have* partial-day
rendering (amber vs blue/green styling, "arrives 9:00am" / "leaves 2:00pm" /
"gone 10:00am–12:00pm" labels, per-name off-window badges) — but the `shape`
column feeding them is wrong for most real leaves, so partials show as plain
full days with no times and no distinct color.

### Root causes (all in `time_off_sync._mirror_shape_and_hours`)

1. **Odoo half-day leaves mirror as `full_day`.** HR enters most partial
   days in Odoo as half days (`request_unit_half=True`). Those rows have
   `request_unit_hours=False`, so the current normalizer's
   `if not request_unit_hours → full_day` branch swallows them. No hours are
   stored → every screen shows a plain full day.
2. **Odoo hour-window leaves covering the whole shift mirror as
   `midday_gap`.** A full unpaid day entered with hour bounds (7:00–15:30)
   keeps its bounds and shows as a bogus "partial, gone 7:00am–3:30pm" on the
   scheduler and kiosk calendar — and `full_day_off_names()` does *not*
   exclude the person from the schedulable roster. (The staffing calendar
   route already papers over this with `is_full_day()`; the scheduler and
   kiosk don't.)
3. **Kiosk late-arrival / early-leave shapes get clobbered.** The kiosk
   stores precise shapes, but the 60s pull poller re-normalizes every synced
   row and overwrites `late_arrival`/`early_leave` with `midday_gap`, so
   "arrives 9:00am" degrades to a bare "7:00am–9:00am".

## Design

Fix the data at the sync layer so `shape` + `hour_from`/`hour_to` are
canonical in the mirror; the existing display code then works everywhere,
with three small wording/threshold touch-ups.

### 1. New pure classifier — `time_off_calendar.classify_off_window()`

```
classify_off_window(hour_from, hour_to, shift_from, shift_to)
  -> (shape, hour_from | None, hour_to | None)
```

- Window spans essentially the whole shift (delegates to the existing
  `is_full_day` span rule: `span >= shift_len - 0.5`) → `('full_day', None, None)`.
- Window anchored at shift start (`hour_from <= shift_from + 0.25`) →
  `('late_arrival', hf, ht)` — the person **arrives** at `hour_to`.
- Window anchored at shift end (`hour_to >= shift_to - 0.25`) →
  `('early_leave', hf, ht)` — the person **leaves** at `hour_from`.
- Interior window → `('midday_gap', hf, ht)` — **gone** `hour_from`–`hour_to`.

Lives in `time_off_calendar.py` (the pure, I/O-free calendar module) so the
sync layer, tests, and any future consumer share one definition.

### 2. Rewrite `time_off_sync._mirror_shape_and_hours()`

Resolution order per Odoo leave:

1. `number_of_days >= 1` → `full_day` (unchanged).
2. `request_unit_hours` with valid `request_hour_from/to` → that window.
3. **New:** otherwise derive the window from the `date_from`/`date_to` UTC
   datetimes (already fetched; Odoo computes them precisely for half-days
   from the employee's resource calendar). Convert to `SITE_TZ`; only trust
   the window when both ends land on the **same local calendar day**
   (multi-day windows → no window). This covers `request_unit_half` am/pm
   leaves with zero new Odoo fields — no API-version risk, no fetch change.
4. No usable window → `full_day`.
5. Any window from steps 2–3 is then normalized through
   `classify_off_window()` against the company schedule
   (`schedule_store.current()`, defensive fallback to `DEFAULT_SCHEDULE`).

Kiosk rows survive round-trips because the kiosk stores late arrivals as
`(shift_start, arrival)` and early leaves as `(leave, shift_end)` — exactly
the windows the classifier maps back to `late_arrival`/`early_leave`.

**Self-healing:** the first poller tick after deploy is a full pass over the
60-day-back/365-day-forward window; every changed shape/hour is UPDATEd, so
existing prod rows correct themselves within a minute of deploy.

### 3. Display touch-ups

- `time_off_calendar.label_for()`: midday gap label becomes
  `"gone 10:00am–12:00pm"` (was a bare range) — matches the scheduler wording
  and the user's ask; "arrives X" / "leaves X" / "full day" unchanged.
- `staffing.html`: `is_partial` becomes `e.hours is not none` (drops the
  hardcoded `< 8`, wrong for the 8.5h shift; with canonical shapes, full days
  always carry `hours=None`).
- `staffing_view.py` partial badge dicts + `scheduler_time_off` cleared-partial
  filter: same `< 8` → `is not None` (and `> 0`) cleanup.

### Explicitly unchanged

- `routes/time_off.py` `is_full_day` recompute stays as a safety net.
- Kiosk Who's Out template (blue/amber + label) and scheduler timing labels
  already render exactly what the user asked for once shapes are right.
- Pending-request visibility rules, privacy rule (leave *type* never shown),
  holiday styling, partial-clear workflow.

## Testing

TDD; all pure-function level plus template static checks:

- `test_time_off_calendar.py`: `classify_off_window` matrix (full / anchored
  start / anchored end / interior / near-full tolerance), `label_for` gap
  wording.
- `test_time_off_sync.py`: `_mirror_shape_and_hours` matrix — day-unit full
  day; hour-unit partial anchored at start → `late_arrival`; anchored at end
  → `early_leave`; interior → `midday_gap`; full-shift hour window →
  `full_day`; half-day am/pm via UTC datetimes → `late_arrival`/`early_leave`
  with correct local hours; multi-day datetimes → `full_day`; Odoo `False`
  fields → `full_day`.
- `test_scheduler_time_off.py` / `test_staffing_static.py` /
  `test_staffing_time_off_pills.py`: updated for the `< 8` cleanup.

## Rollout

Single deploy; no schema change; no new Odoo fields; poller self-heals
existing rows on the first full pass after boot.

## Addendum (2026-07-03): field reliability, measured against prod Odoo

Live inspection of all 15 leaves overlapping 07-01..07-17 (read-only replay
against the prod DB + Odoo) corrected two assumptions:

1. **`number_of_days` is unreliable and must not veto an hour window.** This
   Odoo instance reported `number_of_days=1.0` for a 45-minute hour-unit
   leave (leave 99 — the partial that stayed invisible) and `3.0` for a
   different one-day leave. The original "rule 1: `days >= 1` → full_day"
   ate every genuine partial, because HR enters partials as hour-unit
   leaves. Fixed order: a valid `request_hour_from/to` window is classified
   FIRST (the classifier's span rule still maps whole-shift windows to
   `full_day`); `number_of_days >= 1` only decides when no usable hour
   window exists.
2. **`date_from`/`date_to` timezones are inconsistent per employee.** Some
   employees' windows convert correctly from UTC (13:00 → 8:00am CDT), others
   come back already in wall-clock local time — matching the known broken
   per-employee Odoo Working Schedules (see calendar-conflict monitor). The
   datetime-derived window therefore stays the LAST resort (half-days only,
   which carry no hour fields); `request_hour_from/to` are wall-clock local
   and trustworthy.

Replay of the fixed order over the same 15 leaves: exactly one row changes
(leave 99 → `midday_gap` 13.5–14.25, "gone 1:30pm–2:15pm"); all 14 genuine
full days stay `full_day`.
