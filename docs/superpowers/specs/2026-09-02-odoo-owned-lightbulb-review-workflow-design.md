# Odoo-Owned Light-Bulb Review Workflow Design

> **Superseded on 2026-09-02. Do not implement this design.** Dale chose the
> simpler app-owned workflow in
> `2026-09-02-simple-lightbulb-task-sync-design.md`. That replacement uses no
> custom code, Studio automation, or webhook in Odoo.

**Date:** 2026-09-02
**Status:** Superseded; do not implement

## Goal

Give the Plant Manager light-bulb menu six clear choices, route Repair directly
to Maintenance, and route every other submission through Odoo so the 2s
Improvement reference table and its one linked task stay synchronized in Plant
Manager, Sales Manager, and OS Manager.

Odoo owns the workflow actions and work state. The apps are entry points and
views over the same Odoo records; they do not implement competing copies of the
review rules.

## Approved choices and routing

The chooser follows the two-section layout in the supplied reference:

### Reporting — the 2s board triages it

| Button | Input | Odoo Type | One work item |
| --- | --- | --- | --- |
| Bug | Description and optional screenshot | `Digital` | Coding task assigned to Dale |
| New Feature | Description and optional screenshot | `Digital - New Feature` | Coding task assigned to Dale |
| Floor Issue | Description and optional screenshot | `Physical - Issue` | Review task assigned to Dale |
| Floor Suggestion | Description and optional screenshot | `Physical - Suggestion` | Review task assigned to Dale |

### Ready to create work

| Button | Input | Result |
| --- | --- | --- |
| Repair | None in Plant Manager | Open `https://www.gpimaintenance.com/request` |
| 2s Improvement | Description and optional screenshot | Create an Odoo row with the distinct Type `2s Improvement` and one review task assigned to Dale |

Repair creates no Plant Manager feedback row, no 2s Improvement reference row,
and no review task. The Maintenance site owns everything after the link opens.
The canonical link includes `www`; the apex `/request` address currently returns
404.

Floor Issue, Floor Suggestion, and 2s Improvement never create a maintenance
work order. Their linked review task is their only work item. Their
`x_studio_linked_wo` value remains empty.

## Submitter experience

Private Plant Manager screens use the signed-in employee automatically. The
timeclock requires the employee to choose **Who is submitting this?** before the
form can be sent. The selected person must resolve to one exact active Odoo
employee and is stored in `x_studio_submitted_by`.

If the employee cannot be resolved exactly, the app does not submit an
anonymous or guessed record. It explains that a manager must fix the employee
match. Review submissions do not ask for equipment or area. The description is
required and one screenshot remains optional.

## Odoo authority and identity

The existing Odoo model `x_2s_improvements` is the authoritative reference
table. Each submission has one exact compound identity:

- `x_studio_source` identifies the submitting app.
- `x_studio_source_id` identifies the submission within that app.

For Plant Manager, the identity remains `GPI Plant Manager` plus
`GPI-PM-FB-<positive id>`. A task title or Odoo task ID is never a substitute
for this identity.

The authoritative work relationship is `x_studio_linked_task`, a relation from
the reference row to exactly one `project.task`. Task names are display text,
not identity. Odoo rejects or surfaces any attempt to create a second task for
an already-linked row.

The table keeps its existing status values:

- `Requested`
- `In-Progress`
- `Completed`
- `Declined`

Add `2s Improvement` as an exact, distinct stored and displayed option in
`x_studio_type`. It is not an alias for `Physical - Suggestion`.

## Odoo-created tasks

Apps create or locate the authoritative reference row. Odoo automation creates
and links the task when `x_studio_linked_task` is empty. This removes separate
app-owned task creation paths and makes retries idempotent.

Odoo routes tasks as follows:

| Odoo Type | Project | Initial stage | Initial assignee | Workflow |
| --- | --- | --- | --- | --- |
| `Digital` | Plant Manager | New | Dale | Coding |
| `Digital - New Feature` | Plant Manager | New | Dale | Coding |
| `Physical - Issue` | GPI OS Manager - TASKS | General | Dale | Review |
| `Physical - Suggestion` | GPI OS Manager - TASKS | General | Dale | Review |
| `2s Improvement` | GPI OS Manager - TASKS | General | Dale | Review |

The automation first checks the exact reference identity and existing linked
task. A retry adopts the already-linked task after readback. It never creates a
replacement because a title, project, assignee, or stage changed later.

## Review lifecycle

A new physical review row starts as `Requested`. Its linked task is open in the
General stage and assigned to Dale. While Requested, the apps show only the
review decision actions:

- **Accept**
- **Decline**

After acceptance, the apps show the work actions:

- **Assign**
- **Complete**
- **Move to L10**

The state contract is:

| Odoo action | Task result | Reference result | Required detail |
| --- | --- | --- | --- |
| Accept | Status `03_approved`; remains in General | `In-Progress` | Acting employee recorded in Odoo history |
| Decline | Status `1_canceled` | `Declined` | Nonblank decline reason; stop date and completing employee |
| Assign | Odoo assignee changes | Remains `In-Progress` | Exact active Odoo user/employee |
| Complete | Status `1_done` | `Completed` | Nonblank result; stop date and completing employee |
| Move to L10 | Stage becomes `L10` in GPI OS Manager - TASKS | Remains `In-Progress` | Existing assignee retained unless explicitly changed |

Accepting is the only transition from Requested to In-Progress. Assignment,
completion, and L10 routing are unavailable until acceptance. Declining is
terminal. Completing is terminal. A terminal item is never silently reopened.

Task chatter keeps the acceptance, decline, assignment, completion, and L10
history, including the acting employee and any required reason/result. The
reference row keeps the original submission notes and picture; a lifecycle
action must not overwrite the request text.

## Odoo-owned actions

### 2026-09-02 transport decision

Use Odoo 19's native Studio `/web/hook/<uuid>` endpoint without a custom Odoo
controller or module. HTTP 200 with exact JSON `{"status":"ok"}` is only an
acknowledgement that the Execute Code action returned without error. Immediately
afterward, the calling application's server reads the exact reference identity
and linked task through authenticated JSON-RPC, constructs the typed action
result locally, and validates the requested transition before showing success.
After a timeout or unknown outcome, it performs that exact readback before any
retry and accepts the result only when the requested transition demonstrably
landed.

Odoo implements the five review operations once. Sales Manager and OS Manager
invoke those same operations by authoritative task/reference identity and then
read both records back. An app must not claim success from its request alone.

The Odoo action layer is responsible for:

1. Authenticating and authorizing the acting employee.
2. Resolving exactly one linked reference row and task.
3. Confirming that the current reference state permits the requested action.
4. Applying the task and reference changes as one business operation.
5. Recording the action in Odoo history.
6. Returning without error so the native endpoint can acknowledge the transaction.

The calling application's server owns the immediate authenticated readback,
constructs the typed final state, and rejects any result that does not prove the
requested transition.

An app may use its own server integration credentials for transport, but it
must send the authenticated employee identity. Odoo validates that identity;
the app cannot invent a completion or review actor.

## Cross-app visibility and synchronization

OS Manager naturally reads the review tasks from `GPI OS Manager - TASKS`.
Sales Manager also includes linked review tasks assigned to the current user,
even though their project is owned by OS Manager. Both apps recognize a review
task through its exact `x_studio_linked_task` relationship, not through title
text.

Both task surfaces use the same control rules:

- Requested: Accept or Decline.
- In-Progress: Assign, Complete, or Move to L10.
- Completed or Declined: read-only terminal result.

After every mutation, the acting app reads the task and reference row back.
The other apps learn the change through their normal Odoo refresh. No app
keeps a private status that can overwrite a newer Odoo state.

Plant Manager keeps its durable local submission and retry queue so a timeclock
submission survives an outage. Local data is authoritative only for the
not-yet-delivered request payload and source identity. Once Odoo has created and
linked the row, Odoo is authoritative for lifecycle status. Plant Manager
adopts Odoo status inbound and must never replay a stale local Requested value
over an accepted, declined, or completed row.

Digital coding tasks remain visible in their existing task surfaces. Their open,
done, and cancelled task states update the same reference statuses through the
Odoo lifecycle mapping, without showing the physical review Accept button.

## Implementation ownership

- Odoo owns reference-row identity, task creation/linking, lifecycle
  transitions, authorization, audit history, and final state.
- Plant Manager owns the six-button chooser, durable submission queue,
  timeclock employee picker, Repair link, and Odoo readback shown to the
  submitter.
- Sales Manager owns only its presentation of linked Odoo review tasks and its
  adapter for invoking the Odoo actions.
- OS Manager owns only its presentation of linked Odoo review tasks, its action
  adapter, and its normal General/L10 task views.

The apps may share a versioned contract fixture for field names, stored values,
and action results, but none may copy the transition rules and become a second
lifecycle authority. The exact Odoo configuration and identifiers must be
captured in reproducible deployment code or an audited configuration record;
they must not exist only as undocumented Studio clicks.

## Failure behavior

- Odoo or network unavailable: keep the local submission queued and clearly
  show that delivery is pending.
- Unknown result after a timeout: read by exact source identity before retrying;
  do not create another row or task.
- Missing or duplicate reference/task relationship: stop safely and show an
  operator-visible attention state.
- Invalid state transition: return the current authoritative state without
  changing either record.
- Inactive or ambiguous assignee/actor: refuse the action without guessing.
- Partial task/reference result: quarantine the operation for reconciliation;
  do not report success.
- Concurrent action: use Odoo's current record state as the winner and return a
  conflict so the app refreshes.
- Repair popup blocked: keep the chooser open and show a normal link the person
  can press again.

No workflow creates, deletes, archives, merges, or directly rewrites an
unrelated Odoo improvement row.

## Safe rollout order

The current apps enforce strict Odoo selection contracts, so adding the new
Type without compatibility work could stop existing feedback delivery. Rollout
must be coordinated:

1. Deploy compatibility readers and preflights in Plant Manager, Sales Manager,
   and OS Manager. They accept the known four-type contract or the known
   five-type contract, but do not expose 2s Improvement yet.
2. Configure the Odoo `2s Improvement` selection, task-creation automation, and
   lifecycle actions. Keep UI entry points disabled.
3. Run read-only contract checks from all three apps. Create a disposable test
   row only in the non-production Odoo test environment, verify one linked task,
   and exercise each lifecycle action there.
4. Deploy Odoo-authoritative inbound lifecycle synchronization and shared task
   controls in Sales Manager and OS Manager.
5. Enable the six-button chooser and Repair link in Plant Manager. Enable the
   matching chooser behavior in Sales Manager where applicable.
6. Verify one real submission by exact source identity in each submitting app,
   then verify the same task and reference state in both task apps.
7. Remove the four-type transition path after every production app reports the
   five-type contract.

Each application deploy can be rolled back independently while Odoo remains the
authority. UI entry points stay feature-gated until the Odoo contract and
actions pass readback.

## Validation

Automated and production checks cover:

- the exact six-button order, grouping, labels, and responsive layout;
- the canonical Repair URL and proof that Repair creates no local/Odoo row;
- required timeclock submitter and exact employee mapping;
- all five feedback-to-Odoo Type mappings, including exact `2s Improvement`;
- one reference row and one linked task under retry and timeout conditions;
- correct project, initial stage, and Dale assignment for each task kind;
- Requested gating and all five Odoo review actions;
- required decline reason and completion result;
- exact task/reference readback after every action;
- Sales Manager and OS Manager rendering the same state and allowed actions;
- stale local status never overwriting a newer Odoo lifecycle;
- concurrent actions, unauthorized actors, invalid assignees, missing stages,
  duplicate relationships, and partial failures;
- light and dark appearance plus phone, tablet, and desktop layouts;
- existing Bug and New Feature submission and coding-task behavior.

Production validation reads the same reference row and task by authoritative
identity from both task apps. A lifecycle change is complete only when Odoo and
both app readbacks agree.

## Superseded decisions

This design supersedes the earlier decisions that Repair was out of scope, that
Plant Manager's local feedback lifecycle remained authoritative after Odoo
delivery, and that apps independently owned task lifecycle changes. Existing
local safeguards remain in force until this design is implemented, validated,
and deployed.

## Out of scope

- Creating a work order for Floor Issue, Floor Suggestion, or 2s Improvement.
- Prefilling the external Maintenance request page; its current page does not
  consume URL query parameters.
- Adding an equipment or area field to the review submission form.
- Replacing the Maintenance application's own request and work-order workflow.
- Guessing, merging, deleting, or rewriting existing Odoo reference rows.
