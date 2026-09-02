# Feedback Owner Task Lifecycle Synchronization

**Date:** 2026-09-02
**Status:** Approved design, awaiting implementation plan

## Goal

Keep every Plant Manager feedback item's authoritative local lifecycle, Odoo
2s Improvement reference, and associated Odoo owner task in the same business
state. Starting work moves both Odoo records to In Progress. Finishing work
moves the Improvement to Completed or Declined and the owner task to Done with
a plain-language result or decline note.

This applies to every feedback type delivered through the owner-task workflow:
bugs, features, questions, and refactors. It also reconciles existing delivered
owner tasks whose stages do not match an established local lifecycle, including
feedback 44 and task 3755.

## Authority and identity

The Plant Manager feedback row remains the lifecycle authority. The Odoo
Improvement and task are synchronized copies; neither can silently reopen or
override a local terminal state.

The Improvement identity remains the exact pair `x_studio_source = "GPI Plant
Manager"` and `x_studio_source_id = "GPI-PM-FB-<positive feedback id>"`.

The task identity comes only from the durable `feedback_task_delivery`
relationship plus readback of the exact Plant Manager project and
identifier-bearing task name. A pasted Task ID is never authoritative. Missing,
duplicate, or mismatched tasks stop safely. Lifecycle synchronization never
guesses, creates a replacement task, or mutates an unrelated task.

## State mapping

| Local feedback | Odoo Improvement | Odoo task stage | Odoo task Status (`state`) | Task note |
|---|---|---|---|---|
| `requested` | Requested | New | any open Status | None |
| `in_progress` | In-Progress | In Progress | any open Status | None |
| `completed` | Completed | Done | `1_done` (Done) | Required resolution note |
| `declined` | Declined | Done | `1_canceled` (Cancelled) | Required note labeled as declined |

The task's Status is written alongside the stage because Odoo never changes
`state` when a task moves stages, and Sales Manager and OS Manager decide
open-versus-closed from `state`. A terminal feedback requires the exact closed
Status; an open feedback only requires an open one, so a Waiting or Approved
Status set from another app is left alone. Readback verifies the Status too.

Task stages are resolved within the task's project by exact configured names.
The worker accepts exactly one destination stage and verifies the task project,
name, active state, and current stage before writing.

## Chosen architecture

Extend the existing durable owner-task delivery queue into a delivery-and-
lifecycle queue. Keep it separate from the guarded Improvements worker because
the two workers target different Odoo models and safety contracts.

Each local lifecycle transition uses one database transaction to validate and
update the local feedback, advance its Improvements projection intent, and
advance the owner task's desired lifecycle version and state. The transaction
never waits for Odoo.

The task worker claims due work with its existing lease, ensures the exact task
has been delivered, then reconciles its stage and terminal note. Temporary Odoo
failures retry. Identity, stage, payload, and readback ambiguity enter a safe,
owner-visible attention state without changing the task.

## Owner-task synchronization data

Extend `feedback_task_delivery` rather than create a competing task queue. Store
the desired lifecycle version/state, last verified lifecycle version, retry and
safe attention state, and immutable dispatch evidence. Evidence identifies the
task, expected project/name, destination stage, and deterministic terminal-note
marker, with dispatch, readback, and settlement timestamps.

Submission still atomically queues initial task creation. Initial delivery
settles the Requested/New lifecycle version after verifying the task. Later
lifecycle transitions reuse that durable relationship.

## Start flow

`process_feedback` continues to accept only a canonical feedback ID or
`GPI-PM-FB-<id>`. Its dry run reports both proposed targets. With `--yes`, one
local transaction moves Requested to In Progress and queues both Odoo copies.

The task worker loads the exact snapshot, validates the stored relationship and
task identity, resolves exactly one In Progress stage, writes only the stage if
needed, reads the task back, and settles the version only on a match. An already
In Progress task is an idempotent success after readback.

## Finish and decline flow

`resolve_feedback` remains gated on an In Progress local state and requires an
authenticated actor and nonblank note. Its transaction moves the feedback to
Completed and queues both Odoo targets. The task worker moves the exact task to
Done and posts the result note once with a deterministic marker derived from
the feedback ID and lifecycle version.

A Declined admin transition also uses Done. Its task message is clearly labeled
`Declined` and contains the required reason. After a timeout, the worker searches
for the marker before posting, preventing duplicate notes.

The task version settles only after readback proves the Done stage and marked
note. The Improvement continues to settle only after its existing full-field
readback. The UI and commands must not claim full synchronization while either
copy remains unverified.

## Existing mismatch reconciliation

A bounded reconciliation migration initializes task-lifecycle intent for every
delivered task from authoritative local feedback state. It performs no Odoo
write itself; the normal worker applies the same safeguards used for new work.

For feedback 44, the worker adopts its stored relationship to task 3755, moves
that exact task to Done, adds the existing resolution note once, and verifies
the result. Unmigrated legacy feedback without local lifecycle authority stays
outside reconciliation. Rows without exactly one delivered task relationship
surface as incomplete rather than manufacturing a relationship.

## Failure and ordering rules

- Local lifecycle transitions remain durable during Odoo outages.
- The two workers may finish in either order; full synchronization requires
  both to verify the same local lifecycle version.
- Task delivery finishes before any later stage mutation.
- Unknown write outcomes recover through readback before another write.
- Missing or ambiguous tasks/stages and readback mismatches enter attention.
- Terminal local feedback is never reopened to repair a task.
- The workflow never creates, deletes, archives, or merges Improvement rows.

## Operator visibility

The admin page shows separate Improvement and owner-task sync status. A terminal
card is not fully synchronized until both desired versions are verified. Safe
attention text identifies which copy needs review without exposing credentials
or raw Odoo data.

Lifecycle command JSON adds only safe task fields: current task sync state,
proposed stage label, and whether the task update was queued.

## Testing and production verification

Automated tests cover atomic dual intent, all feedback types, each mapping,
one-note idempotency, exact task identity, missing/duplicate/malformed targets,
timeout recovery, readback settlement, either worker finishing first, bounded
existing-row reconciliation, and regression coverage for both existing Odoo
integrations and legacy migration.

Production verification reads back the same Improvement and task. A completed
item requires Improvement Status `Completed`, task stage `Done`, and the marked
result note before handoff can claim success. Feedback 44/task 3755 must pass
that generalized path.
