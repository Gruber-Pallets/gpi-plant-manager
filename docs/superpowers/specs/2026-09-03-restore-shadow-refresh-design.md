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

Restore the coherent attendance-rollout surface from the known-good state at
commit `15d86881`. A later merge replaced the readiness module, rollout policy,
Settings wiring, Inbox bindings, CLI, and their tests with an older rollout.
The repair is a selective three-way restoration: keep unrelated later feedback
work, but restore the compatible Task 13 implementation across its owned files.

The owned runtime files are `attendance_readiness.py`,
`attendance_location_policy.py`, `attendance_exceptions.py`,
`exception_inbox.py`, `inbox_reconcile.py`, `precompute.py`,
`routes/settings.py`, the attendance-readiness adapter in `app.py`, and the
readiness CLI. Restore the matching Task 13 tests as one coherent suite.

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
the current setting keys. Restore and run the matching Task 13 readiness,
policy, Settings, failure-mode, and end-to-end tests, then the wider attendance
suite and full suite. After deployment, verify aggregate freshness, mirror
health, and that Live remains unscheduled.

## Operational outcome

The repair restores automated Shadow observation starting today. It cannot
retroactively certify September 2, so the next complete clean workday remains
the earliest valid evidence for a Live schedule.
