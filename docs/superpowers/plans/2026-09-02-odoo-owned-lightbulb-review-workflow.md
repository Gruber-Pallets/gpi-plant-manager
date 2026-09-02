# Odoo-Owned Light-Bulb Review Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the exact six light-bulb choices, send Repair only to GPI Maintenance, and make Odoo create and govern one shared review task for Floor Issue, Floor Suggestion, and 2s Improvement across Plant Manager, Sales Manager, and OS Manager.

**Architecture:** `x_2s_improvements` is the source of truth. Plant Manager and Sales Manager durably create one reference row by compound source identity; Odoo automation creates and links the only work item; one Odoo-owned action endpoint enforces review transitions; Sales Manager and OS Manager read that same relationship and call the same actions. Compatibility readers and `ODOO_FEEDBACK_WORKFLOW_V2_ENABLED` ship dark before any new entry point is enabled.

**Tech Stack:** Python 3.11, FastAPI, PostgreSQL, pytest, Ruff, TypeScript, Next.js, React, Drizzle, Vitest, Odoo 19 JSON-RPC/Studio automation, Odoo external webhooks

## Global Constraints

- Preserve all pre-existing dirty files in all three repositories. In particular, inspect and integrate the current uncommitted `gpi-sales-manager/src/lib/feedback/catalog.ts`; never reset, replace, or silently commit unrelated work.
- Work in dependency order. Odoo compatibility and the duplicate-database action contract must pass before enabling submission or review controls.
- Repair is an external link to `https://www.gpimaintenance.com/request`. It creates no local submission, Odoo improvement, task, or work order in these apps.
- Floor Issue, Floor Suggestion, and 2s Improvement create one review task only. `x_studio_linked_wo` stays empty.
- The exact label and stored Odoo selection are `2s Improvement`.
- Odoo alone owns task creation, the reference/task link, transition validation, task state, reference status, completion identity/date, and chatter. Apps submit requests, invoke actions, and read the result back.
- Preserve the original request in `x_studio_notes`; decline and completion text go to chatter and dedicated completion data, never over the original request.
- Use exact identities, never task titles: source + source ID + `x_studio_linked_task`.
- Do not create, delete, archive, merge, or rewrite unrelated Odoo improvement rows.
- Never expose the Odoo webhook URL or secret to browser code, logs, error messages, screenshots, fixtures, or version control.
- Every mutation must verify the configured Odoo database/company immediately before the call and must read both the task and reference back before reporting success.

**2026-09-02 explicit transport decision:** Use the native Odoo Studio
`/web/hook/<uuid>` endpoint and no custom controller/module. Treat only HTTP 200
with exact JSON `{"status":"ok"}` as acknowledgement, never as final state.
Immediately read the exact reference identity and linked task through
authenticated JSON-RPC, construct and validate `ReviewActionResult` in the
calling app's server, and report success only when it matches the requested
transition. On timeout or unknown outcome, perform exact readback before any
retry and infer success only when the transition demonstrably landed.

- A timeout means “unknown”; read by exact identity before retrying. Never create a replacement task.
- Existing four-type records and locally owned coding tasks must continue to work throughout rollout.
- Add a short, child-readable `CHANGELOG.md` entry before every push to a repository's `main` branch.

## File and Ownership Map

This is one coordinated plan because no app can safely ship the requested behavior by itself.

### GPI Plant Manager — submission owner and deployment record

- Modify `src/zira_dashboard/feedback_types.py` for the five Odoo-backed submission types and UI grouping metadata.
- Modify `src/zira_dashboard/_schema.py` for exact submitter identity and local-vs-Odoo task ownership.
- Modify `src/zira_dashboard/feedback_store.py`, `feedback_projection.py`, `feedback_task_delivery.py`, and `feedback_task_worker.py` so V2 records wait for and adopt Odoo's linked task instead of creating a second task.
- Modify `src/zira_dashboard/odoo_improvements.py`, `feedback_sync.py`, and `app.py` for read-only linked-field access and inbound lifecycle polling.
- Modify `src/zira_dashboard/routes/feedback.py`, `templates/_feedback.html`, `static/feedback.js`, and `static/feedback.css` for the six-button chooser, Repair link, and timeclock employee picker.
- Add `src/zira_dashboard/feedback_inbound.py` for Odoo-to-local lifecycle/link adoption.
- Add `scripts/check_odoo_review_workflow.py`, `docs/odoo/2s-review-workflow-setup.md`, and `docs/odoo/contracts/2s-review-workflow-v2.json` for reproducible Odoo setup and read-only verification.
- Extend the existing `tests/test_feedback_*.py`, `tests/test_odoo_improvements.py`, and `tests/test_timeclock_feedback_static.py` suites.

### GPI Sales Manager — submission and task surface

- Modify `src/lib/feedback/catalog.ts`, `prompt.ts`, and `src/components/shell/FeedbackWidget.tsx` for exact choice semantics.
- Modify `src/app/api/feedback/route.ts`, `src/lib/improvements/contract.ts`, `projection.ts`, `odoo-scoped.ts`, and `inbound.ts` so review submissions create only a reference row and adopt Odoo's task link.
- Add `src/lib/improvements/review.ts` for typed review metadata/readback and `src/lib/improvements/review-actions.ts` for server-only action calls.
- Add `src/app/api/tasks/[id]/review/route.ts` and `src/components/tasks/ImprovementReviewActions.tsx`.
- Modify `src/lib/tasks/types.ts`, `store.ts`, `src/components/tasks/TaskDetailPane.tsx`, `TaskWindow.tsx`, and `TaskDetailsContent.tsx` to display review tasks assigned to the current user even though they live in the OS task project.
- Add the next Drizzle migration after the current highest migration if a local link-adoption index or constraint is needed; do not renumber existing migrations.

### GPI OS Manager — task surface

- Add review contract types to `src/integrations/odoo/types.ts` and methods to `OdooAdapter.ts` plus `odooJsonRpcAdapter.ts`.
- Add `src/features/tasks/domain/improvementReview.ts` and `src/features/tasks/services/improvementReviewService.ts` for batch metadata reads and server-only action calls.
- Add `src/app/api/tasks/[taskId]/review/route.ts` and `src/features/tasks/components/ImprovementReviewActions.tsx`.
- Modify `src/app/todo/page.tsx`, `src/features/tasks/components/TaskWorkspace.tsx`, `TaskTable.tsx`, `TaskList.tsx`, `TaskRow.tsx`, and `TaskDetailPanel.tsx` to pass separate review metadata without enabling coding-copy controls.

### Odoo — single workflow authority

- Add the exact `2s Improvement` option to `x_studio_type` in a duplicate database first.
- Add one scoped on-save automation that creates and links a task only when `x_studio_linked_task` is empty.
- Add one secret native external webhook action that applies Accept, Decline, Assign, Complete, and Move to L10 in Odoo. It returns only the native acknowledgement; the server-side adapter performs immediate authoritative task/reference readback.
- Add task-to-reference lifecycle automation for digital coding tasks so done/cancelled/open state is reflected in the same reference table without using the physical-review controls.

---

## Task 1: Freeze and Test the Versioned Cross-App Contract

**Files:**

- Create: `docs/odoo/contracts/2s-review-workflow-v2.json`
- Modify: `src/zira_dashboard/feedback_types.py`
- Modify: `src/zira_dashboard/odoo_improvements.py`
- Test: `tests/test_feedback_types.py`
- Test: `tests/test_odoo_improvements.py`
- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/improvements/contract.ts`
- Test: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/improvements/contract.test.ts`
- Modify: `/Users/dalegruber/Projects/gpi-os-manager/src/integrations/odoo/types.ts`
- Test: `/Users/dalegruber/Projects/gpi-os-manager/tests/integrations/odoo/odooJsonRpcAdapter.test.ts`

- [ ] **Step 1: Record and protect current workspace state.**

Run in each repository:

```bash
git status --short
git diff -- src/lib/feedback/catalog.ts
```

The second command applies only to Sales Manager. Save the output in the implementation notes. Do not stage any pre-existing dirty file until its changes have been deliberately reconciled with this plan.

- [ ] **Step 2: Write failing contract tests.**

Assert all of the following in Python and TypeScript:

```text
types = Digital, Digital - New Feature, Physical - Issue,
        Physical - Suggestion, 2s Improvement
statuses = Requested, In-Progress, Completed, Declined
review types = Physical - Issue, Physical - Suggestion, 2s Improvement
repair URL = https://www.gpimaintenance.com/request
contract version = 2
linked task and linked work order are readable, not app-writable in V2
```

The compatibility preflight must accept exactly the known V1 four-type set or V2 five-type set. It must reject missing known values, renamed values such as `2S Improvement`, and extra unknown values.

- [ ] **Step 3: Run the new tests and confirm they fail for the missing fifth type/contract.**

```bash
pytest -q tests/test_feedback_types.py tests/test_odoo_improvements.py
cd /Users/dalegruber/Projects/gpi-sales-manager && npm test -- src/lib/improvements/contract.test.ts
cd /Users/dalegruber/Projects/gpi-os-manager && npm test -- tests/integrations/odoo/odooJsonRpcAdapter.test.ts
```

Expected: assertions for `2s Improvement`, V2 compatibility, or linked-field policy fail; unrelated tests remain green.

- [ ] **Step 4: Add the canonical JSON contract.**

Use this shape and exact values:

```json
{
  "version": 2,
  "model": "x_2s_improvements",
  "types": ["Digital", "Digital - New Feature", "Physical - Issue", "Physical - Suggestion", "2s Improvement"],
  "reviewTypes": ["Physical - Issue", "Physical - Suggestion", "2s Improvement"],
  "statuses": ["Requested", "In-Progress", "Completed", "Declined"],
  "taskStates": {"accepted": "03_approved", "declined": "1_canceled", "completed": "1_done"},
  "project": "GPI OS Manager - TASKS",
  "stages": {"initial": "General", "meeting": "L10"},
  "repairUrl": "https://www.gpimaintenance.com/request"
}
```

- [ ] **Step 5: Implement dark compatibility constants in all three apps.**

Expose explicit helpers rather than broad string checks:

```python
def odoo_contract_version(selection_values: set[str]) -> int:
    """Return 1 or 2 for an exact known contract; raise ContractError otherwise."""
```

```ts
export type ImprovementType =
  | "Digital"
  | "Digital - New Feature"
  | "Physical - Issue"
  | "Physical - Suggestion"
  | "2s Improvement";

export const REVIEW_IMPROVEMENT_TYPES: ReadonlySet<ImprovementType> = new Set([
  "Physical - Issue",
  "Physical - Suggestion",
  "2s Improvement",
]);
```

Do not expose the new UI yet. `ODOO_FEEDBACK_WORKFLOW_V2_ENABLED` remains false by default.

- [ ] **Step 6: Make linked fields read-only in app runtime clients.**

Remove `x_studio_linked_task` and `x_studio_linked_wo` from V2 write allowlists. Keep both in read projections. Existing V1 rows may retain already-written values, but no V2 path clears or replaces them.

- [ ] **Step 7: Re-run the focused tests.**

Expected: all focused tests pass under fixtures representing both exact V1 and exact V2 selection sets.

- [ ] **Step 8: Commit each repository independently.**

```bash
git add docs/odoo/contracts/2s-review-workflow-v2.json src/zira_dashboard/feedback_types.py src/zira_dashboard/odoo_improvements.py tests/test_feedback_types.py tests/test_odoo_improvements.py CHANGELOG.md
git commit -m "feat: accept the versioned review workflow contract"
```

Use equivalent narrowly scoped commits in Sales Manager and OS Manager. Push each commit to its repository's `origin/main` only after its focused tests pass.

## Task 2: Document and Verify the Odoo Workflow in a Duplicate Database

**Files:**

- Create: `docs/odoo/2s-review-workflow-setup.md`
- Create: `scripts/check_odoo_review_workflow.py`
- Test: `tests/test_check_odoo_review_workflow.py`
- Modify: `.env.example`

- [ ] **Step 1: Write failing checker tests using a fake Odoo client.**

Test exact success and each safe failure:

- fifth selection missing or misspelled;
- duplicate/missing `GPI OS Manager - TASKS` project;
- duplicate/missing General or L10 stage on that project;
- Dale missing, inactive, or ambiguous;
- creation automation missing or disabled;
- review webhook automation missing or disabled;
- wrong watched fields/domain;
- secret absent locally;
- production target used with `--exercise`.

The checker is read-only unless both `--exercise` and `--allow-duplicate-db` are supplied and the database UUID equals `ODOO_REVIEW_TEST_DB_UUID`.

- [ ] **Step 2: Run the test and confirm it fails because the checker does not exist.**

```bash
pytest -q tests/test_check_odoo_review_workflow.py
```

Expected: import/file-not-found failure.

- [ ] **Step 3: Write the audited setup runbook.**

Record exact model names, field names, domains, watched fields, project/stage lookup rules, feature gate order, duplicate-database evidence, rollback steps, and the secret rotation procedure. Store only an environment-variable name in the document:

```dotenv
ODOO_REVIEW_ACTION_WEBHOOK_URL=
ODOO_REVIEW_TEST_DB_UUID=
ODOO_FEEDBACK_WORKFLOW_V2_ENABLED=false
```

The URL value must never be committed.

- [ ] **Step 4: Define the creation automation exactly once.**

Configure an on-create/save automation scoped to supported GPI sources, `Requested`, and empty linked fields. Its action must:

1. Re-search the row by `x_studio_source` + `x_studio_source_id` and require exactly one match.
2. Return the existing `x_studio_linked_task` on retry.
3. Require `x_studio_linked_wo` to remain empty for review types.
4. Resolve exactly one project, stage, and active Dale user.
5. Create the task and write `x_studio_linked_task` in the same Odoo transaction.
6. Route digital types to the submitting app's coding project and review types to `GPI OS Manager - TASKS` / General.
7. Post a chatter entry containing source identity and submission type, not a secret.

Use a uniqueness safeguard on source + source ID if the existing Studio model supports the constraint; otherwise the action must reject duplicates before creation and the app checker must surface them.

- [ ] **Step 5: Define the single review action payload and result.**

```ts
export type ReviewActionRequest = {
  taskId: number;
  source: "GPI Plant Manager" | "GPI Sales Manager";
  sourceId: string;
  action: "accept" | "decline" | "assign" | "complete" | "move_l10";
  actorUserId: number;
  assigneeUserId?: number;
  note?: string;
};

export type ReviewActionResult = {
  ok: true;
  task: { id: number; state: string; stageId: number; assigneeUserIds: number[] };
  improvement: { id: number; status: string; linkedTaskId: number; dateStop: string | null };
};
```

The Odoo action must reject unknown keys/actions, non-positive IDs, overlong notes, mismatched source identity, non-review types, actor without one active employee, actor not currently assigned, illegal state, ambiguous assignee, and any linked-task mismatch. Decline and Complete require a trimmed note. Assign requires exactly one active target user/employee. Move to L10 resolves the exact stage within the exact project. All actions post chatter.

- [ ] **Step 6: Encode the transition table once in Odoo.**

```text
Requested + accept   -> task 03_approved, reference In-Progress
Requested + decline  -> task 1_canceled, reference Declined + stop/closer
In-Progress + assign -> assignee changed, reference unchanged
In-Progress + complete -> task 1_done, reference Completed + stop/closer
In-Progress + move_l10 -> stage L10, assignee and reference status unchanged
terminal + any action -> conflict, no write
```

Task and reference changes must occur in one transaction. After the exact native acknowledgement, the calling app must re-read both records and build the action result locally. A mismatch is failure or conflict.

- [ ] **Step 7: Implement the checker and run it read-only against the duplicate database.**

```bash
python scripts/check_odoo_review_workflow.py
```

Expected before configuration: a nonzero exit with only safe field/config names. Expected after configuration: exit 0 with V2, one project, both stages, one Dale identity, and both enabled automations. No webhook URL is printed.

- [ ] **Step 8: Exercise disposable review rows in the duplicate database.**

```bash
python scripts/check_odoo_review_workflow.py --exercise --allow-duplicate-db
```

Expected: one task per row under retries; exact Accept/Decline/Assign/Complete/L10 results; no linked work order; original notes unchanged; cleanup archives only the disposable duplicate-database records created by this test. The script must refuse this mode against production.

- [ ] **Step 9: Commit and push the runbook/checker.**

```bash
git add docs/odoo/2s-review-workflow-setup.md scripts/check_odoo_review_workflow.py tests/test_check_odoo_review_workflow.py .env.example CHANGELOG.md
git commit -m "docs: make the Odoo review workflow reproducible"
git push origin main
```

## Task 3: Store Exact Plant Submitter Identity and Task Ownership

**Files:**

- Modify: `src/zira_dashboard/_schema.py`
- Modify: `src/zira_dashboard/feedback_store.py`
- Modify: `src/zira_dashboard/feedback_task_delivery.py`
- Test: `tests/test_feedback_schema.py`
- Test: `tests/test_feedback_store.py`
- Test: `tests/test_feedback_task_delivery.py`

- [ ] **Step 1: Write failing schema/store tests.**

Require:

```sql
feedback.submitter_employee_odoo_id BIGINT NULL
feedback_task_delivery.task_owner TEXT NOT NULL DEFAULT 'local'
CHECK (task_owner IN ('local', 'odoo'))
```

New V2 submissions use `task_owner='odoo'`. Existing rows default to `local` and keep their current delivery behavior. Positive signed-64-bit validation is mandatory for an employee ID.

- [ ] **Step 2: Run the tests and observe the missing columns/arguments.**

```bash
pytest -q tests/test_feedback_schema.py tests/test_feedback_store.py tests/test_feedback_task_delivery.py
```

- [ ] **Step 3: Add idempotent migrations and the explicit create API.**

Add these exact keyword parameters to the existing `create_submission()`
signature while preserving its current validated return type and transaction:

```python
submitter_employee_odoo_id: int | None
task_owner: Literal["local", "odoo"]
```

Require `submitter_employee_odoo_id` for timeclock V2 submissions. Permit signed-in private submissions to retain the exact email-to-employee resolution path until their ID is resolved during projection.

- [ ] **Step 4: Keep queue creation atomic.**

Insert feedback, optional image, Odoo mirror intent, and task-delivery ownership in one database transaction. A failed insert must leave none of the four records behind.

- [ ] **Step 5: Pass focused tests and commit.**

```bash
pytest -q tests/test_feedback_schema.py tests/test_feedback_store.py tests/test_feedback_task_delivery.py
git add src/zira_dashboard/_schema.py src/zira_dashboard/feedback_store.py src/zira_dashboard/feedback_task_delivery.py tests/test_feedback_schema.py tests/test_feedback_store.py tests/test_feedback_task_delivery.py CHANGELOG.md
git commit -m "feat: record Odoo-owned feedback delivery"
git push origin main
```

## Task 4: Add Plant Timeclock Identity and the Six-Button Chooser

**Files:**

- Modify: `src/zira_dashboard/routes/feedback.py`
- Modify: `src/zira_dashboard/templates/_feedback.html`
- Modify: `src/zira_dashboard/static/feedback.js`
- Modify: `src/zira_dashboard/static/feedback.css`
- Modify: `src/zira_dashboard/feedback_types.py`
- Test: `tests/test_feedback_routes.py`
- Test: `tests/test_timeclock_feedback_static.py`
- Test: `tests/test_feedback_types.py`

- [ ] **Step 1: Write failing route and static tests for the exact UI contract.**

Assert the order and grouping:

```text
Reporting — the 2s board triages it
  Bug
  New Feature
  Floor Issue
  Floor Suggestion
Ready to create work — straight to the floor team
  Repair
  2s Improvement
```

Assert Repair has an external `href` to the canonical `www` URL and is never submitted to `/feedback`. Assert review forms request description + optional screenshot only. Assert the label is never `2S Improvement`.

- [ ] **Step 2: Write failing timeclock identity tests.**

The timeclock form must show `Who is submitting this?`, load only active, non-excluded people with positive Odoo employee IDs, require one selection, and reject inactive, unknown, missing, or duplicate IDs server-side. Private screens omit the picker and use the authenticated UPN.

- [ ] **Step 3: Run the focused tests and confirm current four-button behavior fails them.**

```bash
pytest -q tests/test_feedback_routes.py tests/test_timeclock_feedback_static.py tests/test_feedback_types.py
```

- [ ] **Step 4: Add a server-provided chooser configuration.**

Do not duplicate labels/types in JavaScript. Render JSON or `data-*` attributes from `FEEDBACK_TYPES`, including group and behavior:

```python
FeedbackType("two_s_improvement", "2s Improvement", "A small change to make work better", "2s Improvement", group="ready", behavior="review")
```

Repair is a link definition, not a `FeedbackType`, because it has no submission.

- [ ] **Step 5: Add exact employee selection validation.**

Query the current local roster with parameterized SQL or the existing roster loader. Convert the posted value to a positive integer and require exactly one current eligible row whose `odoo_id` matches. Pass that exact employee ID to `create_submission`; do not accept a posted name/email as identity.

- [ ] **Step 6: Gate new behavior.**

When `ODOO_FEEDBACK_WORKFLOW_V2_ENABLED != "true"`, keep the current four submission choices and do not render 2s Improvement. Repair may render as a link only if product rollout explicitly permits it. When enabled, require a passing cached/read-only V2 contract check; otherwise disable review submission with an operator-safe message.

- [ ] **Step 7: Implement responsive presentation.**

Match the approved two-section reference at desktop widths and stack cleanly on the timeclock. Keep keyboard focus, Escape behavior, idle-timer pause/resume, screenshot preview/removal, and accessible labels intact. If `window.open` is blocked, leave the modal open and reveal the same normal Repair link.

- [ ] **Step 8: Pass focused tests and commit.**

```bash
pytest -q tests/test_feedback_routes.py tests/test_timeclock_feedback_static.py tests/test_feedback_types.py
git add src/zira_dashboard/routes/feedback.py src/zira_dashboard/templates/_feedback.html src/zira_dashboard/static/feedback.js src/zira_dashboard/static/feedback.css src/zira_dashboard/feedback_types.py tests/test_feedback_routes.py tests/test_timeclock_feedback_static.py tests/test_feedback_types.py CHANGELOG.md
git commit -m "feat: add the six-choice feedback menu"
git push origin main
```

Keep the production V2 flag off.

## Task 5: Make Plant Adopt Odoo's Linked Task and Lifecycle

**Files:**

- Modify: `src/zira_dashboard/odoo_improvements.py`
- Modify: `src/zira_dashboard/feedback_projection.py`
- Modify: `src/zira_dashboard/feedback_task_delivery.py`
- Modify: `src/zira_dashboard/feedback_task_worker.py`
- Create: `src/zira_dashboard/feedback_inbound.py`
- Modify: `src/zira_dashboard/feedback_sync.py`
- Modify: `src/zira_dashboard/app.py`
- Test: `tests/test_feedback_projection.py`
- Test: `tests/test_feedback_task_worker.py`
- Create: `tests/test_feedback_inbound.py`
- Test: `tests/test_feedback_warmer.py`

- [ ] **Step 1: Write failing projection tests for exact employee identity and note preservation.**

Projection prefers `submitter_employee_odoo_id` when present and verifies that the referenced employee is active. It falls back to exact email resolution only for private submissions. Lifecycle writes must never replace `x_studio_notes`; remove resolution-note projection for Odoo-owned review rows.

- [ ] **Step 2: Write failing task-delivery tests for `task_owner='odoo'`.**

Require these states:

```text
reference not mirrored yet -> pending, no task RPC
reference exists, linked task empty -> pending, no task RPC
reference has one linked task -> adopt task ID and mark delivered
duplicate references or mismatched link -> attention, no write
timeout -> read exact identity, then decide; never create
```

Existing `task_owner='local'` rows must continue through the current creator and lifecycle worker unchanged.

- [ ] **Step 3: Write failing inbound lifecycle tests.**

Define a pure decision function and transactional apply path:

```python
@dataclass(frozen=True)
class InboundImprovement:
    feedback_id: int
    odoo_improvement_id: int
    odoo_task_id: int | None
    status: Literal["Requested", "In-Progress", "Completed", "Declined"]
    date_stop: datetime | None
```

Requested maps to local `requested`, In-Progress to `in_progress`, Completed to `completed`, and Declined to `declined`. Inbound adoption sets `lifecycle_origin='odoo'`, records the linked task ID, advances local projection bookkeeping so stale Requested is not replayed, and does not enqueue an outbound lifecycle mutation.

- [ ] **Step 4: Run focused tests and confirm failures.**

```bash
pytest -q tests/test_feedback_projection.py tests/test_feedback_task_worker.py tests/test_feedback_inbound.py tests/test_feedback_warmer.py
```

- [ ] **Step 5: Add safe linked-field reads.**

Extend Odoo reads with `x_studio_linked_task` and `x_studio_linked_wo`. Parse false/null or `[id, display_name]` many2one values strictly. Reject malformed, non-positive, or multiple relationships. Never write these fields.

- [ ] **Step 6: Implement Odoo-owned task adoption.**

Branch `feedback_task_worker.run_batch()` on `task_owner`. For Odoo-owned rows, perform read-only exact-identity lookup and store the linked task ID only after readback proves the reference and task agree. Do not call task create/update/complete RPCs.

- [ ] **Step 7: Add the inbound warmer.**

```python
async def _tick_feedback_inbound():
    """Adopt authoritative Odoo feedback status and linked task IDs."""
    from . import feedback_inbound
    await asyncio.to_thread(feedback_inbound.run_batch)
```

Register it after the mirror warmer and before the task-adoption warmer. Use an independent lease/cursor so overlapping app workers do not apply a stale page twice.

- [ ] **Step 8: Verify stale local status cannot overwrite Odoo.**

Add a regression test: submit Requested, Odoo accepts it, inbound adopts In-Progress, then the normal mirror worker runs. Expected: no write returning the reference to Requested and no task mutation.

- [ ] **Step 9: Pass focused and full Plant checks, then commit.**

```bash
pytest -q tests/test_feedback_projection.py tests/test_feedback_task_worker.py tests/test_feedback_inbound.py tests/test_feedback_warmer.py tests/test_feedback_sync.py
ruff check src tests scripts
pytest -q
git add src/zira_dashboard/odoo_improvements.py src/zira_dashboard/feedback_projection.py src/zira_dashboard/feedback_task_delivery.py src/zira_dashboard/feedback_task_worker.py src/zira_dashboard/feedback_inbound.py src/zira_dashboard/feedback_sync.py src/zira_dashboard/app.py tests/test_feedback_projection.py tests/test_feedback_task_worker.py tests/test_feedback_inbound.py tests/test_feedback_warmer.py CHANGELOG.md
git commit -m "feat: adopt Odoo-owned feedback tasks"
git push origin main
```

## Task 6: Route Sales Feedback to One Odoo-Owned Task

**Files:**

- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/feedback/catalog.ts`
- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/feedback/catalog.test.ts`
- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/feedback/prompt.ts`
- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/feedback/prompt.test.ts`
- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/components/shell/FeedbackWidget.tsx`
- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/components/shell/FeedbackWidget.test.tsx`
- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/app/api/feedback/route.ts`
- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/improvements/projection.ts`
- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/improvements/inbound.ts`
- Test: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/improvements/inbound.test.ts`
- Create: `/Users/dalegruber/Projects/gpi-sales-manager/src/app/api/feedback/route.test.ts`

- [ ] **Step 1: Reconcile the existing uncommitted catalog change.**

Keep useful `digital | review | link` work already present, but correct it to the approved contract: exact `2s Improvement`, distinct Odoo type, no equipment/area/urgency input for review types, and Repair as the canonical external link. Record which lines were pre-existing so the eventual commit does not claim unrelated work.

- [ ] **Step 2: Write failing catalog/widget tests.**

Assert the same six labels/order/groups as Plant Manager. Assert `opensWorkOrder()` is false for Floor Issue, Floor Suggestion, and 2s Improvement. Assert Repair never sends a POST. Assert review types need description and optional attachments only.

- [ ] **Step 3: Write failing route tests.**

For each review type, assert:

```text
local feedback row inserted once
improvement sync queued once
task create not called
work-order create not called
taskOdooId initially null
workOrderOdooId null
HTTP 201 means queued, not “task created”
```

Digital submissions also stop creating a task directly once Odoo creation automation is enabled; the local row queues the authoritative reference and later adopts the link.

- [ ] **Step 4: Implement the route split.**

The browser handles Repair. The API accepts only Odoo-backed types, validates the V2 gate/contract, stores the request, and calls `enqueueImprovementSync`. Delete the V2 calls to `createWorkOrder()` and `createTask()` from this route. Preserve V1 behavior behind the disabled compatibility path until cutover.

- [ ] **Step 5: Adopt Odoo's linked task inbound.**

Extend `InboundImprovement`:

```ts
export type InboundImprovement = {
  odooId: number;
  submissionId: number;
  status: ImprovementStatus;
  dateStop: Date | null;
  linkedTaskId: number | null;
  linkedWorkOrderId: number | null;
};
```

For review types, require linked work order null. Set `feedbackSubmissions.taskOdooId` only when null or equal; a conflicting non-null task ID is quarantined and never overwritten. Do not close/reopen the task from inbound status: Odoo already owns both records.

- [ ] **Step 6: Pass focused tests and verify no review work-order call remains.**

```bash
cd /Users/dalegruber/Projects/gpi-sales-manager
npm test -- src/lib/feedback/catalog.test.ts src/lib/feedback/prompt.test.ts src/components/shell/FeedbackWidget.test.tsx src/app/api/feedback/route.test.ts src/lib/improvements/inbound.test.ts
rg -n "createWorkOrder|createTask" src/app/api/feedback/route.ts
```

Expected: tests pass; any remaining creator calls are confined to the gated legacy branch and are annotated with the retirement condition.

- [ ] **Step 7: Run typecheck/lint and commit only reconciled files.**

```bash
npm run typecheck
npm run lint
git add src/lib/feedback/catalog.ts src/lib/feedback/catalog.test.ts src/lib/feedback/prompt.ts src/lib/feedback/prompt.test.ts src/components/shell/FeedbackWidget.tsx src/components/shell/FeedbackWidget.test.tsx src/app/api/feedback/route.ts src/app/api/feedback/route.test.ts src/lib/improvements/projection.ts src/lib/improvements/inbound.ts src/lib/improvements/inbound.test.ts CHANGELOG.md
git diff --cached --check
git commit -m "feat: route feedback through Odoo review tasks"
git push origin main
```

## Task 7: Add One Server-Side Review Action Client in Sales Manager

**Files:**

- Create: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/improvements/review.ts`
- Create: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/improvements/review.test.ts`
- Create: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/improvements/review-actions.ts`
- Create: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/improvements/review-actions.test.ts`
- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/improvements/odoo-scoped.ts`
- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/improvements/odoo-scoped.test.ts`
- Create: `/Users/dalegruber/Projects/gpi-sales-manager/src/app/api/tasks/[id]/review/route.ts`
- Create: `/Users/dalegruber/Projects/gpi-sales-manager/src/app/api/tasks/[id]/review/route.test.ts`

- [ ] **Step 1: Write failing parsers for linked review metadata.**

```ts
export type ImprovementReview = {
  improvementId: number;
  taskId: number;
  source: string;
  sourceId: string;
  type: "Physical - Issue" | "Physical - Suggestion" | "2s Improvement";
  status: ImprovementStatus;
  submittedByEmployeeId: number | null;
  submittedByName: string | null;
  notes: string;
  beforeImagePresent: boolean;
};
```

Batch lookup uses `x_studio_linked_task in [...]`; it rejects duplicate references for one task rather than choosing one. Digital rows are not review metadata.

- [ ] **Step 2: Write failing action-client tests.**

Mock `fetch` and prove: URL is read server-side, payload contains exact source identity and authenticated actor Odoo user ID, decline/complete require trimmed notes, assign requires positive target user ID, only HTTP 200 with exact `{"status":"ok"}` is accepted as acknowledgement, non-2xx responses are redacted, timeouts trigger exact readback before any retry, and success is returned only if the separate authenticated task/reference readback matches the requested transition.

- [ ] **Step 3: Run tests and confirm modules are missing.**

```bash
cd /Users/dalegruber/Projects/gpi-sales-manager
npm test -- src/lib/improvements/review.test.ts src/lib/improvements/review-actions.test.ts 'src/app/api/tasks/[id]/review/route.test.ts'
```

- [ ] **Step 4: Add the narrow read scope and action client.**

Allow read-only `project.task`, `project.task.type`, and `res.users` only as needed for readback/labels. Keep `x_2s_improvements` as the only mutable model in the mirror; the review action client performs no JSON-RPC write and calls only the secret Odoo webhook.

- [ ] **Step 5: Add the authenticated API route.**

Request body:

```ts
type AppReviewBody = {
  action: "accept" | "decline" | "assign" | "complete" | "move_l10";
  assigneeUserId?: number;
  note?: string;
};
```

The route derives task ID from the URL, source/source ID from Odoo metadata, and actor user ID from the authenticated Sales Manager session. It never accepts actor/source identity from the browser. Return 409 for stale state and include the safe current review state so the client can refresh.

- [ ] **Step 6: Pass tests, typecheck, lint, and commit.**

```bash
npm test -- src/lib/improvements/review.test.ts src/lib/improvements/review-actions.test.ts src/lib/improvements/odoo-scoped.test.ts 'src/app/api/tasks/[id]/review/route.test.ts'
npm run typecheck
npm run lint
git add src/lib/improvements/review.ts src/lib/improvements/review.test.ts src/lib/improvements/review-actions.ts src/lib/improvements/review-actions.test.ts src/lib/improvements/odoo-scoped.ts src/lib/improvements/odoo-scoped.test.ts 'src/app/api/tasks/[id]/review/route.ts' 'src/app/api/tasks/[id]/review/route.test.ts' CHANGELOG.md
git commit -m "feat: call Odoo review actions from Sales Manager"
git push origin main
```

## Task 8: Show the Shared Review Task and Actions in Sales Manager

**Files:**

- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/tasks/types.ts`
- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/tasks/store.ts`
- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/lib/tasks/store.test.ts`
- Create: `/Users/dalegruber/Projects/gpi-sales-manager/src/components/tasks/ImprovementReviewActions.tsx`
- Create: `/Users/dalegruber/Projects/gpi-sales-manager/src/components/tasks/ImprovementReviewActions.test.tsx`
- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/components/tasks/TaskDetailPane.tsx`
- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/components/tasks/TaskWindow.tsx`
- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/components/tasks/TaskDetailsContent.tsx`
- Modify: `/Users/dalegruber/Projects/gpi-sales-manager/src/components/tasks/TaskDetailsContent.test.tsx`

- [ ] **Step 1: Write failing task visibility tests.**

An active linked review task assigned to the current Odoo user appears in Sales Manager even when its project is `GPI OS Manager - TASKS`. Unassigned or other-user tasks do not bypass the normal scope. The test must prove inclusion is based on the exact linked-reference query, not title words.

- [ ] **Step 2: Write failing control-state tests.**

```text
Requested -> Accept, Decline
In-Progress -> Assign, Complete, Move to L10
Completed/Declined -> no mutation buttons, show terminal result
Digital -> no review controls
ambiguous/missing reference -> warning, no mutation buttons
```

Decline and Complete open a required-text dialog. Assign uses exact active Odoo users. All buttons disable while a request is pending.

- [ ] **Step 3: Run focused tests and observe failures.**

```bash
cd /Users/dalegruber/Projects/gpi-sales-manager
npm test -- src/lib/tasks/store.test.ts src/components/tasks/ImprovementReviewActions.test.tsx src/components/tasks/TaskDetailsContent.test.tsx
```

- [ ] **Step 4: Add review metadata separately from coding feedback.**

Add `improvementReview?: ImprovementReview | null` to the task presentation model. Do not reuse `TaskFeedback`; doing so would show `Copy for Coding` on physical reviews.

- [ ] **Step 5: Implement controls with authoritative refresh.**

POST to the dedicated route, then invalidate/refetch task + review metadata. Display success only from the returned readback. On 409, show “This review changed in Odoo. The latest state is shown.” and refresh. Do not optimistically change status.

- [ ] **Step 6: Pass focused/full Sales checks and commit.**

```bash
npm test -- src/lib/tasks/store.test.ts src/components/tasks/ImprovementReviewActions.test.tsx src/components/tasks/TaskDetailsContent.test.tsx src/components/tasks/TaskDetailPane.test.tsx src/components/tasks/TaskWindow.test.tsx
npm test
npm run typecheck
npm run lint
npm run improvements:preflight
git add src/lib/tasks/types.ts src/lib/tasks/store.ts src/lib/tasks/store.test.ts src/components/tasks/ImprovementReviewActions.tsx src/components/tasks/ImprovementReviewActions.test.tsx src/components/tasks/TaskDetailPane.tsx src/components/tasks/TaskWindow.tsx src/components/tasks/TaskDetailsContent.tsx src/components/tasks/TaskDetailsContent.test.tsx CHANGELOG.md
git commit -m "feat: review Odoo improvements in Sales Manager"
git push origin main
```

Keep the V2 UI gate off in production.

## Task 9: Add Read-Only Review Metadata to OS Manager

**Files:**

- Modify: `/Users/dalegruber/Projects/gpi-os-manager/src/integrations/odoo/types.ts`
- Modify: `/Users/dalegruber/Projects/gpi-os-manager/src/integrations/odoo/OdooAdapter.ts`
- Modify: `/Users/dalegruber/Projects/gpi-os-manager/src/integrations/odoo/odooJsonRpcAdapter.ts`
- Modify: `/Users/dalegruber/Projects/gpi-os-manager/tests/integrations/odoo/odooJsonRpcAdapter.test.ts`
- Create: `/Users/dalegruber/Projects/gpi-os-manager/src/features/tasks/domain/improvementReview.ts`
- Create: `/Users/dalegruber/Projects/gpi-os-manager/src/features/tasks/services/improvementReviewService.ts`
- Create: `/Users/dalegruber/Projects/gpi-os-manager/src/features/tasks/services/improvementReviewService.test.ts`
- Modify: `/Users/dalegruber/Projects/gpi-os-manager/src/app/todo/page.tsx`

- [ ] **Step 1: Write failing adapter/service tests.**

Add an adapter method:

```ts
listImprovementReviewsByTaskIds(odooTaskIds: string[]): Promise<OdooImprovementReview[]>;
```

Test chunked batch lookup, strict many2one parsing, exact review-type filter, empty input without an RPC, duplicate reference quarantine, and no app-side write to `x_2s_improvements`.

- [ ] **Step 2: Run tests and confirm the interface/module is missing.**

```bash
cd /Users/dalegruber/Projects/gpi-os-manager
npm test -- tests/integrations/odoo/odooJsonRpcAdapter.test.ts src/features/tasks/services/improvementReviewService.test.ts
```

- [ ] **Step 3: Implement strict metadata reads.**

Use exact fields: id, source, source ID, type, status, submitted by, notes, before-image presence, linked task, linked work order, date stop. Never fetch binary image bytes in task-list batch calls. Add a separate authorized image endpoint only if the existing Odoo integration can stream it safely.

- [ ] **Step 4: Load metadata for the visible task set.**

In `todo/page.tsx`, batch the Odoo task IDs already loaded and construct `improvementReviewByOdooTaskId`. A metadata read failure must not hide the task; it disables review controls and shows a safe refresh warning.

- [ ] **Step 5: Pass focused tests, typecheck, lint, and commit.**

```bash
npm test -- tests/integrations/odoo/odooJsonRpcAdapter.test.ts src/features/tasks/services/improvementReviewService.test.ts
npm run typecheck
npm run lint
git add src/integrations/odoo/types.ts src/integrations/odoo/OdooAdapter.ts src/integrations/odoo/odooJsonRpcAdapter.ts tests/integrations/odoo/odooJsonRpcAdapter.test.ts src/features/tasks/domain/improvementReview.ts src/features/tasks/services/improvementReviewService.ts src/features/tasks/services/improvementReviewService.test.ts src/app/todo/page.tsx CHANGELOG.md
git commit -m "feat: read Odoo improvement reviews in OS Manager"
git push origin main
```

## Task 10: Add the Same Odoo Review Actions to OS Manager

**Files:**

- Modify: `/Users/dalegruber/Projects/gpi-os-manager/src/features/tasks/services/improvementReviewService.ts`
- Modify: `/Users/dalegruber/Projects/gpi-os-manager/src/features/tasks/services/improvementReviewService.test.ts`
- Create: `/Users/dalegruber/Projects/gpi-os-manager/src/app/api/tasks/[taskId]/review/route.ts`
- Create: `/Users/dalegruber/Projects/gpi-os-manager/src/app/api/tasks/[taskId]/review/route.test.ts`
- Create: `/Users/dalegruber/Projects/gpi-os-manager/src/features/tasks/components/ImprovementReviewActions.tsx`
- Create: `/Users/dalegruber/Projects/gpi-os-manager/src/features/tasks/components/ImprovementReviewActions.test.tsx`
- Modify: `/Users/dalegruber/Projects/gpi-os-manager/src/features/tasks/components/TaskWorkspace.tsx`
- Modify: `/Users/dalegruber/Projects/gpi-os-manager/src/features/tasks/components/TaskTable.tsx`
- Modify: `/Users/dalegruber/Projects/gpi-os-manager/src/features/tasks/components/TaskList.tsx`
- Modify: `/Users/dalegruber/Projects/gpi-os-manager/src/features/tasks/components/TaskRow.tsx`
- Modify: `/Users/dalegruber/Projects/gpi-os-manager/src/features/tasks/components/TaskDetailPanel.tsx`
- Modify: `/Users/dalegruber/Projects/gpi-os-manager/src/features/tasks/components/TaskDetailPanel.test.tsx`

- [ ] **Step 1: Write failing API tests using the same app request contract as Sales Manager.**

The route derives `actorUserId` from `requireCurrentActor()` → PersonRecord → `odooUserId`, derives source identity from the reference read, rejects an actor without an Odoo user, redacts webhook failures, and returns only verified task/reference readback.

- [ ] **Step 2: Write failing UI tests for action parity.**

Use the same state/button table, required note behavior, assignee validation, conflict refresh message, terminal read-only presentation, and pending-button behavior as Sales Manager.

- [ ] **Step 3: Run focused tests and confirm failures.**

```bash
cd /Users/dalegruber/Projects/gpi-os-manager
npm test -- 'src/app/api/tasks/[taskId]/review/route.test.ts' src/features/tasks/components/ImprovementReviewActions.test.tsx src/features/tasks/components/TaskDetailPanel.test.tsx
```

- [ ] **Step 4: Implement the server action call.**

Use the same payload and verification rules as Task 7. The browser receives neither the webhook URL nor actor/source fields. Keep this path separate from generic task PATCH/complete routes so those routes cannot bypass Requested → Accept.

- [ ] **Step 5: Thread review metadata through the task components.**

Pass `improvementReview` independently from existing `feedbackMetadata`. `TaskDetailPanel` shows `ImprovementReviewActions` for physical review metadata and must not show `CopyForCodingButton` for it. Task rows may show a small `Review requested`, `Accepted`, `Completed`, or `Declined` badge derived from Odoo status.

- [ ] **Step 6: Ensure L10 movement uses Odoo action, not generic origin editing.**

The review button calls `move_l10`; it does not locally set TaskOrigin or stage. After Odoo readback, normal inbound sync displays the task in L10.

- [ ] **Step 7: Pass focused/full OS checks and commit.**

```bash
npm test -- src/features/tasks/services/improvementReviewService.test.ts 'src/app/api/tasks/[taskId]/review/route.test.ts' src/features/tasks/components/ImprovementReviewActions.test.tsx src/features/tasks/components/TaskDetailPanel.test.tsx
npm test
npm run typecheck
npm run lint
npm run build
git add src/features/tasks/services/improvementReviewService.ts src/features/tasks/services/improvementReviewService.test.ts 'src/app/api/tasks/[taskId]/review/route.ts' 'src/app/api/tasks/[taskId]/review/route.test.ts' src/features/tasks/components/ImprovementReviewActions.tsx src/features/tasks/components/ImprovementReviewActions.test.tsx src/features/tasks/components/TaskWorkspace.tsx src/features/tasks/components/TaskTable.tsx src/features/tasks/components/TaskList.tsx src/features/tasks/components/TaskRow.tsx src/features/tasks/components/TaskDetailPanel.tsx src/features/tasks/components/TaskDetailPanel.test.tsx CHANGELOG.md
git commit -m "feat: review Odoo improvements in OS Manager"
git push origin main
```

Keep the V2 UI gate off in production.

## Task 11: Prove the Whole Workflow in the Duplicate Environment

**Files:**

- Modify: `scripts/check_odoo_review_workflow.py`
- Create: `tests/e2e/test_odoo_review_workflow.py`
- Modify: `docs/odoo/2s-review-workflow-setup.md`
- Add equivalent E2E fixtures to Sales Manager and OS Manager only where their existing harness requires local browser/API coverage.

- [ ] **Step 1: Add an opt-in cross-system E2E test.**

Require explicit duplicate-database environment variables and skip by default. The test creates unique source IDs with a test prefix and records every created ID for recoverable cleanup.

- [ ] **Step 2: Cover all five Odoo-backed submission types.**

Verify Bug and New Feature each produce one coding task. Verify Floor Issue, Floor Suggestion, and exact 2s Improvement each produce one OS review task in General assigned to Dale. Repeat delivery/readback and prove the linked task ID never changes. Assert every review has empty linked work order.

- [ ] **Step 3: Cover every lifecycle path from both apps.**

Run:

```text
Sales Accept -> OS sees In-Progress/Approved
OS Assign -> Sales sees new assignee
Sales Move to L10 -> OS sees L10
OS Complete with result -> Sales sees Completed/Done
new row: OS Decline with reason -> Sales sees Declined/Canceled
```

After each operation, read task and reference directly from Odoo and compare both apps. Verify original request notes and before image remain intact. Verify chatter includes action/actor/note where required.

- [ ] **Step 4: Cover failure and concurrency behavior.**

Test blocked Repair popup fallback without creating data, duplicate source identity, duplicate linked reference, webhook timeout followed by readback, inactive actor, inactive assignee, illegal Complete before Accept, terminal replay, and two concurrent Accept calls. Exactly one Odoo state wins; neither app invents success.

- [ ] **Step 5: Run all repository checks.**

```bash
cd /Users/dalegruber/Projects/gpi-plant-manager && ruff check src tests scripts && pytest -q
cd /Users/dalegruber/Projects/gpi-sales-manager && npm test && npm run typecheck && npm run lint && npm run improvements:preflight
cd /Users/dalegruber/Projects/gpi-os-manager && npm test && npm run typecheck && npm run lint && npm run build
```

Expected: all commands exit 0. Save duplicate-database checker/E2E results in the runbook without secrets or request text.

- [ ] **Step 6: Commit test/runbook evidence and push.**

```bash
cd /Users/dalegruber/Projects/gpi-plant-manager
git add scripts/check_odoo_review_workflow.py tests/e2e/test_odoo_review_workflow.py docs/odoo/2s-review-workflow-setup.md CHANGELOG.md
git commit -m "test: verify the shared Odoo review workflow"
git push origin main
```

## Task 12: Roll Out Safely and Verify Production Readback

**Files:**

- Modify: `docs/odoo/2s-review-workflow-setup.md`
- Modify: `CHANGELOG.md`
- Modify equivalent `.env.example`, operations docs, and `CHANGELOG.md` files in Sales Manager and OS Manager.

- [ ] **Step 1: Deploy compatibility readers with every V2 feature gate false.**

Verify the current four-type mirror and all existing task workflows remain healthy in production. Run read-only contract checks from all three apps.

- [ ] **Step 2: Apply the audited Odoo configuration to production.**

Use the duplicate-tested runbook. Add exact `2s Improvement`, the scoped task-creation automation, the review action webhook, and digital task/reference lifecycle automation. Rotate/store the production webhook URL in each server's secret manager. Never paste it into browser config or logs.

- [ ] **Step 3: Run production read-only verification.**

```bash
python scripts/check_odoo_review_workflow.py
```

Run the Sales Manager preflight and OS Manager adapter smoke check. All must report V2 and exact project/stage/field contracts before any UI gate changes.

- [ ] **Step 4: Enable Odoo-authoritative readers/actions first.**

Enable server-side review metadata and action routes in Sales Manager and OS Manager. Keep submission buttons disabled. Verify an existing duplicate-safe operator test row, or an approved disposable production test row, is visible identically in both apps.

- [ ] **Step 5: Enable submitters last.**

Enable `ODOO_FEEDBACK_WORKFLOW_V2_ENABLED=true` in Plant Manager, then Sales Manager. Confirm the exact six-button layout, timeclock employee picker, canonical Repair link, and no equipment fields for review types.

- [ ] **Step 6: Perform one approved production smoke submission per submitting app.**

For each, record source + source ID, then verify exactly one reference, one linked task, empty linked work order for review types, General/Dale initial routing, and identical Requested state in Sales Manager and OS Manager. Exercise Accept followed by one terminal path only on the approved smoke row.

- [ ] **Step 7: Observe at least two normal worker cycles.**

Verify Plant local state adopts Odoo, Sales task-link adoption settles, no duplicate task is created, no stale Requested write occurs, and no attention/dead-letter queue grows.

- [ ] **Step 8: Remove the V1 transition path only after all production checks pass.**

Delete legacy app-owned task/work-order creation for feedback, tighten preflights to V2 only, rerun every full suite, and deploy each repository independently. Retain rollback instructions that disable entry points while leaving Odoo records untouched.

- [ ] **Step 9: Final completion audit.**

Confirm all implementation commits are on each repository's `origin/main`, all required validations passed, and the same Odoo rows read back correctly. Only then, if this work corresponds to one exact existing `2s Improvement Reference Data` row, run the repository's required completion workflow against that supplied source ID and verify Odoo says `Completed`. If no exact lifecycle row was supplied, do not create or guess one and do not mark the implementation request complete in Odoo.

## Final Acceptance Checklist

- [ ] Plant Manager displays Bug, New Feature, Floor Issue, Floor Suggestion, Repair, and exact `2s Improvement` in the approved two-section layout.
- [ ] Repair opens `https://www.gpimaintenance.com/request` and creates nothing in Plant, Sales, OS, or the 2s table.
- [ ] Timeclock submission requires one exact active employee; private Plant screens use the signed-in employee.
- [ ] Review submissions have description + optional screenshot and no equipment/area/urgency field.
- [ ] Floor Issue, Floor Suggestion, and 2s Improvement create exactly one Odoo review task and no maintenance work order.
- [ ] Review tasks start in `GPI OS Manager - TASKS` / General / Dale with reference status Requested.
- [ ] Requested shows only Accept/Decline; accepted shows Assign/Complete/Move to L10; terminal items are read-only.
- [ ] Decline reason and completion result are required; original request notes/images are unchanged.
- [ ] Sales Manager and OS Manager invoke the same Odoo action and read the same task/reference state back.
- [ ] Plant and Sales adopt Odoo's linked task/status and never replay stale local state.
- [ ] Duplicate, timeout, inactive identity, invalid transition, and concurrent-action cases stop safely without creating another task.
- [ ] Full automated suites, type checks, lint checks, builds, preflights, duplicate-database E2E, and production readbacks pass.
- [ ] Every implementation commit is pushed to the correct `origin/main`; no unrelated dirty work was staged.
