# Strict-Day Marker Ownership Fix Design

**Date:** 2026-08-31

## Problem

The Odoo attendance-location rollout is configured `off`, but the production
Exception Inbox is showing Odoo Location Missing rows and a Strict Production
source warning.

The attendance mirror currently inserts a row into `attendance_strict_days`
whenever a mirrored attendance row changes after the baseline is complete. The
strict-day table is intentionally authoritative even if the rollout setting is
later turned off, because a day that has already saved strict production must
never silently return to legacy attribution. Those two rules combine to make a
normal attendance sync look like a successful strict-production cutover.

Production evidence on 2026-08-31 confirms the failure mode:

- the rollout setting is `off`;
- August 28, 29, and 31 have strict-day rows whose current reason is
  `odoo_attendance_changed`;
- the Odoo attendance mirror is healthy and contains real attendance rows with
  blank work-center fields;
- the strict recalculation for August 31 repeatedly fails because Dismantler 3
  sample units do not match its adjusted total.

The location rows therefore reflect blank source fields, but the application
should not be enforcing those fields while the rollout is off.

## Goals

1. Make a successful strict-production save the only operation that can make a
   new day permanently strict.
2. Continue queuing production recalculation whenever mirrored attendance is
   inserted, materially changed, recovered, or deleted.
3. Preserve existing strict markers when an already-strict day changes.
4. Remove only the falsely created production markers after the corrected code
   is deployed.
5. Restore legacy production calculation for those false days and verify the
   live Exception Inbox no longer enforces the disabled location rollout.
6. Keep the Dismantler 3 reconciliation failure visible as a separate rollout
   blocker before strict production is intentionally enabled.

## Non-Goals

- Do not fill, dismiss, or alter employee work-center attendance records.
- Do not weaken the five-minute missing-location grace period for a legitimately
  strict day.
- Do not make strict production tolerate inconsistent aggregate and sample
  totals.
- Do not enable the Odoo attendance-location rollout.
- Do not delete or rewrite saved production until the false marker set has been
  verified immediately before cleanup.

## Considered Approaches

### 1. Mark strict only when strict production is saved — selected

Attendance sync continues to enqueue recalculation with `mark_strict=False`.
`precompute._upsert_production_daily_cur` remains the single owner that inserts
the marker, in the same transaction that replaces the day's production with a
successfully prepared strict snapshot.

This matches the table's meaning, avoids rollout-state races during sync, and
keeps marker creation atomic with the data whose matching policy it protects.

### 2. Let attendance sync inspect the rollout setting

The mirror could mark strict only when the setting says `live`. This is rejected
because the setting could change between mirror ingestion and recalculation,
and ingestion has not proven that strict attribution can succeed.

### 3. Delete today's marker without changing code

This would clear the immediate screen, but the next attendance change would
recreate the marker. It treats the symptom and is rejected.

## Design

### Strict-day ownership

`attendance_mirror` will never create a new strict-day marker for an attendance
insert, update, recovery, or deletion. Both internal recalculation enqueue
calls will pass `mark_strict=False`.

The recalculation queue remains unchanged. If a day was already strict, its
existing marker still causes `attendance_location_policy.match_state_for_day`
to select strict matching. If a live rollout makes a new day strict,
`precompute.prepare_day` prepares a strict result and
`precompute._upsert_production_daily_cur` inserts the marker in the same
transaction that saves that result.

### Regression coverage

Tests will demonstrate both sides of the invariant:

- an incremental attendance change after baseline queues recalculation without
  inserting a strict-day row;
- a full-sweep deletion after baseline queues recalculation without inserting a
  strict-day row;
- an already-strict day remains strict after either change because its existing
  marker is not removed;
- a successful strict production save continues to insert the marker atomically.

The first two tests must fail against the current implementation before the
production code changes.

### Production cleanup

Cleanup happens only after the corrected deployment is healthy:

1. Read back the rollout setting and verify it is still `off`.
2. Read all candidate strict rows and verify the expected false dates and
   attendance-change provenance.
3. Check that no candidate date has evidence of a successfully activated live
   rollout. If that cannot be established for a date, leave it unchanged.
4. Delete only the verified false rows from `attendance_strict_days`.
5. Requeue or reset recalculation for those exact dates without marking them
   strict, allowing the normal worker to replace production through the legacy
   matcher.
6. Read back the markers, recalculation state, and Exception Inbox snapshot.

The expected live result is that the disabled Odoo location sections disappear,
the Strict Production warning disappears, and unrelated Inbox items remain.

### Dismantler 3 follow-up

The production mismatch is not hidden or normalized away. Before a future live
rollout, compare the Dismantler 3 adjusted aggregate with the detailed sample
sum at one consistent source boundary and correct the producing source or
snapshot assembly. Strict matching must remain blocked while those values do
not reconcile.

## Failure Handling and Rollback

- If tests or deployment verification fail, do not modify production markers.
- If the production marker candidates differ from the verified set, stop the
  cleanup and report the discrepancy.
- If legacy recalculation fails after cleanup, leave the rollout off, retain the
  failed queue evidence, and report the exact failing date and reason.
- The code change is safe to roll back because it does not remove existing
  markers; however, rolling it back would allow false markers to recur.

## Success Criteria

- Attendance mirror changes enqueue recalculation without creating new strict
  markers.
- Successful strict production storage still creates a permanent marker.
- Focused and full test suites pass, and Ruff is clean.
- The implementation and plain-language changelog entry are pushed to
  `origin/main`.
- Verified false production markers are removed only after deployment.
- Production readback shows rollout `off`, no false strict marker for today, and
  no Odoo-location or Strict Production warning caused by the disabled rollout.
