# Auto-Lunch for the Timeclock — Design

**Date:** 2026-06-02
**Status:** Design — approved in brainstorming; not yet implemented.

## Context

Today the Timeclock writes one continuous `hr.attendance` record from a person's
morning clock-in to their end-of-day clock-out (work-center **transfers** are the
only thing that split it — `transfer_out` closes one attendance and
`transfer_in` opens the next). **Lunch is not deducted:** employees stay clocked
in through the lunch break, so Odoo's worked-hours total includes the unpaid
lunch. With StratusTime decommissioned and Odoo now the system of record for
attendance, that overpayment needs to be closed automatically.

The pieces this builds on already exist:

- **Schedules already define lunch.** `shift_config.breaks_for(day)`
  (`src/zira_dashboard/shift_config.py`) resolves the day's breaks in priority
  order — per-date `custom_hours` breaks → Saturday default → weekday global —
  and returns `Break(start, end, name)` tuples. The lunch break is the one named
  **"Lunch"** (global default `11:00–11:30`, Saturday default `10:00–10:30`).
- **Punch → Odoo plumbing is solved.** Punches land in
  `timeclock_punches_log`, then sync to Odoo `hr.attendance` via an immediate
  background task plus the 60s `retry_unsynced_punches()` sweep
  (`src/zira_dashboard/timeclock_sync.py`). The sync is **self-correcting**:
  clock-in calls `get_current_attendance()` before creating (no duplicate open
  attendance) and clock-out closes whatever Odoo actually has open.
- **State reconciliation is solved.** Per the
  `2026-06-01-timeclock-odoo-state-reconciliation` design, `_current_state()`
  (`src/zira_dashboard/routes/timeclock.py`) decides the kiosk screen from the
  local log reconciled against the `odoo_open_attendance_cache` snapshot
  (refreshed ~30s by `_warm_odoo_attendance_loop`), with a race-guard so a fresh
  kiosk tap never flickers.
- **Per-employee schedules are mirrored.** `people.resource_calendar_id` points
  at an Odoo `resource.calendar`; `work_schedules`
  (`src/zira_dashboard/work_schedule_store.py`) mirrors its per-weekday hours.

Two employees (Juan, Benjamin) are on Odoo **flexible** work schedules with
variable start times, so a fixed wall-clock lunch window doesn't fit them — they
need a lunch triggered by elapsed time on the clock instead.

This feature adds a background worker that signs people out at lunch start and
back in at lunch end (creating the unpaid gap in Odoo), reusing all of the above.

## Goals

1. On a **published workday**, every currently-clocked-in **fixed-schedule**
   employee is auto-signed-out at the day's **Lunch** break start and
   auto-signed-in at its end — producing one unpaid lunch gap in Odoo
   (two `hr.attendance` records: morning + afternoon).
2. **Flex** employees (Odoo Schedule Type = flexible) get **at most one** auto
   lunch per day — **never two, even on a 10-hour day** — triggered by **elapsed
   time since their first clock-in** (default **5h**), for a fixed duration
   (default **30 min**), independent of the published schedule.
3. **Manual punches never break it.** The automation only ever reverses its own
   actions, never double-punches, and an employee who **signs out during the
   lunch gap ends their day** (the pending auto sign-in is cancelled).
4. Auto-punches are **durable and auditable** — they flow through
   `timeclock_punches_log` and the existing sync/retry path, tagged
   `source='auto_lunch'`.
5. **Safe rollout:** a master enable toggle plus an **observe-only** mode that
   logs intended actions without writing any punch.

## Non-goals

- **No scheduling of other breaks.** Morning/afternoon/cleanup breaks stay paid
  (clocked-in). Only the break named **Lunch** is acted on.
- **No early-return shortening.** The **full** scheduled lunch is always
  deducted; there is no "I'm back early" affordance. (Decided in brainstorming.)
- **No per-employee flex parameters.** One global flex rule (after-hours +
  minutes) covers all flex employees. Odoo tells us *who* is flex, not their
  numbers — those live in app settings.
- **No change to worked-hours math in Odoo.** The gap *is* the deduction; two
  attendance records per day is expected and already happens with transfers.
  `fetch_attendances_for_day()` already coalesces to the first check-in per
  person, so dashboards are unaffected.
- **No kiosk UI redesign.** The only kiosk-facing change is that `_current_state`
  treats an active auto-lunch gap as "on shift" (so the screen still offers
  **sign out**), and clock-out during the gap cancels the auto sign-in.
- **No live Odoo call on any hot path.** The worker reads the existing
  open-attendance cache + local log; writes go through the existing sync.
- **No cron / webhooks.** A new `asyncio` loop, matching the house pattern.

## Architecture

*A background worker drives a per-person/per-day lunch state machine, reusing the
existing punch-log → sync path for all Odoo writes and the existing
cache/log reconciliation for all reads.*

```
            ┌──────────────────────────────────────────────────┐
   reads →  │  _warm_auto_lunch_loop()  (asyncio, ~60s)         │
            │      └─ auto_lunch.run_tick(now)                  │
            └───────────────┬──────────────────────────────────┘
   shift_config.breaks_for(day) / is_workday(day)   (lunch window)
   odoo_open_attendance_cache + timeclock_punches_log (who's in, since when, WC)
   work_schedules.is_flexible                       (the flex set)
   auto_lunch_settings                              (enabled / observe / X / Y)
                            │
                            ▼ advances state machine, writes punches
   ┌──────────────────────────────────────────────────────────┐
   │ timeclock_punches_log  (NEW col: source='auto_lunch')      │
   │ auto_lunch_runs        (NEW: per-person/day state)         │
   └───────────────┬──────────────────────────────────────────┘
                   ▼ existing immediate task + 60s retry sweep
              Odoo hr.attendance  (morning close + afternoon open)
```

The worker never calls Odoo directly. It writes log rows exactly like a kiosk
tap and lets `timeclock_sync` carry them over with its existing retry and
self-correction.

---

## Part 1 — Data model changes

### `timeclock_punches_log` — add `source`

```sql
ALTER TABLE timeclock_punches_log
  ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'employee';
-- values: 'employee' (every existing + kiosk row) | 'auto_lunch' (this worker)
```

`source` is what lets the worker recognize its own actions, lets reports tell
auto-lunch apart from real punches, and (critically) lets the kiosk's
shift-session logic ignore auto rows when deciding the screen (Part 4).

### `auto_lunch_runs` — per-person/day state (NEW)

The state machine store. The `UNIQUE (person_odoo_id, day)` constraint is the
structural guarantee of **one lunch per day**, and because it lives in Postgres
it **survives Railway redeploys** (a restart mid-lunch never re-deducts).

```sql
CREATE TABLE IF NOT EXISTS auto_lunch_runs (
  id              BIGSERIAL PRIMARY KEY,
  person_odoo_id  INTEGER NOT NULL,
  day             DATE    NOT NULL,
  kind            TEXT    NOT NULL CHECK (kind IN ('scheduled','flex')),
  state           TEXT    NOT NULL CHECK (state IN
                    ('pending','auto_out','done','skipped','ended_by_employee')),
  target_out_at   TIMESTAMPTZ,   -- lunch start  (fixed) | first_in + X h (flex)
  target_in_at    TIMESTAMPTZ,   -- lunch end    (fixed) | target_out + Y min (flex)
  wc_name         TEXT,          -- work center captured at auto-out, restored at auto-in
  out_punch_id    BIGINT,        -- the auto clock_out row in timeclock_punches_log
  in_punch_id     BIGINT,        -- the auto clock_in row
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (person_odoo_id, day)
);
```

**States:** `pending` (no action yet) → `auto_out` (we signed them out, owe an
auto-in) → `done` (auto-in written, or lunch resolved with no action needed).
Terminal side-states: `skipped` (they were already clocked out at trigger time —
we never touch them) and `ended_by_employee` (they signed out during the gap —
auto-in cancelled).

### `work_schedules` — add `is_flexible`

```sql
ALTER TABLE work_schedules
  ADD COLUMN IF NOT EXISTS is_flexible BOOLEAN NOT NULL DEFAULT FALSE;
```

Synced from Odoo (Part 3). `FALSE` for every existing/fixed schedule, so behavior
is unchanged until a calendar is actually marked flexible in Odoo.

### `auto_lunch_settings` — singleton (NEW)

Modeled on `global_schedule` / the other singleton stores (a single `id=1` row,
read through a small `auto_lunch_settings` module with an in-process cache).

```sql
CREATE TABLE IF NOT EXISTS auto_lunch_settings (
  id                    INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  enabled               BOOLEAN NOT NULL DEFAULT FALSE,  -- master switch (ship dark)
  observe_only          BOOLEAN NOT NULL DEFAULT TRUE,   -- log intended actions, write nothing
  flex_after_hours      NUMERIC NOT NULL DEFAULT 5.0,    -- flex trigger threshold
  flex_minutes          INTEGER NOT NULL DEFAULT 30      -- flex lunch length
);
```

Defaults mean: until someone flips `enabled`, nothing happens; the first enable
runs **observe-only** so a real day can be sanity-checked against the log before
it touches payroll. A small panel in the existing settings sidebar exposes the
toggle + two numbers (minor UI add; the feature works from defaults without it).

---

## Part 2 — The worker (`auto_lunch.run_tick`)

A new `src/zira_dashboard/auto_lunch.py`. Pure, table-driven logic so it's unit
testable under the Python-3.9 local constraint. A sibling loop in `app.py`,
modeled on `_warm_odoo_attendance_loop`, calls it every ~60s:

```python
async def _warm_auto_lunch_loop():
    while True:
        try:
            await asyncio.to_thread(auto_lunch.run_tick)
        except Exception as e:               # never let the worker kill itself
            _log.warning("auto-lunch tick failed: %s", e)
        await asyncio.sleep(60)
```

…registered/cancelled in `lifespan()` alongside the existing tasks.

**Each tick:**

1. Load `auto_lunch_settings`. If `enabled` is false → return immediately.
2. `now` = current plant-local time; `day` = today.
3. **Fixed lunch window:** if `is_workday(day)`, scan `breaks_for(day)` for the
   break whose name lowercases to `"lunch"` → build tz-aware `lunch_start` /
   `lunch_end` for today (same local→tz construction other schedule code uses).
   No workday, or no Lunch break that day → no fixed lunches this tick.
4. **Flex set:** the `resource_calendar_id`s whose `work_schedules.is_flexible`
   is true → the people on them.
5. **Current state:** for each person, the **reconciled** real attendance state —
   the cache-vs-local-log decision from the reconciliation design, **including its
   race-guard** (an unsynced punch is trusted over the cache), but **without** the
   Part-4 auto-lunch overlay. The race-guard matters here: right after we write
   the auto `clock_out`, that unsynced row makes the reconciled state read
   **clocked out** even though the cache still shows the morning attendance open —
   so the auto sign-in correctly fires at lunch end instead of being skipped (which
   would otherwise strand the person clocked out all afternoon). Also pull, per
   person for today: **first clock-in** (their morning `clock_in`) and **latest
   punch**.
6. Advance each relevant person's `auto_lunch_runs` row (lazily inserted as
   `pending`). **Relevant** = anyone clocked in now, or with punches today, or
   with a non-terminal run. Timestamps written onto punches are the **scheduled
   boundary times**, never the moment the tick happens to run — so a tick at
   11:00:43 still stamps the clock-out at 11:00:00. Because auto-punches are
   stamped exactly at the schedule, punch **rounding is a no-op** for them.

**Observe-only:** the worker runs the full state machine and **persists
`auto_lunch_runs`** rows (so the day's intended actions are queryable —
"what would auto-lunch have done today?") and `log()`s each intended punch, but
writes **no** `timeclock_punches_log` rows and makes **no** Odoo change. Because
runs are keyed by `day`, going live is a **between-days** flip: set
`observe_only=FALSE` and the next day's fresh runs write for real. (Flipping it
off mid-day is not a supported transition — that day's runs may already be
advanced.)

**Catch-up / restarts:** triggers are "past the target time and the run isn't
done," not "exactly at." A late tick or a redeploy resumes from the persisted
run state — it stamps the correct boundary times and never double-acts.

---

## Part 3 — Flex detection (sync from Odoo)

An employee is **flex** iff their `resource.calendar`'s **Schedule Type** is
*flexible* (vs *fully fixed*). `odoo_sync.refresh_work_schedule_hours()` already
does a `resource.calendar` read to refresh per-weekday hours; we add the Schedule
Type field to that same `search_read` and persist it via
`work_schedule_store.refresh_synced(...)` onto the new `is_flexible` column
(alongside the Odoo-owned `name`/`work_hours`; the app-owned rounding columns are
untouched, same contract as today).

> **Confirm at plan time:** the exact Odoo technical field name for "Schedule
> Type" (e.g. `flexible_hours` boolean, or a `schedule_type`-style selection) and
> the value that means flexible. This is one field added to an existing
> `search_read` — it does not affect any of the logic below.

If the field is briefly unreadable, `is_flexible` keeps its last synced value
(fixed by default), so a sync blip degrades to "treat as fixed," never to a wrong
auto-deduction.

---

## Part 4 — Kiosk behavior during the lunch gap (shift-session)

The kiosk must keep offering **sign out** during the lunch gap, because from the
employee's point of view they never left — the auto-out is invisible background
payroll. Implemented as a thin overlay on the existing reconciliation, enabled by
the `source` tag:

- **Screen decision (`_current_state`):** after the normal cache/log
  reconciliation, if the person has an **active** run (`state='auto_out'` and
  `now` within `[target_out_at, target_in_at)`), **override** the result to
  *clocked-in* at `run.wc_name`, and expose `on_auto_lunch=True` +
  `auto_lunch_run_id`. So during the gap — when raw Odoo state is "no open
  attendance" — the kiosk still shows the on-shift screen (**sign out** /
  transfer). Outside that window the function is unchanged.

- **Sign out during the gap (`kiosk_clock_out`):** if the person is
  `on_auto_lunch`, then after logging their `clock_out` (an `employee` row), set
  the run to `ended_by_employee`. Effects:
  - The auto sign-in **never fires** (worker only acts on `state='auto_out'`).
  - Their morning attendance is already closed at lunch start, so the employee
    clock-out is a **no-op to Odoo** — the sync's `get_current_attendance()`
    finds nothing open and closes nothing. **Day ends at lunch start.** (If the
    auto-out hadn't synced yet, the two log rows process in `occurred_at` order:
    the auto `clock_out` @ lunch-start closes the morning record, the employee
    `clock_out` finds nothing open → no-op. Same result.)

Because sign-out now means "end of day," an employee **cannot punch their own
mid-day lunch** — which is why the flex double-deduction guard isn't needed: the
only intraday gap is the one the worker creates.

---

## Part 5 — The state machine (both kinds are symmetric)

Let `lunch_out` / `lunch_in` be the trigger/return times for the person:

- **Fixed:** `lunch_out = lunch_start`, `lunch_in = lunch_end` (from the day's
  schedule).
- **Flex:** `lunch_out = first_clock_in_today + flex_after_hours`,
  `lunch_in = lunch_out + flex_minutes`. If the person has no clock-in today,
  there is no trigger.

Per person, per tick:

| Run state | Condition | Action |
|---|---|---|
| `pending` | `now ≥ lunch_out`, currently **clocked in** | Write auto `clock_out` @ `lunch_out`; capture `wc_name`; set `target_in_at=lunch_in`; → `auto_out` |
| `pending` | `now ≥ lunch_out`, currently **clocked out** | They're handling their own day → `skipped` (never auto-in someone we didn't auto-out) |
| `auto_out` | `now ≥ target_in_at`, currently **clocked out** | Write auto `clock_in` @ `target_in_at` at `wc_name`; → `done` |
| `auto_out` | `now ≥ target_in_at`, currently **clocked in** | Already in (e.g. manual Odoo add) → `done`, no action (never double-punch) |
| `auto_out` | employee signed out in the gap | Set by `kiosk_clock_out` → `ended_by_employee` (auto-in cancelled) |
| `done` / `skipped` / `ended_by_employee` | — | Terminal; nothing |

This is the full "don't fight a human" behavior:

| Situation | Outcome |
|---|---|
| Clocked in at lunch start | Auto-out at start, auto-in at end |
| Already clocked out at lunch start (left / not yet in) | `skipped` — never auto-in |
| Signs out **during** the lunch gap | Day ends at lunch start; auto-in cancelled |
| Absent / on time-off / day off | Not clocked in → `skipped` |
| Flex day runs 10h | One `auto_lunch_runs` row → exactly one lunch |
| Server restarts mid-lunch | Run row in Postgres → resumes, no double-deduct |

---

## Edge cases

- **Transfer + lunch same day.** Auto-out captures the person's *current* WC
  from the reconciled state; auto-in restores it. A day with a transfer *and*
  lunch is 3+ attendance records — sums correctly; dashboards coalesce.
- **Auto-out hasn't synced when auto-in is due (sub-30s, rare).** The auto-in
  clock-in is self-correcting (`_retry_one` checks `get_current_attendance`
  first): nothing open → create the afternoon record; something unexpectedly open
  → adopt it instead of duplicating.
- **No Lunch break configured for the day** (custom day with breaks omitted, or
  a non-workday) → no fixed auto-lunch that day. Flex is unaffected (it's
  elapsed-time based, not schedule based).
- **Flex person ends day before reaching X hours** → never clocked in at the
  trigger → `skipped`; no lunch (they worked a short straight shift). Correct.
- **Cache/warmer down** → the worker reads stale-or-missing open state; it should
  **skip acting** that tick rather than guess (no auto-punch on unknown state),
  and catch up once the cache is fresh. Mirrors the kiosk's safe degradation.
- **Person without `odoo_id`** → no attendance, never relevant; skipped.
- **Residual runaway:** someone who leaves during lunch *and never touches the
  kiosk* still gets auto-signed-in. This is **no worse than today** (a missed
  punch-out is already a supervisor correction) and is the rare case left after
  shift-session sign-out handles the common one.

## Testing strategy

Per the local constraint (Python 3.9; full suite runs in CI/Railway; local verify
via `py_compile` + ast-exec; fully-stubbed pytest files run locally):

- **State machine (pure logic, no Odoo):** each row of the Part-5 tables as a
  unit test — fixed and flex; clocked-in vs out at trigger; early sign-out in the
  gap; already-in at return; restart resume from each persisted state; the
  one-per-day `UNIQUE` guarantee on a 10h flex day.
- **Trigger-time computation:** fixed pulls the **Lunch** break from
  `breaks_for(day)` across custom-day / Saturday / global resolution; flex =
  `first_clock_in + flex_after_hours`; punches stamped at boundary times, not
  tick time.
- **`_current_state` overlay:** active `auto_out` window → reports clocked-in at
  `wc_name` + `on_auto_lunch`; outside the window / other states → unchanged
  (race-guard from the reconciliation design still holds).
- **`kiosk_clock_out` during the gap:** sets `ended_by_employee`, auto-in never
  fires, Odoo close is a no-op (mocked `odoo_client`).
- **Flex sync:** Schedule Type = flexible → `is_flexible=TRUE`; fully-fixed →
  `FALSE`; unreadable field → prior value retained.
- **Observe-only:** worker advances run states and logs intended actions but
  writes **zero** `timeclock_punches_log` rows.

## Done criteria

- ☐ `source` column, `auto_lunch_runs`, `work_schedules.is_flexible`, and
  `auto_lunch_settings` migrations ship (idempotent, additive).
- ☐ `_warm_auto_lunch_loop` registered; one settings read per tick; no Odoo call
  on the worker path.
- ☐ Fixed-schedule clocked-in employees get morning-close + afternoon-open in
  Odoo around the day's Lunch window; full lunch always deducted.
- ☐ Flex employees (Schedule Type = flexible) get exactly one lunch per day,
  triggered at first-clock-in + X hours for Y minutes.
- ☐ Signing out during the gap ends the day and cancels the auto-in; the kiosk
  shows **sign out** throughout the gap.
- ☐ Already-out-at-trigger employees are never auto-signed-in.
- ☐ Restart mid-lunch never double-deducts (persisted run state).
- ☐ Master toggle + observe-only verified; ships dark, first enable is
  observe-only.
- ☐ Tests above passing in CI.

## Open Questions

To confirm during planning (external-system lookups, not design ambiguities):

1. Exact Odoo technical field + value for "Schedule Type = flexible" on
   `resource.calendar` (Part 3).
2. The plant-local timezone source used to build `lunch_start`/`lunch_end`
   timestamps — reuse whatever existing schedule-driven code already uses; no new
   config.
