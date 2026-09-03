# Restore Shadow Refresh Design

## Problem

Production is in Shadow mode, but its saved comparison stopped at 2026-09-01
08:37 CDT. The attendance mirror continues to refresh. A later merge replaced
`attendance_readiness.py` with an older interface: its
`refresh_shadow_comparison` requires a day and production client, while the
running app calls the newer no-argument `attendance_readiness.tick()` every 30
seconds. Each background tick therefore fails before it can record an
observation.

## Decision

Restore `src/zira_dashboard/attendance_readiness.py` from the known-good
attendance rollout state at commit `15d86881`. That version matches the
current app warmer contract, contains the current local-only readiness and
cutover fences, and uses the existing production Shadow setting keys.

Do not adapt the older module with a compatibility wrapper. That would leave
older readiness rules and state formats in production. Do not force Live or
manufacture yesterday's Shadow observation.

## Behavior

- The real app warmer continues to call `attendance_readiness.tick()` without
  arguments every 30 seconds.
- In Shadow mode, each successful tick writes an aggregate-only comparison for
  the current plant day and preserves verified earlier clean days.
- Live remains disabled. A valid completed Shadow day and a normal readiness
  report are still required before scheduling a future cutover.
- Existing department policy and the salaried no-work-center exemption remain
  unchanged.

## Validation

Add a regression test through `app._tick_attendance_readiness()` that proves
the app and readiness interfaces fit together and stores a Shadow comparison.
Run the focused readiness/app tests, then the relevant attendance suite and
full suite. After deployment, verify the installed production signature,
Shadow aggregate freshness, mirror health, and that Live remains unscheduled.

## Operational outcome

The repair restores automated Shadow observation starting today. It cannot
retroactively certify September 2, so the next complete clean workday remains
the earliest valid evidence for a Live schedule.
