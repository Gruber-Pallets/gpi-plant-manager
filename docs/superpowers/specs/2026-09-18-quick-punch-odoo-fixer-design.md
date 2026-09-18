# Quick-Punch Odoo Fixer

## Purpose

Correct quick-punch mistakes in Odoo's own `hr.attendance` records, not just
in the app's view. Once Odoo holds one continuous record, every screen and
number agrees, including People Performance, hours, live location views, and
payroll.

This is the follow-up to `2026-09-18-quick-punch-smoothing-design.md`, which
smooths the same mistakes in the app only and is live. The fixer reuses that
feature's rules and pallet protections to decide what to merge. It reuses the
existing Odoo correction engine (`attendance_corrections.py`) to make the
edits.

## Decisions (Dale, 2026-09-18)

- **Fully automatic, logged.** No human approves individual fixes. Every fix is
  audited and shown in the Exception Inbox archive.
- **Sign-out gaps are filled.** The merged record is continuous, so the gap of
  at most 5 minutes becomes paid time. Detour and wrong-first-pick fixes change
  only the station, never the hours.
- **Fix right after they come back.** The fixer may edit the record the person
  is clocked into right now.
- **History:** also clean up the current open pay period. Never touch a day
  payroll has processed.
- **Rollout:** an Off / Preview / Live setting, audited like Auto-Lunch. It
  ships in Preview. Dale reviews a day of previews, then switches to Live.
  The pay-period cleanup shows its list before anything is applied.

## What Counts as a Fix

The fixer runs the same smoothing as the dashboards
(`quick_punch_smoothing.smooth_quick_punches`) over one person's valid Odoo
location spans for the day. It uses the same inputs: the 5-minute limit,
blocking windows, the relief guard, and meter data for the first-pick and
orphaned-pallet guards. Any smoothed stint that differs from the real rows it
came from is a candidate fix:

- **Target:** that person, at the stint's station, from the stint's start to
  its end. The end is open if the last absorbed row is still open.
- **Sources:** the Odoo attendance IDs of every row the stint absorbed.

A candidate is applied only when it is **settled**, meaning future punches
cannot change the answer. The fixer checks these conditions each tick and
simply retries on the next tick while they are false:

1. **Settle delay:** at least 2 minutes have passed since the stint's last
   internal boundary, which is the latest start or end of an absorbed row
   other than the stint's own final end. That gives the mirror, the meters, and
   any relief punches time to arrive.
2. **Wrong first pick needs the new station to stick.** A stint whose station
   differs from its first absorbed row's station, because a short first pick
   was folded forward, is settled only once the person has been at the final
   station for more than 5 minutes. Before that, a quick return could turn it
   into a came-back merge instead. This happened to Christian at 07:03.
3. **Fresh meters:** every station the stint absorbed a blip from, meaning
   an absorbed row at a different station than the stint, needs meter data
   known through the blip's end. The meter data must not be truncated, and its
   `last_reading_at` must be at or after the blip's end. A merge that depends
   on a missing or stale meter waits.
4. **No breaks:** a stint whose absorbed blips or filled gaps overlap a
   scheduled break is never fixed in Odoo. Meters don't record pallets during
   breaks, so idleness there can't be proven.
5. **No other work in progress:** the fixer skips the person while any
   correction job for them is active, whether a manager's or its own.
6. **Fresh attendance mirror:** it skips the whole tick when the attendance
   mirror is unavailable or stale. It uses the same policy check the
   dashboards use.

## Payroll Safety

- Only days inside the current open pay period are touched
  (`staffing_hours.current_pay_period_bounds(today)`).
- It skips any person-day that has a non-draft or conflicting `hr.work.entry`
  (`odoo_client.fetch_payroll_work_entries`). That is the same rule the payroll
  work-entry guard uses.
- Hours change only when a sign-out gap is filled, and then by at most 5
  minutes, always upward.

## Correction Engine Changes

A new optional request flag, `merge`, is added to the engine. When it is
absent, every legacy plan, stored job, and manager correction behaves and
validates exactly as today.

- **Request:** `plan_correction(..., merge=False)`. When `merge` is true, the
  request mapping carries `"merge": True`. It is part of the integrity hash,
  the JSON round-trip, job binding, and `_validate_plan`'s re-derivation.
- **Closed merge:** every source row overlapping `[start, end)` becomes one
  target piece covering `[start, end)`, filling any gaps between them. Left
  and right remainders are kept as today. The survivor is the first row fully
  inside the range, as today.
- **Open merge:** the survivor is the currently open affected row
  (`check_out` is null) when there is one. Its `check_in` moves earlier and its
  station is set; the other affected rows are deleted. The live row keeps its
  Odoo ID, because the kiosk (`timeclock_punches_log.odoo_attendance_id`) and
  Luke's plant-floor app close a person's shift by that ID.
- **No-op rule:** a merge is a no-op only when exactly one row already covers
  `[start, end)` with the target station and department.
- **Operation order:** production Odoo has had 0 overlapping attendance rows
  in 90 days, so it enforces no-overlap. Updates that grow a closed row's
  interval now run after deletes, and updates that leave a row open already
  run last. This applies to every job. For manager jobs it replaces an order
  that could never succeed when Odoo rejects overlaps.
- **Attempt cap for fixer jobs:** jobs whose `item_key` starts with
  `quick-punch:` stop retrying after 6 recoverable failures (about 16 minutes
  of backoff). They are marked `failed` with an event, and an Exception Inbox
  alert is raised. Manager jobs keep today's behavior.

## The Fixer

- **New module `quick_punch_fixes.py`, pure:** `find_fixes(...)` takes one
  day's location spans, meter production times, meter freshness, scheduled
  breaks, and now. It returns `PlannedFix` records: employee, person name,
  app station name, start, end (or None for open), source attendance IDs, a
  before summary, and an `item_key`. The key is
  `quick-punch:<employee_id>:<sorted source attendance ids>`, which is stable
  for the same situation and new for any later bounce.
- **New module `quick_punch_fixer.py`, I/O:** `tick(now)` runs on a new
  60-second warmer. It does nothing when the mode is Off. Otherwise it loads
  today's inputs, calls `find_fixes`, and applies the payroll, active-job, and
  dedupe guards. Dedupe skips an `item_key` that already has a preview event,
  a job, or a failed job.
  - **Preview:** records one archive event per `item_key`, action
    `quick_punch_would_merge`, with before and after text. Odoo is never
    called.
  - **Live:** `correction_preview(merge=True)` followed by
    `create_job_from_preview`, under the system actor. The existing 15-second
    correction worker applies it: preflight re-read, verification, mirror
    update, recalculation.
- **System actor:** `actor_upn="system:quick-punch"`,
  `actor_name="Quick-punch auto-fix"`, so its events are visible by default in
  the archive, where NULL-actor events are hidden.
- **Completion audit:** fixer jobs record action `quick_punch_merged` with
  before, for example `D3 07:00–07:02 · D2 07:02–07:04 · D3 07:04–now`, and
  after, for example `D3 07:00–now`. The archive renders the before → after
  text for these events.
- **Failure alert:** a failed fixer job surfaces as an urgent Exception Inbox
  row, "Quick-punch fix couldn't finish for <name> — check Odoo", until a
  manager clears it.

## Setting

- **Storage:** a singleton `quick_punch_fix_settings` table (`mode IN
  ('off','preview','live')`, default `preview`) plus an append-only
  `quick_punch_fix_setting_events` audit table, with the same save, lock,
  cache, reconcile, and baseline pattern as `auto_lunch_settings`.
- **UI:** Settings → Timeclock gets a "Quick-punch fixer" radio (Off /
  Preview / Live) with its change history.

## Pay-Period Cleanup

- **Script:** `scripts/quick_punch_backfill.py`. It is a dry run by default and
  covers the current pay period start through yesterday. It prints every fix
  and every skip with its reason. `--yes` creates the jobs through the same
  path as Live, and refuses unless the mode is Live.
- **Past-day meters:** past days use stored meter data. A meter day that is
  truncated or missing counts as unknown, so fixes that depend on it are
  skipped.

## Interplay

- Once a fix completes, the mirror holds one row and the app-side smoothing
  has nothing left to do. People Performance, which stays on real punches,
  now shows the merged record.
- App-side smoothing still covers the minutes before a fix lands, plus any day
  or case the fixer won't touch.

## Testing

- **Engine:** merge planning for open and closed ranges, gap fill, open-row
  survivor, no-op, JSON and integrity round-trip, and tampered-flag rejection.
  Legacy plans must be unchanged, with existing tests untouched.
- **Ordering:** end-to-end against a fake Odoo that rejects overlapping rows.
- **Attempt cap and failure alert.**
- **`find_fixes`:** Christian's real day, including the 07:03 unsettled
  moment. Also the settle delay, stale or truncated meters, breaks, relief,
  orphaned pallets, and open vs closed.
- **Fixer tick:** Off does nothing. Preview writes events only and never calls
  Odoo. Live creates exactly one job per `item_key`. Also the payroll skip,
  active-job skip, stale-mirror skip, and dedupe.
- **Settings:** save, audit, reconcile, and the UI round-trip.
- **Backfill:** the dry run lists fixes and skips, and `--yes` refuses unless
  the mode is Live.

## Out of Scope

- A manual per-fix undo. The audit keeps the before rows, and a manager can
  correct them with the existing Exception Inbox correction tool.
- Days before the current pay period.
- Any change to rounding, auto-lunch, or kiosk behavior.
