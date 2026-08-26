# Feedback delivery to the app owner's Odoo task list

**Date:** 2026-08-26
**Status:** Approved design, pending implementation plan

## Summary

Each newly submitted Plant Manager bug report or feature request will create
exactly one task in Odoo's **Plant Manager** project. The task will always be
assigned to the app owner: the Odoo user authenticated through `ODOO_LOGIN`.

The submission remains local-first. Plant Manager saves the report and a
durable task-delivery request in one database transaction, then a background
worker delivers the task. A temporary Odoo failure never loses a report or
asks its submitter to try again. The existing guarded mirror into the shared
Odoo Improvements list stays independent and unchanged.

This applies to reports submitted after the feature is deployed. Existing
local feedback is not backfilled into tasks.

## Goals

- Create one Odoo `project.task` for every new bug or feature request.
- Assign every such task to the authenticated `ODOO_LOGIN` user, who is the
  app owner.
- Preserve the current local feedback record, lifecycle, optional screenshot,
  and shared-Improvements mirror.
- Deliver tasks automatically after transient Odoo or network failures.
- Prevent duplicate tasks even if a worker crashes or an Odoo response times
  out after creating the task.
- Let the app owner see task-delivery failures without exposing credentials or
  raw Odoo errors to submitters.

## Non-goals

- Reassigning reports to their submitters or to a per-category owner.
- Backfilling historical feedback into Odoo tasks.
- Synchronizing local feedback lifecycle changes back to the task's stage.
- Changing the shared Improvements mirror's gates, target, or safety rules.
- Sending email or Slack notifications in addition to the Odoo task.

## Chosen approach

The app will use a dedicated durable task-delivery queue.

Creating a task inline in `POST /feedback` would provide immediate feedback,
but it would either lose a report or force the submitter to retry when Odoo is
unavailable. Reusing `feedback_odoo_sync` is not viable because that worker is
intentionally constrained to `x_2s_improvements` and its safety contract does
not allow project-task writes or owner assignment.

The task-delivery queue keeps the request path local and fast, retries safely,
and makes delivery state inspectable. It uses the existing general Odoo client
and project-task helpers, not the dedicated Improvements client.

## Submission and delivery flow

1. `POST /feedback` validates and normalizes the report and optional screenshot
   as it does today.
2. `feedback_store.create_submission()` inserts the local feedback row, image,
   existing Improvements-mirror intent, and a task-delivery row in one
   transaction.
3. The response confirms that the feedback was received and queued for the app
   owner. It never claims the Odoo task already exists.
4. A dedicated 60-second background tick claims due task deliveries with a
   lease, so concurrent app processes cannot work on the same report.
5. The worker authenticates with the normal `ODOO_LOGIN` credentials, resolves
   the **Plant Manager** project and either the **Bug** or **Feature request**
   tag, then creates or adopts the uniquely identified task.
6. The worker saves the task ID and marks delivery complete. It retries
   temporary failures with bounded backoff. A safe delivery error remains
   visible to the app owner until a later retry succeeds.
7. The existing Improvements worker continues to mirror the same local
   feedback independently when its own exact write gates are enabled.

## Odoo task contract

Each task will have:

- Project: **Plant Manager** (find or create using the current helper).
- Assignee: the Odoo user ID returned by `odoo_client.authenticate()` for
  `ODOO_LOGIN`.
- Deadline: the server's current local date.
- Tag: **Bug** for bugs or **Feature request** for feature requests.
- Name: an immutable, unique identifier followed by the readable summary, for
  example `[GPI-PM-FB-482] [Bug] Scheduler save button does nothing`.
- Description: the full escaped report text, submitter name and UPN when
  available, source page URL, and the local feedback identifier.
- Screenshot: the normalized JPEG already saved locally, attached with an
  identifier-derived filename such as `GPI-PM-FB-482-before.jpg`.

The unique identifier is the idempotency key. Before creating a task, and
again after an uncertain result, the worker looks up the exact task identity
within the Plant Manager project, including archived tasks. Zero results allow
creation; one result is adopted; multiple results stop the delivery for owner
review rather than create another task.

Attachments follow the same rule: the worker records successful attachment
delivery and uses the identifier-derived attachment name to adopt a matching
attachment after an uncertain response. A retry therefore does not attach the
same screenshot repeatedly.

## Data model and boundaries

Add a `feedback_task_delivery` table owned only by the task-delivery worker.
It contains one row per feedback ID, delivery state, next-attempt time, lease
metadata, retry count, safe last-error text, Odoo task ID, and attachment
delivery state. The feedback ID is its primary key and foreign key.

`feedback_store` owns atomic submission creation and read models for the
owner's feedback page. A new focused `feedback_task_delivery` module owns
claiming, leasing, retry scheduling, completion, and safe failure reporting.
A `feedback_task_worker` module owns the Odoo interaction and does not mutate
the Improvements queue.

The pre-existing `odoo_task_id` column remains reserved for historical
task-backed feedback. New delivery data uses the dedicated table so historical
migration and the shared Improvements mirror retain their current contracts.

## User experience and owner visibility

Submitters continue to receive a successful result once Plant Manager saves
their report. The feedback UI will describe it as sent to the app owner rather
than imply instant Odoo completion.

The existing super-admin feedback view gains a concise delivery indicator:
**Queued**, **Assigned to owner**, or **Needs attention**. Needs-attention
states include a safe explanatory message but never credentials, raw request
data, or remote diagnostics. This is an owner-facing operational status; it
does not change the feedback lifecycle controls.

## Failure handling

- Odoo unavailable, timeout, or temporary transport failure: retain the local
  report and retry later.
- Authentication, project, permission, or response-contract failure: retain
  the report, display a safe owner-facing status, and retry on the normal
  schedule after configuration is corrected.
- Worker crash or expired lease: another worker reclaims the delivery after
  the lease expires.
- Create or attachment response lost: use the exact idempotency lookup before
  retrying a write.
- More than one matching remote task or attachment: stop that delivery for
  owner review; do not guess or create another remote record.

## Testing

- Submission atomically creates the feedback record, Improvements intent, and
  queued task delivery.
- Worker creates the correct project task, owner assignment, tag, deadline,
  escaped description, and screenshot attachment.
- Assignment comes from the configured Odoo authentication user, never the
  feedback submitter.
- A normal Odoo outage leaves the submission successful and schedules a retry.
- Lease recovery and uncertain create/attachment results adopt the one matching
  remote record without duplication.
- Ambiguous remote matches surface a safe Needs-attention status and make no
  further write.
- Existing Improvements-mirror tests continue to prove its gates and target
  are unchanged.
- The admin feedback page presents queued, delivered, and needs-attention
  states only to super-admins.
