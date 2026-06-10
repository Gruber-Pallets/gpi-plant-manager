# Missed Punch-Out — Design

**Date:** 2026-06-09
**Status:** Approved (brainstorming → implementation planning)

## Context

When an hourly employee clocks in at the kiosk but forgets to clock out, their
`hr.attendance` record stays **open** (no `check_out`) indefinitely. Until they
(or someone) closes it, the record accrues hours forever and corrupts every
report that reads the interval — production attribution, hours, leaderboards.
This actually happened: **Jesus Moreno** clocked in, never clocked out, and has
been showing as open across multiple days.

There is no automatic backstop. Auto-lunch
([2026-06-02-auto-lunch-timeclock-design.md](2026-06-02-auto-lunch-timeclock-design.md))
handles scheduled mid-day gaps, but nothing handles "still clocked in when the
day is over."

Dale wants two things:
1. **A backstop:** anyone still punched in at the end of the day gets punched out
   automatically at **midnight** (the end of that actual day).
2. **A correction path:** an alert on **every screen** — modeled on the existing
   Late/Absence and Missing-Work-Center reports — that says **"Missed Punch
   Out"** and lets a manager type the time the employee *actually* left. Entering
   the time rewrites that attendance record so it ends at the entered time
   instead of midnight.

### The pattern to mirror (Late / Missing-Work-Center alerts)

Neither is a page banner — each is a **nav badge injected by the shared
[`footer.js`](../../../src/zira_dashboard/static/footer.js) on every page** that
polls a cheap JSON endpoint; clicking it opens a **modal** with actionable rows.
Pure logic lives in a small module (`late_report.py`, `missing_wc.py`); a route
returns the structured rows; `footer.js` renders the badge + modal. Because
`footer.js` loads on every page, the badge appears app-wide — including the
timeclock and the dashboards. Routes are their own module
([`routes/missing_wc.py`](../../../src/zira_dashboard/routes/missing_wc.py))
registered in [`app.py`](../../../src/zira_dashboard/app.py).

## Decisions (from brainstorming)

- **Who's covered:** *anyone* still clocked in across midnight — hourly,
  salaried, or manager. No wage-type filter (unlike the Late / Missing-WC
  alerts). The goal is data hygiene: no open record should ever accrue hours
  forever.
- **Night shift:** none. The plant is day-shift only, so an open record at
  midnight is always a forgotten punch. Auto-close at midnight is always the
  correct baseline.
- **Alert actions:** **enter the real punch-out time only.** No "dismiss" — the
  *only* way a flagged row clears is by entering a corrected time, so the badge
  nags until every missed punch is fixed.
- **Rounding:** the corrected time is **stored exactly as entered**. The
  manager's correction is authoritative; no kiosk punch rounding is applied.

## Goals

1. A background job auto-closes every `hr.attendance` still open from a **prior
   day**, setting `check_out` to that day's **midnight** (site-local), and
   records it as a missed punch-out.
2. Surface every unresolved missed punch-out as a **nav badge + modal that look
   and function like the Missing-WC report** — **"⏰ N Missed Punch Out"** on
   every page.
3. Let a manager type the employee's **actual punch-out time** for a flagged
   record; this rewrites the Odoo attendance's `check_out` from midnight to the
   entered time (stored exactly), and the row clears.
4. Clean up the existing backlog (Jesus Moreno) on first deploy: close each
   stale open record at the midnight following **its own** check-in day, and
   flag it.

## Non-goals

- **Wage-type / flex filtering.** Unlike the Late / Missing-WC alerts, every
  open-across-midnight record is flagged regardless of wage type.
- **A "dismiss" path.** Explicitly excluded (Dale's choice).
- **Rounding the corrected time.** Stored verbatim.
- **Cross-midnight (overnight) shifts.** Ruled out — day-shift-only plant. The
  correction is constrained to the check-in day; genuinely overnight work would
  be a manual Odoo edit.
- **Splitting/merging records.** We only ever set/overwrite one `check_out`.

## Design

### Detection + auto-close (new warmer tick)

A new `_tick_missed_punch_out` joins the warmer registry in
[`app.py`](../../../src/zira_dashboard/app.py) (`_WARMERS`), running ~every 60s.
Cadence does **not** affect correctness: the close time is *computed* from the
record's check-in day, not from "now," so it is always exactly midnight no
matter when within the minute the tick fires.

```
open_rows = odoo_client.fetch_open_attendances()   # existing; one cheap Odoo read
closures  = missed_punch_out.overdue_closures(open_rows, today_site_local())
for c in closures:
    odoo_client.clock_out(c.att_id, c.midnight)     # existing; sets check_out
    missed_punch_out.record_close(c.att_id, c.employee_odoo_id,
                                  c.name, c.check_in, c.midnight)
```

- [`fetch_open_attendances()`](../../../src/zira_dashboard/odoo_client.py:536)
  already returns `[{att_id, employee_odoo_id, check_in (ISO UTC), wc_name}]` for
  every open record. **No new Odoo function is needed.**
- **`overdue_closures(open_rows, today)`** (pure, unit-testable, no DB/Odoo):
  for each row, convert `check_in` to **site-local** and take its date. If that
  date `< today` (site-local), it's overdue. Emit
  `{att_id, employee_odoo_id, check_in, midnight}` where
  `midnight = datetime.combine(check_in_date + 1 day, 00:00, SITE_TZ)`. Records
  whose check-in is *today* are left alone (normal in-progress shifts).
- [`clock_out(att_id, ts)`](../../../src/zira_dashboard/odoo_client.py:694) sets
  `check_out`; it is safe on an already-closed record (it just overwrites).
- `record_close` inserts the flag row `ON CONFLICT (attendance_id) DO NOTHING`
  → **idempotent**. Once closed, the record is no longer open, so the next tick
  won't re-find it; the `ON CONFLICT` guard also protects against any overlap.
- **Name** is resolved at close time from `people` (`odoo_id → name`), falling
  back to `"#<employee_odoo_id>"` if the employee isn't in `people`. We never
  skip a closure for a missing name — the close already happened and needs a
  correction.

Because Odoo is the system of record and the open-attendance cache refreshes
from Odoo within ~30s
([`live_cache.refresh_odoo_open_attendance`](../../../src/zira_dashboard/live_cache.py)),
closing in Odoo propagates the clocked-out state to every screen (kiosk
included) automatically. No local `timeclock_punches_log` punch is written; this
is a cleanup correction, not a kiosk action. (Trade-off noted in Risks.)

### New table — `missed_punch_out`

A single audit table (added to
[`_schema.py`](../../../src/zira_dashboard/_schema.py)):

```sql
CREATE TABLE IF NOT EXISTS missed_punch_out (
  attendance_id    BIGINT PRIMARY KEY,
  employee_odoo_id BIGINT NOT NULL,
  name             TEXT,                 -- resolved at close time; fallback "#<id>"
  check_in         TIMESTAMPTZ NOT NULL, -- from the Odoo record
  auto_closed_at   TIMESTAMPTZ NOT NULL, -- the midnight we set
  corrected_at     TIMESTAMPTZ,          -- the real punch-out the manager entered
  resolved_at      TIMESTAMPTZ,          -- NULL = still flagged
  flagged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Unresolved rows (`resolved_at IS NULL`) drive the alert. A corrected row keeps
its history for audit (`corrected_at`, `resolved_at` set). No `dismissed` state
exists.

### `missed_punch_out.py` (new — mirrors `missing_wc.py`)

```python
def overdue_closures(open_rows: list[dict], today) -> list[dict]   # pure date math
def record_close(att_id, employee_odoo_id, name, check_in, auto_closed_at) -> None
def current_rows() -> list[dict]        # unresolved rows shaped for badge/modal
def correct(attendance_id: int, corrected_ts) -> dict | None       # mark resolved; returns the row
```

`current_rows()` reads unresolved rows (all-local Postgres, no Odoo) and shapes
each as:
`{attendance_id, name, check_in_label ("6:58 AM Mon Jun 8"),
auto_closed_label ("12:00 AM Tue"), employee_odoo_id, check_in_date (ISO date)}`,
sorted by `check_in` descending. `check_in_date` lets the front-end show the day
the entered time applies to.

### Routes — `routes/missed_punch_out.py` (new, mirrors `routes/missing_wc.py`)

Registered in `app.py` alongside `missing_wc.router`.

- **`GET /api/missed-punch-out`** → `{count, rows}` from `current_rows()` (the
  badge polls this; cheap, all-local, no Odoo I/O).
- **`POST /missed-punch-out/correct`** — body `{attendance_id, time}` where
  `time` is `"HH:MM"` (24-hour, from an `<input type="time">`):
  1. Look up the flag row; 404 if unknown / already resolved.
  2. `corrected_ts = combine(check_in.date(), parsed_time, SITE_TZ)`.
  3. **Validate** `check_in < corrected_ts ≤ auto_closed_at` (after they clocked
     in, and on/within the check-in day — overnight is ruled out). 400 on bad
     input.
  4. `odoo_client.clock_out(att_id, corrected_ts)` — overwrites midnight → the
     entered time, stored exactly (no rounding).
  5. `missed_punch_out.correct(att_id, corrected_ts)` — set `corrected_at` +
     `resolved_at`. Returns `{ok: true}`; the row drops from the next poll.

### Badge + modal (`footer.js` / `footer.css`, mirrors the Missing-WC block)

A new block mirroring the Missing-WC badge:
- Poll `GET /api/missed-punch-out`; when `count > 0`, inject a badge
  **"⏰ N Missed Punch Out"** (new `.mpo-nav-badge`, styled like
  `.mwc-nav-badge`).
- Click → modal (`.mpo-modal`, same structure/classes-of-style as the Missing-WC
  modal) listing each row:
  `name · "clocked in 6:58 AM Mon Jun 8 · auto-closed at midnight"` with an
  inline `<input type="time">` + **Save** →
  `POST /missed-punch-out/correct {attendance_id, time}` → row shows
  "Corrected ✓", removed on refresh.
- Reuse the Missing-WC / Late CSS by paralleling `.mpo-*` rules in the same
  stylesheet.

## Acceptance criteria

- An `hr.attendance` open with a `check_in` on any day before today is, within a
  tick, closed in Odoo at the midnight ending its check-in day and appears as a
  row in the "⏰ N Missed Punch Out" modal.
- A record whose `check_in` is **today** is never touched (in-progress shift).
- Jesus Moreno's stale open record is closed at the midnight following *his*
  check-in day (not tonight's) and flagged on first deploy.
- Entering a time for a flagged row rewrites the Odoo `check_out` to exactly that
  time on the check-in day (verified via `clock_out`), and the row disappears.
- A corrected time that is before/equal to check-in, or after midnight, is
  rejected (400) and the row stays flagged.
- There is no dismiss action; the badge persists until every flagged row has a
  corrected time.
- The auto-close is idempotent — re-running the tick never double-closes or
  duplicates a flag row.
- `GET /api/missed-punch-out` does no Odoo I/O (reads the table); the tick owns
  the Odoo reads/writes.
- The badge/modal render and behave like the Missing-WC report, on every page.

## Risks

- **Hours overstated until corrected.** Between auto-close (midnight) and the
  manager's correction, the record reads as ending at midnight, overstating that
  day's hours. Intended: far better than an open record accruing forever, and the
  persistent badge nags until corrected.
- **Kiosk state during a cache outage.** State is closed in Odoo only; the
  open-attendance cache reflects it within ~30s. If the cache is *stale* at the
  moment the person next visits the kiosk, the local-log fallback
  ([`attendance_state.state_from_log`](../../../src/zira_dashboard/attendance_state.py))
  could briefly read their old (synced) clock-in as still open. Rare, transient,
  self-heals on the next cache refresh; the same risk exists for any Odoo-side
  close. No local punch is written, by design.
- **Employee not in `people`.** Name falls back to `"#<odoo_id>"`; the row is
  still flagged and correctable. Acceptable.
- **Multiple open records / multi-day spans.** Each open record is flagged
  independently and closed at the midnight following its own check-in day. A
  record open across several midnights still closes at the *first* midnight after
  its check-in (its real shift was that day).
- **DST / timezone.** All "today" and midnight math is done in `SITE_TZ`
  (`shift_config.SITE_TZ`); Odoo stores naive-UTC, so `check_in` is converted to
  site-local before taking its date. The same conversion the other features use.

## File touch list

- Modify: `src/zira_dashboard/_schema.py` — `missed_punch_out` table.
- New: `src/zira_dashboard/missed_punch_out.py` — `overdue_closures` (pure),
  `record_close`, `current_rows`, `correct` (mirrors `missing_wc.py`).
- Modify: `src/zira_dashboard/app.py` — `_tick_missed_punch_out` warmer in
  `_WARMERS`; register `missed_punch_out.router`.
- New: `src/zira_dashboard/routes/missed_punch_out.py` — `GET /api/missed-punch-out`,
  `POST /missed-punch-out/correct`.
- Modify: `src/zira_dashboard/static/footer.js` — badge + modal block.
- Modify: `src/zira_dashboard/static/footer.css` — `.mpo-*` styling (reuse
  Missing-WC / Late classes where practical).
- New tests: `tests/test_missed_punch_out.py` — pure `overdue_closures`
  (prior-day vs. today, midnight computation, SITE_TZ/DST); table
  `record_close`/`current_rows`/`correct` (Postgres-gated, like missing-wc);
  the tick (mocked `fetch_open_attendances` + `clock_out`: only prior-day records
  closed at the right midnight + flagged, today untouched, idempotent); the
  correct route (validation bounds, `clock_out` called with the combined ts, row
  resolved — mocked `odoo_client.clock_out`).

## Testing note

Postgres-backed tests run in CI (the `DATABASE_URL` gate); they skip locally
where no test DB is set. The pure `overdue_closures` and label helpers are
unit-testable without a DB. Verify locally with `ruff` + `py_compile`; CI is the
authority for the DB-gated paths. **Grep for any test asserting nav-badge /
footer behavior before changing `footer.js`/`footer.css`** (a stale contract
test bit the rounding work; same care here).
