# Feedback Task Contract Versioning

**Date:** 2026-09-02
**Status:** Approved design, implementation planned

## Problem

Plant Manager feedback 43 is complete, and its Odoo task 3656 is in the Done
stage, but the task's separate Odoo Status is still In Progress. The local
`feedback_task_delivery` row reports the task as synchronized because its
feedback lifecycle version was settled before task synchronization learned to
write and verify Odoo's separate `project.task.state` field.

The existing queue can detect a newer feedback lifecycle version. It cannot
detect that the task synchronization contract itself changed. A worker upgrade
can therefore leave older tasks materially incomplete while local state still
says they are synchronized.

## Goal

Make task synchronization version both the feedback lifecycle and the worker's
task contract. A completed Plant Manager feedback item is not fully synchronized
until the exact Odoo Improvement and its durably associated Odoo task have both
been read back in their required terminal states.

For feedback 43, the generalized repair must update only its stored task 3656,
verify the Done stage and Done Status, and leave the Improvement row unchanged.

## Chosen design

Add an independent integer contract version to the durable owner-task queue.
The application owns one current task-contract constant. New delivery rows use
that version. Existing rows keep their last verified contract version, so an
application upgrade can identify every task whose Odoo state has not been
verified under the new rules.

`feedback_task_delivery` stores:

- `desired_contract_version`: the task contract the worker must apply;
- `last_synced_contract_version`: the task contract last verified by Odoo
  readback.

These fields are separate from `desired_version` and `last_synced_version`,
which continue to describe the feedback lifecycle projection. The queue is
synchronized only when both version pairs match their current targets.

The current contract version is `2`, representing task stage, task Status, and
terminal-note verification. Existing rows start below version 2 and are
therefore eligible for reconciliation after deployment. Future task contract
changes must increment the constant and add or update tests that prove old rows
are requeued.

## Queue and worker behavior

The bounded lifecycle reconciler queues a durable task when either:

- its feedback lifecycle intent or verified lifecycle version differs from the
  authoritative feedback row; or
- its desired or verified contract version is older than the application's
  current task contract.

The reconciler performs no Odoo mutation. It only makes the existing durable
relationship due. It does not create, guess, replace, archive, or merge tasks.
Blocked identity relationships remain blocked for human review.

A worker claim carries both lifecycle and contract versions. Before settlement,
the worker follows the existing safeguards: it verifies the exact stored task,
project, active state, identifier-bearing name, destination stage, task Status,
and deterministic terminal-note marker. Settlement advances both verified
versions only after fresh Odoo readback proves every required field. A claim
made under an older contract cannot settle a newer contract version.

Temporary failures retry. Missing, duplicate, inactive, renamed, or otherwise
mismatched tasks stop safely without touching another task.

## Completion reporting

The feedback lifecycle command and admin UI must not report `synced` merely
because the feedback lifecycle versions match. They require:

- the Improvement projection version to be readback-verified;
- the task lifecycle version to be readback-verified; and
- the task contract version to equal the application's current version and be
  readback-verified.

Repository completion instructions also require a final readback of the exact
durably associated task. Completed feedback requires task stage `Done` and task
Status `Done`; declined feedback requires task stage `Done` and task Status
`Cancelled`. The final handoff states both the Improvement and task results.

## Feedback 43 repair

After the implementation is pushed and deployed, run the bounded reconciliation
path. It must discover feedback 43 from its local lifecycle and stored task
relationship, not from the pasted Task ID. The normal worker then updates only
task 3656's Status from In Progress to Done, preserves its existing Done stage
and terminal note, and settles contract version 2 after readback.

Read back both exact records:

- Improvement source ID `GPI-PM-FB-43` has Status `Completed`;
- the locally associated task ID is 3656, its stage is `Done`, and its Status is
  `Done`.

If any identity, write, deployment, worker, or readback check fails, leave the
feedback terminal, do not manufacture another relationship, and report the task
as incomplete.

## Testing

Test-first coverage must prove:

- new delivery rows use the current task contract version;
- a locally settled lifecycle row with an older verified contract is queued;
- a current lifecycle and current contract are not queued again;
- blocked relationships remain untouched;
- claims and renewals preserve both contract versions;
- settlement advances the verified contract only after successful Odoo
  readback;
- a stale claim cannot settle a newer desired contract;
- lifecycle command and admin status report pending until both version pairs
  match;
- schema migration is idempotent for fresh and existing databases; and
- feedback 43's observed shape—Done stage with In Progress Status—is repaired by
  the generalized worker path.

Focused tests cover schema, task delivery, lifecycle commands, admin status,
and task worker behavior. The full available test suite and Ruff must pass
before pushing implementation.
