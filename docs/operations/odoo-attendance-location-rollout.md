# Odoo Attendance Location Rollout

Use this runbook when Odoo work-center attendance is ready to become the source
for production credit. Do not skip a stage, turn on live mode during a shift, or
change the rollout setting directly in the database.

## Before you begin

- Use a super-admin account in Plant Manager.
- Know the next local workday's configured start time.
- Keep the rollout in **Off** until the deployed app, database schema, and
  attendance mirror worker are healthy.
- A readiness result is evidence for one moment. It does not replace the
  boundary recheck that Plant Manager performs when live mode starts.

## 1. Deploy with the rollout off

Deploy the release through the normal production process while **Settings →
Attendance locations → Rollout mode** is **Off**. Confirm the app starts without
schema errors and the attendance mirror worker is running. Do not schedule live
mode yet.

## 2. Wait for complete source history

Wait until Settings shows both a completed attendance baseline and a completed
full sweep. Confirm the mirror has no sync error, open attendance rows were
refreshed, and the displayed freshness is within the allowed limit.

If either the baseline or full sweep is incomplete, stop here. Do not treat a
partial import as safe.

## 3. Observe one complete production day in shadow mode

Set the rollout mode to **Shadow**. Leave it there for at least one complete
production day, from the configured local workday start through the end of
production. Shadow mode compares answers but does not let the new match control
production credit.

Plant Manager records when Shadow starts. Older comparisons and a day that
started before that time do not count, even if their data is still saved.

After the day closes, confirm the shadow comparison completed. Review changed
worker units and unassigned units before continuing.

## 4. Clear problems and prove one correction

Resolve every work-center mapping and location conflict that affects
production. During a non-production test interval, complete one manager
attendance correction from preview through verified Odoo readback and finished
production recalculation. Confirm department repairs have no failed jobs.

Do not continue while a correction, repair, or recalculation is failed or
stuck.

## 5. Run the read-only readiness check

From the deployed application environment, run:

```bash
uv run python scripts/check_attendance_location_readiness.py
```

The command prints one JSON readiness report. Exit code `0` means every current
gate is ready. Exit code `1` means `ready` is false; read the `blockers` list,
fix those conditions, and run the same command again. The command reads current
state only: it does not write to Odoo, change rollout mode, or create database
schema.

Continue only after the command exits `0`.

## 6. Schedule the live boundary

In Settings, schedule **Live** for the next local workday's exact configured
start time, before production begins. Never choose a time inside an active
shift. Plant Manager saves the schedule only after a fresh readiness check and
checks everything again at the boundary.

At the boundary:

- a successful recheck activates live mode, marks that workday strict, and
  queues its first strict production rebuild;
- a failed recheck returns the rollout to Shadow, leaves the prior matcher in
  control, and creates one urgent cutover-blocked item with the reasons.

If the boundary is blocked, resolve the listed reasons and schedule a new clean
workday boundary. Do not force the saved setting.

## 7. Monitor the live start

Watch these Settings values closely during the first live workday:

- mirror age and last completed full sweep;
- open rows not refreshed;
- unassigned units and their oldest age;
- recalculation queue depth and age;
- location conflicts, missing locations, and unmapped locations;
- correction failures, retries, and verification failures;
- department repair failures.

Stop scheduling additional rollout changes if any source becomes stale or a
queue remains stuck. Use the Exception Inbox to resolve the source problem; do
not assign production by guessing.

## 8. Roll back only at a clean boundary

If rollback is needed, schedule **Shadow** for the next clean local workday
boundary. The rollback changes the matcher for new data only. Keep all of the
following intact:

- the attendance mirror;
- days already marked strict;
- Odoo source records;
- correction, repair, recalculation, and rollout audit history.

Never delete or rewrite those records to make a rollback look clean. A later
Odoo change to an already-strict day must still recalculate that day under the
strict rules.

Plant Manager appends each Shadow, schedule, activation, blocked start, and
rollback transition to its rollout audit. Operators should not edit that audit.
