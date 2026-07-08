# Machine breakdown handling in the Exception Inbox — design

**Date:** 2026-07-07
**Status:** Approved (brainstorm), pending implementation plan

## Problem

When a recycling machine breaks down mid-shift and stops producing (the trigger
case: Dismantler 2 died and stopped producing), two things go wrong today:

1. **The operators get penalized.** Leaderboard averages measure each person
   against a *whole-shift* expected target
   (`routes/leaderboards.py` `averages_for_wc`/`averages_for_group` →
   `target_per_hour * shift_config.productive_minutes_for(day)/60`, which is
   person-independent and does **not** subtract downtime). So while the machine
   sits dead, the operator's units stop climbing but their expected keeps growing,
   quietly dragging their average down through no fault of their own.
2. **There's no fast way to re-deploy the people.** The manager has to leave the
   inbox, open the scheduler/recycling dashboard, and move each idle person by hand.

We want to handle the whole thing from the inbox: surface the breakdown, keep the
dead time off the operators' machine averages automatically, and let the manager
re-deploy each idle operator (move now / decide later) in place.

## Decisions (from brainstorm)

- **Trigger:** both auto-detect *and* a manual "Report a breakdown" button.
- **Stop time:** auto-detected (when output last landed) — not set by hand.
- **Per-operator actions:** **Transfer** and **Snooze 15 min**. (Send home was
  considered and **dropped** — an operator leaving simply punches out at the kiosk,
  which already ends their attendance and drops them off the card. No separate
  clock-out button is needed.)
- **Card-level dismiss:** a **"Not a breakdown"** button. Because the exclusion now
  applies automatically on detection, this is the safety valve against a
  false-positive (a machine that read "no output" for an innocent reason): it
  resolves the incident *and deletes* its exclusion rows, restoring normal averages,
  and suppresses re-detection for that machine until it produces again.
- **Averages (Approach A — scoped):** keep everything the operator produced before
  the breakdown; exclude only the dead window from their *expected* on that machine.
  The exclusion is **automatic** — a property of the detected breakdown applied to
  each operator on the machine, capped when that operator leaves the machine
  (transfer, punch-out, or the machine coming back). It does **not** depend on the
  manager pressing a button, because operators may just punch out to go home.
  **Historical leaderboard numbers are never touched** — only the specific
  machine/window flagged in a breakdown is affected. (Rejected Approach B: making
  averages subtract all Zira-recorded downtime globally — more correct but shifts
  everyone's existing numbers and is a bigger, riskier change. Noted as possible
  future work.)
- **Transfer** is per-person, modeled as the mirror of a normal transfer — a
  transfer records when someone *arrives* at a machine; the breakdown records when
  someone *leaves* one (which is also what caps their exclusion window).
- **Snooze** only defers a per-operator row for 15 minutes; it changes nothing about
  the exclusion (which is automatic regardless).

## What we're building

### The card

One inbox card per broken machine, one sub-row per idle operator on it:

```
⚠️ Dismantler 2 — stopped producing            [AUTO-DETECTED]
No output since 1:02 PM (23 min). Productive time on this machine stops
automatically at 1:02 PM for the operators below — it won't count against
their averages.
──────────────────────────────────────────────────────────────
Juan Perez      On since 6:58 AM · ran ~5h      [Repair 3 ▾]     [Transfer] [Snooze 15m]
Benjamin Cruz   On since 6:58 AM · ran ~5h      [Pick a spot ▾]  [Transfer] [Snooze 15m]
                                                          [Not a breakdown]  ← card-level
```

A **＋ Report a breakdown** button in the inbox header opens the same card on demand
(pick a machine; stop time = last output, else now).

### The automatic exclusion

The moment a breakdown is detected, each operator on the machine gets their
productive time on that machine excluded from the detected `stop` onward — this is
the "won't count against their averages" guarantee, and it holds no matter how the
operator eventually leaves:

- **Transferred away** → exclusion window is `[stop, transfer time]`.
- **Punched out to go home** → exclusion window is `[stop, punch-out time]`.
- **Stayed on the machine** (e.g. helping fix it) until it resumed or shift end →
  `[stop, resume-or-shift-end]`.

`testing` (the existing exclusion) removes **units** produced in a window and keeps
expected; `breakdown` is the mirror — it removes **expected minutes** and keeps
units (there are ~none in a dead window anyway, so no unit adjustment is needed).

### The two per-operator actions

- **Transfer** → moves the operator to the chosen work center **now** (normal
  transfer + Odoo punch move, reusing `staffing_transfer.decide_and_apply`). This
  also caps their breakdown exclusion window at the transfer time. Shows a
  **5-second Undo** (the existing inbox pattern).
- **Snooze 15m** → hides that operator's row for 15 minutes; it reappears if the
  machine is still down when the snooze expires. Changes nothing about the exclusion.

**Not a breakdown** (card-level) resolves the whole incident, deletes its exclusion
rows, and suppresses re-detection of that machine until it produces again. Shows a
5-second Undo.

The card clears when every operator has left the machine (transferred or punched
out), the machine starts producing again (auto-resolve), or it's dismissed.

### Display rules

- Operators shown = people currently resolved onto that work center and clocked in
  (from `assignment_windows.resolve_segments` / `who_by_wc` + live open attendance),
  minus anyone whose per-operator snooze is still active.
- The "stopped at" time and elapsed are read-only (auto-detected).
- Priority: `urgent` (a dead machine with idle people on it is top-of-queue).

## Architecture

Grounded in the existing inbox pattern (`docs/superpowers/specs/2026-06-26-inbox-queue-archive-audit-design.md`
and the six-place "new inbox category" checklist). New code favors small,
independently testable units with logic separated from I/O.

### 1. Detection — new `src/zira_dashboard/machine_breakdown.py`

- **`detect(station_totals, operators_by_wc, now, shift_window, known, testing_windows)`**
  — **pure, no I/O.** Given per-station Zira data
  (`leaderboard.StationTotal.active_intervals` / `downtime_minutes` /
  `active_minutes`), returns candidate incidents: a metered recycling station
  (`stations.recycling_stations()` — Dismantler 1–4, Repair 1–3, Trim Saw, Junior 2)
  that has produced **no output for ≥ `BREAKDOWN_NO_OUTPUT_MINUTES` (default 15)**
  during shift hours, has ≥1 operator clocked in on it, is not inside a testing
  window, is not already a known/open incident, and has **not been dismissed** for
  the day without producing since (the "Not a breakdown" suppression). `stop` = end
  of the last active interval.
- **`current_rows()`** — **I/O.** Reads the cached station day
  (`leaderboard.cached_leaderboard` over `recycling_stations()`), the live operator
  resolution, and open incidents; persists newly-detected incidents; maintains the
  per-operator exclusion rows (open at detection, capped on departure/resume — see
  §3); returns the open incidents (with operators + snooze state) for the snapshot.
  Mirrors the shape of `missing_wc.current_rows` / `missed_punch_out.current_rows`.
- **`run_detect_tick()`** — folded into an existing warmer (the ~45s production
  warmer that already fetches station data), registered in
  `src/zira_dashboard/app.py`. Kill-switch env `MACHINE_BREAKDOWN_ENABLED`
  (default on) + tunable `BREAKDOWN_NO_OUTPUT_MINUTES`, following the project's
  feature-flag convention.
- **`report_manual(wc_name, now)`** — creates an incident on demand for the manual
  button; `stop` = last output for that station, else `now`.

### 2. Data model — `src/zira_dashboard/_schema.py`

- **`machine_breakdowns`** — the incident anchor (stabilizes the detected stop and
  the item key, prevents duplicate cards):
  `id, wc_name, day, detected_stop_utc, source ('auto'|'manual'), created_at,
  resolved_at, resume_utc, resolution ('recovered'|'handled'|'dismissed'|NULL)`.
  One open incident per `(wc_name, day)`. A `dismissed` incident suppresses
  re-detection of that machine until it produces output again.
- **`breakdown_snoozes`** — per-operator 15-min deferral (mirrors `late_snoozes`):
  `breakdown_id, person_name, snooze_until`.
- **Exclusion reuses the existing `wc_time_attributions` table** (schema
  `_schema.py:183-196`) with a **new `source = 'breakdown'`** — the same mechanism
  `source='testing'` already uses to keep production off a person's record. A
  breakdown row is `(day, wc_name, person_name, start_utc=stop, end_utc=cap,
  source='breakdown')`, written via a new `wc_attributions.add_breakdown(...)` /
  `cap_breakdown(...)` pair alongside the existing helpers
  (`src/zira_dashboard/wc_attributions.py`).

### 3. Automatic exclusion — the mirror of "testing"

- On detection, `current_rows()` writes one open-ended `source='breakdown'` row
  (`start_utc=stop`, `end_utc=NULL`) for each operator on the machine. Open-ended =
  "excluded from `stop` until further notice," so today's live averages are correct
  during the outage.
- Each subsequent tick **caps** an operator's row (`end_utc`) the moment they leave
  the machine: transfer time, punch-out time, or — when the incident resolves —
  the machine's `resume_utc` (or shift end). Departure is read from live attendance /
  the resolved segments, so it works identically whether the operator was moved via
  the Transfer button or just punched out.
- A shared pure helper `excluded_minutes_for(person, wc, day)` computes the overlap
  of that person's `source='breakdown'` windows with their productive shift minutes
  on that WC (using `shift_config.productive_minutes_in_window`, which already nets
  out breaks).
- **Precompute** (`src/zira_dashboard/precompute.py` / `production_history.py`
  `attribute_for_day`): store the per-person excluded minutes on `production_daily`
  (new `excluded_minutes` column) so the leaderboard reads don't need extra queries.
- **Leaderboard averages** (`routes/leaderboards.py` `averages_for_wc` :61-67 and
  `averages_for_group` :118-123): subtract the record's `excluded_minutes` from that
  person's expected denominator. Only people with a breakdown exclusion see a reduced
  expected; everyone else is unchanged; rows with zero exclusions (i.e. all history)
  are bit-for-bit identical to today.
- **Recycling / per-WC dashboards** (`recycling_data.compute_per_wc_expected` →
  `assignment_windows.expected_by_wc`): subtract the same excluded minutes so
  "doesn't count against averages **or anything**" holds everywhere, not just the
  leaderboards.

### 4. Transfer from a breakdown

- New `POST /api/exceptions/breakdown/transfer` that caps the operator's breakdown
  exclusion row at `now` on the **old** (broken) WC, then calls
  `staffing_transfer.decide_and_apply(person, new_wc, now)` (the existing chokepoint
  → `odoo_client.transfer` = clock-out + new clock-in tagged with the new WC). Logs
  the inbox event with `reversible=True`.

### 5. Snooze

- `POST /api/exceptions/breakdown/snooze` → upsert `breakdown_snoozes(breakdown_id,
  person, now + 15m)`. No Odoo write; the automatic exclusion is unaffected.
  `current_rows()` filters snoozed operators until expiry.

### 5b. Dismiss ("Not a breakdown")

- `POST /api/exceptions/breakdown/dismiss` → set the incident `resolved_at=now`,
  `resolution='dismissed'`, and **delete** all of its `source='breakdown'` exclusion
  rows (restoring normal averages). Detection then skips that machine until it
  produces again. Logs the inbox event with `reversible=True`.

### 6. Inbox wiring (the standard six places)

- `inbox_keys.py` — `breakdown(wc, stop_iso) -> "breakdown:{wc}:{stop_iso}"`.
- `exception_inbox.py` — `_capture` `machine_breakdown.current_rows` in
  `build_summary()` + `build_snapshot()`; append a `breakdown` section whose rows
  carry `priority="urgent"`, `item_key`, and per-operator action metadata; pass the
  work-center list for the transfer dropdowns (reuse the snapshot's existing
  `work_centers`).
- `inbox_reconcile.py` — add `breakdown` to `_SECTION_KIND` / `_KIND_SOURCE` so a
  recovered/handled incident auto-resolves with the existing completeness + grace +
  human-event guards.
- `templates/exceptions.html` — a `breakdown` branch in the `data-*` block and in
  `.row-actions` rendering the per-operator controls (WC `<select>` + Transfer +
  Snooze) plus the card-level "Not a breakdown" button.
- `static/exceptions.js` — `js-breakdown-transfer` / `js-breakdown-snooze` /
  `js-breakdown-dismiss` handlers in the delegated listener; pass `resp.event_id` to
  `resolveRow` for Transfer and Dismiss (undo); add to `refreshSharedBadge`.
- Backend handlers (§4–5b) → each does the write + `inbox_log.log_event_safe(...)` →
  returns `{"ok": True, "event_id": eid}`.

### 7. Undo

Extend the existing undo machinery in `routes/exceptions.py`:

- Add `("breakdown","transfer")` and `("breakdown","dismiss")` to `_UNDOABLE`.
- `_reverse_event` branches:
  - `breakdown/transfer` → `odoo_client.undo_transfer(closed_id, new_id)` (existing)
    **and** re-open the operator's breakdown exclusion row (clear the `end_utc` cap so
    they're back on the dead machine, excluded).
  - `breakdown/dismiss` → clear the incident's `resolved_at`/`resolution` (re-open it)
    **and** re-create the per-operator `source='breakdown'` exclusion rows.

## Testing

TDD — pure logic first, following the repo's existing inbox test suite
(`tests/test_exception_inbox.py`, `test_inbox_reconcile.py`, `test_inbox_undo*.py`).

- **`detect` unit tests** (the bulk): no-output threshold; only during shift hours;
  requires ≥1 operator; skips testing windows; skips already-known incidents;
  correct `stop` = end of last active interval; nothing when a machine is producing.
- **Exclusion lifecycle:** an open-ended `breakdown` row is written per operator at
  detection; capped at transfer time when transferred; capped at punch-out time when
  the operator clocks out on their own; capped at `resume_utc` when the machine comes
  back with the operator still on it.
- **Exclusion math:** `excluded_minutes_for` overlap vs a person's productive window
  (partial overlap, break-netting, zero when no breakdown rows); `averages_for_wc` /
  `averages_for_group` reduce expected by exactly the excluded minutes and are
  **unchanged** when there are no exclusions (regression guard on historical numbers);
  recycling per-WC expected honors the same exclusion.
- **Transfer from breakdown:** exclusion capped on the old WC + `decide_and_apply`
  called with the new WC.
- **Snooze:** row hidden until expiry, reappears after; exclusion untouched.
- **Dismiss:** incident resolved as `dismissed`, all its exclusion rows deleted,
  and the machine is not re-detected until it produces output again.
- **Inbox builder / reconcile:** a breakdown row has the expected shape and
  `item_key`; a recovered or fully-handled incident auto-resolves under the guards;
  a transient station-fetch error does not mass-resolve (and must not orphan or
  wrongly widen exclusion windows).
- **Undo:** transfer-undo reverses the Odoo state **and** re-opens the exclusion row;
  dismiss-undo re-opens the incident **and** re-creates its exclusion rows; both
  rejected outside the window / when already undone.

## Edge cases

- **Machine recovers (auto-resolve).** Every operator's open exclusion row is capped
  at `resume_utc`; the card clears. A false 15-min blip therefore excludes only the
  real no-output window — a small, defensible amount.
- **False-positive detection** (a genuine idle stretch with no work queued) → the
  automatic exclusion would credit that window as non-productive. Mitigated by the
  15-min threshold, short windows, and the **"Not a breakdown"** dismiss (§5b), which
  deletes the exclusion rows and suppresses re-detection until the machine produces
  again.
- **Operator already clocked out / on break at detection** → no open attendance, so
  they're not on the card and get no exclusion row.
- **Second breakdown of the same machine the same day** → the incident key includes
  `stop_iso`, so a distinct later stop opens a new card after the first resolves.
- **Breakdown near end of shift** → the exclusion window clamps to shift end via
  `productive_minutes_in_window`.
- **Manual report for a machine with no operators** → the picker lists all metered
  recycling stations; a card with no clocked-in operators is informational only
  (nothing to act on, no exclusion rows) and auto-resolves on the next reconcile tick.
- **Odoo write fails on transfer** → surfaced as a row error; the local punch log +
  `timeclock_sync` retry keep state consistent, as elsewhere.

## Out of scope

- **Send home / any manager-initiated clock-out** — operators punch out at the kiosk
  themselves; the existing timeclock handles it.
- **Approach B** (global downtime-aware averages for all history). Possible future
  unification.
- Any change to how station-level downtime is *detected/reported* by Zira — we only
  consume `StationTotal`.
- Live, no-reload card updates (the inbox's Phase 4b live-polling work is tracked
  separately); the card refreshes on the existing 60s inbox poll.
- Breakdowns on non-metered work centers (no output data → nothing to exclude).
