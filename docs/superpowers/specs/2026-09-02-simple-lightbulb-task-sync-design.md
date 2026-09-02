# Simple Light-Bulb Task Sync Design

**Date:** 2026-09-02  
**Status:** Approved in conversation; ready for implementation planning
**Supersedes:** `2026-09-02-odoo-owned-lightbulb-review-workflow-design.md`

## Goal

Make all six Plant Manager light-bulb choices work while keeping one Odoo task
as the only work item for each review request. The same task is visible and
actionable in Sales Manager and OS Manager. Plant Manager updates the matching
2s Improvement reference row in the background, normally within one minute.

The workflow must not require a duplicate Odoo database, a custom Odoo module,
Studio Execute Code, an Odoo automation script, or a special webhook.

## Button routing

The chooser uses the two sections and exact labels from Dale's reference.

### Reporting — the 2s board triages it

| Button | Required input | Stored type | Result |
| --- | --- | --- | --- |
| Bug | Description; screenshot optional | `Digital` | Existing coding-task flow |
| New Feature | Description; screenshot optional | `Digital - New Feature` | Existing coding-task flow |
| Floor Issue | Description; screenshot optional | `Physical - Issue` | One review task |
| Floor Suggestion | Description; screenshot optional | `Physical - Suggestion` | One review task |

### Ready to create work — straight to the floor team

| Button | Required input | Result |
| --- | --- | --- |
| Repair | None in Plant Manager | Open `https://www.gpimaintenance.com/request` |
| 2s Improvement | Description; screenshot optional | One review task with stored type `2s Improvement` |

Repair creates no Plant feedback record, no reference row, and no task. If the
browser blocks the new tab, the chooser stays open and shows the same normal
link for the person to press.

Floor Issue, Floor Suggestion, and 2s Improvement create no work order. Their
one Odoo review task is the only work item. The reference row is a synchronized
record, not another item someone must work.

## Submitter identity

Private Plant Manager pages use the signed-in person's exact identity. The
timeclock form asks **Who is submitting this?** and lists only active employees
with one valid positive Odoo employee ID. The server rejects missing, inactive,
unknown, or duplicate employee matches instead of guessing.

Review submissions require a description and allow one screenshot. They do not
ask for equipment or area.

## Standard Odoo records only

Odoo stores ordinary records and runs no custom workflow code for this feature:

- no custom Odoo module;
- no Studio Execute Code action;
- no automated/server action containing Python;
- no custom review webhook;
- no Odoo-side transition logic.

The apps use their existing authenticated Odoo APIs to create, read, and update
the task and reference row. If the reference table's Type selection does not
already contain the exact `2s Improvement` choice, an administrator may add
that one choice through the normal no-code field settings. No executable code
is added to Odoo.

## Identity and ownership

Every Plant submission retains the exact source identity:

- Source: `GPI Plant Manager`
- Source ID: `GPI-PM-FB-<positive local feedback id>`

Plant Manager's durable local queue owns only delivery and retry bookkeeping.
The Odoo task is the human-facing work item. The Odoo
`x_2s_improvements` row is the shared reference record.

For Plant-sourced review requests, Plant Manager is the only app allowed to:

1. create or adopt the exact reference row;
2. create or adopt the exact review task;
3. set the reference row's linked task once when it is empty; and
4. project the task lifecycle back to that exact reference row.

Sales Manager and OS Manager update the task only. They never create a second
reference row, replace the linked task, or write Plant-owned reference fields.

The versioned cross-app contract advances to an app-owned-link variant. It
allows only Plant Manager to write `x_studio_linked_task`, and only as a
compare-and-set from empty to the exact adopted task ID. It never permits an
overwrite. `x_studio_linked_wo` remains empty for all three review types.

## Creation and linking

Plant Manager submits locally first so a timeclock network interruption cannot
lose the request. Its existing workers then reconcile by exact source identity:

1. Create or adopt one reference row.
2. Create or adopt one task in `GPI OS Manager - TASKS`, General stage,
   assigned to Dale.
3. Read both records back.
4. If the reference link is empty, set it to the exact task ID.
5. If the link already equals that task, treat the retry as successful.
6. If duplicates or a conflicting link exist, stop and mark the local delivery
   as needing attention. Never choose one or overwrite the conflict.

Bug and New Feature retain their existing Plant Manager coding-task behavior.
Only the three review types use the review project and lifecycle below.

## Review actions

A new review task starts in General and is assigned to Dale. The task's verified
state drives the available controls immediately; the reference status may lag
behind for up to one background-sync interval. Before acceptance, Sales Manager
and OS Manager show only:

- **Accept**
- **Decline**

After acceptance, both apps show:

- **Assign**
- **Complete**
- **Move to L10**

The task changes are standard Odoo task writes:

| Action | Task result | Required input |
| --- | --- | --- |
| Accept | Status `03_approved`; remain in General | None |
| Decline | Status `1_canceled` | Nonblank reason |
| Assign | Change to one exact active Odoo assignee | Assignee |
| Complete | Status `1_done` | Nonblank result |
| Move to L10 | Stage `L10`; retain assignee and approved status | None |

Each action is one atomic write to the task. The same write appends a small,
human-readable, versioned review event to the task description. The block has
exact labels for `Event ID`, `Action`, `Actor Odoo user ID`, `Actor employee ID`,
`Time UTC`, and `Detail`; an Assign event also has `Target Odoo user ID`. Detail
is required for Decline and Complete. Dynamic text is escaped before it becomes
Odoo HTML. Plant Manager parses only complete versioned blocks with allowed
actions and valid typed values.

Existing task description text and prior events are preserved. The writer
re-reads and checks the task's latest update time before writing so a concurrent
action becomes a visible conflict instead of silently replacing newer text.

The event is data in a normal task description, not executable Odoo code.

## Background reference synchronization

Plant Manager's existing background worker polls the exact linked task and
projects its verified state to the exact Plant-owned reference row. A normal
update should appear in the reference table within one minute.

| Verified task state | Reference state |
| --- | --- |
| New, no Accept event | `Requested` |
| Approved, with matching Accept event | `In-Progress` |
| Approved after Assign or Move to L10 | `In-Progress` |
| Cancelled, with matching Decline reason | `Declined` plus reason, date, and employee |
| Done, with matching Complete result | `Completed` plus result, date, and employee |

The worker never rewrites the original request description, screenshot, source,
source ID, submitted employee, or type. A terminal task without its required
reason/result event is flagged for attention instead of producing incomplete
reference data. Terminal reference rows are never silently reopened.

If reference synchronization fails, the task action remains visible in Odoo
and the worker retries. Sales Manager and OS Manager show **Reference update
pending** until the linked row agrees; they do not report that the reference is
already updated.

## Cross-app behavior

Sales Manager and OS Manager identify review tasks through the exact linked
reference relationship, not title text. Both use the same action contract,
validation messages, state rules, and Odoo readback behavior.

After an action, the acting app immediately reads the task back before showing
success. The other app receives the change through its normal Odoo refresh.
The reference-table status follows through Plant Manager's background worker.

Digital coding tasks keep their existing presentation and Copy for Coding
behavior. They do not show the physical review Accept/Decline controls.

## Failure and concurrency behavior

- Task write fails: show an error and leave the current task state on screen.
- Task write outcome is unknown: read the exact task before retrying.
- Concurrent task update: refresh and explain that the task changed in Odoo.
- Reference update fails: keep retrying and show Reference update pending.
- Missing or duplicate source identity, reference, task, project, stage,
  employee, or assignee: stop safely and show an attention state.
- Conflicting linked task: never overwrite it automatically.
- Invalid or stale action: reject it and refresh the latest task state.
- Completed or declined item: read-only; no silent reopen.

No workflow action deletes, archives, merges, or rewrites an unrelated Odoo
task or improvement row.

## Rollout and verification

The app code is developed and tested without changing Odoo configuration.
Automated coverage includes:

- the six-button grouping, exact labels, keyboard access, and Repair fallback;
- private and timeclock submitter validation;
- exact source identity and idempotent task/reference adoption;
- no work-order creation for review types;
- compare-and-set task linking and duplicate/conflict quarantine;
- all five actions in both task apps;
- required decline reason and completion result;
- task-description event preservation and concurrent-write conflicts;
- one-minute reference projection, retries, terminal safeguards, and pending UI;
- cross-app readback of task state, stage, assignee, and reference status.

The existing feature gate stays off until Plant, Sales, and OS Manager changes
are deployed and their full test suites pass. A final code change enables the
buttons; Dale does not need to configure a webhook, database copy, or custom
Odoo code.

Production verification uses one clearly labeled disposable review submission,
confirms it appears as the same task in Sales and OS Manager, exercises the
approved actions, verifies the reference row catches up, and then completes the
test task normally. It never uses or alters an unrelated reference row.

## Out of scope

- Custom code or automation inside Odoo.
- A second review work item.
- A maintenance work order for Floor Issue, Floor Suggestion, or 2s Improvement.
- Changing Bug or New Feature coding-task behavior.
- Equipment or area fields on review submissions.
- Instant cross-model atomic updates; Dale approved background reconciliation
  within about one minute.
