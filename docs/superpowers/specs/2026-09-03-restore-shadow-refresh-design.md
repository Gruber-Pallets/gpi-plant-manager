# Restore Shadow Refresh Design

## Problem

Production is in Shadow mode, but its saved comparison stopped at 2026-09-01
08:37 CDT. The attendance mirror continues to refresh. A later merge replaced
`attendance_readiness.py` with an older interface: its
`refresh_shadow_comparison` requires a day and production client, and it reads
the earlier `odoo_attendance_shadow_epoch` setting. The active Shadow setting
was created by the current rollout code under
`odoo_attendance_location_shadow_epoch`. The older warmer therefore sees no
Shadow epoch and returns without recording an observation. The same merge also
changed the app warmer back to the older `run_warmer_tick(...)` call.

## Decision

Restore `src/zira_dashboard/attendance_readiness.py` from the known-good
attendance rollout state at commit `15d86881`. That version matches the
current local-only readiness and cutover fences, and uses the existing
production Shadow setting keys. Restore only the corresponding
`_tick_attendance_readiness()` call in `src/zira_dashboard/app.py`; preserve
the later app changes outside that narrow adapter.

Do not adapt the older module with a compatibility wrapper. That would leave
older readiness rules and state formats in production. Do not force Live or
manufacture yesterday's Shadow observation.

## Behavior

- The real app warmer calls `attendance_readiness.tick()` without arguments
  every 30 seconds.
- In Shadow mode, each successful tick writes an aggregate-only comparison for
  the current plant day and preserves verified earlier clean days.
- Live remains disabled. A valid completed Shadow day and a normal readiness
  report are still required before scheduling a future cutover.
- Existing department policy and the salaried no-work-center exemption remain
  unchanged.

## Validation

Add a regression test through `app._tick_attendance_readiness()` that creates
a current Shadow epoch and proves the app/worker path stores a comparison using
the current setting keys. Run the focused readiness/app tests, then the
relevant attendance suite and full suite. After deployment, verify aggregate
freshness, mirror health, and that Live remains unscheduled.

## Operational outcome

The repair restores automated Shadow observation starting today. It cannot
retroactively certify September 2, so the next complete clean workday remains
the earliest valid evidence for a Live schedule.
