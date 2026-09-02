# Simple Light-Bulb Task Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Plant Manager's six light-bulb buttons create the correct single Odoo work item, show the same review controls in Sales Manager and OS Manager, and synchronize the task lifecycle into the matching 2s Improvement reference row within one minute.

**Architecture:** Plant Manager durably queues each review submission, creates or adopts one Odoo reference row and one Odoo task, links them once, and runs the sole reference-lifecycle reconciler. Sales Manager and OS Manager mutate only the ordinary Odoo task through their existing authenticated APIs; a versioned, human-readable event in the task description carries the actor and required reason/result back to Plant Manager. Odoo stores normal records only and contains no custom workflow code, automation script, or special webhook.

**Tech Stack:** Python 3.12, FastAPI, Jinja, browser JavaScript, PostgreSQL, pytest; Next.js 15, React, strict TypeScript, Vitest, Tailwind, Drizzle/PostgreSQL in Sales Manager; Next.js, React, strict TypeScript, Vitest, Prisma/PostgreSQL in OS Manager; Odoo 19 XML-RPC/JSON-RPC standard model APIs.

## Global Constraints

- The visible labels are exactly `Bug`, `New Feature`, `Floor Issue`, `Floor Suggestion`, `Repair`, and `2s Improvement`.
- The chooser has two sections: `Reporting — the 2s board triages it` and `Ready to create work — straight to the floor team`.
- Repair opens `https://www.gpimaintenance.com/request` and creates no Plant feedback row, reference row, task, or work order.
- Floor Issue, Floor Suggestion, and 2s Improvement create one review task and no maintenance work order; `x_studio_linked_wo` stays empty.
- The review task is the only human work item. The `x_2s_improvements` row is synchronized reference data.
- Review tasks start in `GPI OS Manager - TASKS`, stage `General`, assigned to Dale.
- Requested tasks expose only Accept and Decline. Accepted tasks expose Assign, Complete, and Move to L10.
- Decline requires a nonblank reason. Complete requires a nonblank result.
- Plant Manager is the only app that writes Plant-owned reference links or lifecycle fields.
- Reference synchronization may lag task state by up to one 60-second worker interval.
- No custom Odoo module, Studio Execute Code, automated/server Python action, custom review webhook, or duplicate Odoo database is required or permitted.
- Existing Bug and New Feature behavior remains unchanged.
- Private Plant pages use the signed-in identity. Timeclock pages require `Who is submitting this?` and one exact active Odoo employee.
- Every write is followed by exact Odoo readback. Unknown outcomes read back before retry.
- Duplicate identities and conflicting links stop safely; no code chooses one or overwrites the conflict.
- Every main push includes the repository's required child-readable What's New note.
- Keep the cross-app UI gate off until all three implementations and full verification pass; enable it only in Task 10.

## File and interface map

### Canonical contract

- Plant Manager `docs/odoo/contracts/2s-review-workflow-v3.json`: exact labels, types, actions, task states, project/stages, event marker, and 60-second reference interval.
- Plant Manager `src/zira_dashboard/feedback_review_events.py`: Python encoder/parser used by task delivery and reconciliation.
- Sales Manager `src/lib/improvements/review-event.ts`: TypeScript encoder/parser used by Sales actions.
- OS Manager `src/features/feedback/domain/reviewEvent.ts`: matching TypeScript encoder/parser used by OS actions.

### Plant Manager

- `src/zira_dashboard/feedback_types.py`: one catalog for labels, groups, behavior, and Odoo types.
- `src/zira_dashboard/_schema.py`: retain the exact submitter Odoo employee ID on the local delivery row.
- `src/zira_dashboard/routes/feedback.py`: private/timeclock identity validation and durable submission endpoint.
- `src/zira_dashboard/templates/_feedback.html`, `static/feedback.js`, `static/feedback.css`: six-button chooser, employee picker, and Repair link fallback.
- `src/zira_dashboard/feedback_task_worker.py`, `odoo_client.py`: create/adopt the exact review task and record its ID.
- `src/zira_dashboard/odoo_improvements.py`, `feedback_sync.py`, `feedback_projection.py`: app-owned V3 reference writes and one-time linked-task compare-and-set.
- `src/zira_dashboard/feedback_review_reconciler.py`, `feedback_store.py`, `app.py`: 60-second task-to-reference lifecycle projection.

### Sales Manager

- `src/lib/improvements/review-metadata.ts`: batch lookup and strict review metadata mapping by linked task.
- `src/lib/improvements/review-action.ts`: validate an action, write one task, and verify readback.
- `src/app/api/tasks/[id]/review/route.ts`: authenticated review-action endpoint.
- `src/lib/tasks/types.ts`, `feedback-metadata.ts`, and `src/components/tasks/TaskDetailsContent.tsx`: attach metadata and render review controls.

### OS Manager

- `src/features/feedback/domain/reviewMetadata.ts`: map exact linked improvements onto task records.
- `src/features/feedback/services/reviewActionService.ts`: standard Odoo task action plus readback.
- `src/app/api/tasks/[taskId]/review/route.ts`: authenticated action endpoint.
- `src/integrations/odoo/OdooAdapter.ts`, `types.ts`, `odooJsonRpcAdapter.ts`: narrow task action and metadata reads.
- `src/features/tasks/domain/unifiedTask.ts`, `TaskDetailPanel.tsx`: presentation and controls.

---

## Task 1: Freeze the V3 app-owned task contract and event codec

**Repositories:** Plant Manager, Sales Manager, OS Manager

**Files:**
- Create: Plant `docs/odoo/contracts/2s-review-workflow-v3.json`
- Create: Sales `docs/odoo/contracts/2s-review-workflow-v3.json`
- Create: OS `docs/odoo/contracts/2s-review-workflow-v3.json`
- Create: Plant `src/zira_dashboard/feedback_review_events.py`
- Create: Plant `tests/test_feedback_review_events.py`
- Create: Sales `src/lib/improvements/review-event.ts`
- Create: Sales `src/lib/improvements/review-event.test.ts`
- Create: OS `src/features/feedback/domain/reviewEvent.ts`
- Create: OS `tests/unit/feedback/reviewEvent.test.ts`
- Modify: Plant `src/zira_dashboard/feedback_types.py`
- Modify: Sales `src/lib/improvements/contract.ts`
- Modify: OS `src/integrations/odoo/feedbackImprovementContract.ts`

**Interfaces:**
- Produces Python `ReviewEvent`, `encode_review_event(event) -> str`, and `parse_review_events(description_html) -> tuple[ReviewEvent, ...]`.
- Produces TypeScript `ReviewEvent`, `appendReviewEvent(descriptionHtml, event): string`, and `parseReviewEvents(descriptionHtml): ReviewEvent[]` in both task apps.
- `ReviewEvent.action` is exactly `accept | decline | assign | complete | move_l10`.
- Event fields are `eventId`, `action`, `actorOdooUserId`, `actorEmployeeId`, `occurredAt`, `detail`, and optional `targetOdooUserId`.
- Each runtime exports `REVIEW_WORKFLOW_ENABLED = false`; only Task 10 may flip it after all full suites pass.

The two TypeScript apps use this exact request/result contract:

```ts
export type ReviewAction = "accept" | "decline" | "assign" | "complete" | "move_l10";

export type ReviewActionRequest = {
  action: ReviewAction;
  detail: string | null;
  targetOdooUserId: number | null;
  taskVersion: string;
};

export type ImprovementReview = {
  referenceId: number;
  source: "GPI Plant Manager";
  sourceId: `GPI-PM-FB-${number}`;
  type: "Physical - Issue" | "Physical - Suggestion" | "2s Improvement";
  referenceStatus: "Requested" | "In-Progress" | "Completed" | "Declined";
  linkedTaskId: number;
  taskVersion: string;
  pending: boolean;
  allowedActions: ReviewAction[];
};

export type ReviewActionResult = {
  referenceId: number;
  linkedTaskId: number;
  taskState: "01_in_progress" | "03_approved" | "1_canceled" | "1_done";
  stageLabel: "General" | "L10";
  assigneeOdooUserId: number | null;
  taskVersion: string;
  referenceStatus: "Requested" | "In-Progress" | "Completed" | "Declined";
  referencePending: boolean;
};
```

- [ ] **Step 1: Write the failing contract and codec tests**

```python
def test_review_event_round_trip_preserves_escaped_detail():
    event = ReviewEvent(
        event_id="018f2f2e-1234-7abc-8def-1234567890ab",
        action="complete",
        actor_odoo_user_id=7,
        actor_employee_id=41,
        occurred_at="2026-09-02T18:30:00Z",
        detail="Guard fixed < safely",
        target_odoo_user_id=None,
    )
    encoded = encode_review_event(event)
    assert "&lt;" in encoded
    assert parse_review_events(encoded) == (event,)

def test_review_event_parser_ignores_incomplete_or_unknown_blocks():
    assert parse_review_events("<p>GPI-REVIEW-EVENT-V1</p><p>Action: erase</p>") == ()
```

```ts
it("round-trips the canonical review event", () => {
  const event: ReviewEvent = {
    eventId: "018f2f2e-1234-7abc-8def-1234567890ab",
    action: "complete",
    actorOdooUserId: 7,
    actorEmployeeId: 41,
    occurredAt: "2026-09-02T18:30:00Z",
    detail: "Guard fixed < safely",
    targetOdooUserId: null,
  };
  expect(parseReviewEvents(appendReviewEvent("<p>Original</p>", event))).toEqual([event]);
});
```

- [ ] **Step 2: Run the new tests and verify RED**

Run in Plant: `.venv/bin/python -m pytest -q tests/test_feedback_review_events.py`
Run in Sales: `npm test -- --run src/lib/improvements/review-event.test.ts`
Run in OS: `npm test -- --run tests/unit/feedback/reviewEvent.test.ts`
Expected: each fails because its event module or V3 contract is absent.

- [ ] **Step 3: Add the canonical V3 JSON and minimal codecs**

The complete canonical JSON is:

```json
{
  "version": 3,
  "model": "x_2s_improvements",
  "types": ["Digital", "Digital - New Feature", "Physical - Issue", "Physical - Suggestion", "2s Improvement"],
  "reviewTypes": ["Physical - Issue", "Physical - Suggestion", "2s Improvement"],
  "statuses": ["Requested", "In-Progress", "Completed", "Declined"],
  "taskStates": {"accepted": "03_approved", "declined": "1_canceled", "completed": "1_done"},
  "project": "GPI OS Manager - TASKS",
  "stages": {"initial": "General", "meeting": "L10"},
  "repairUrl": "https://www.gpimaintenance.com/request",
  "taskOwner": "plant-manager",
  "referenceSyncSeconds": 60,
  "reviewEventMarker": "GPI-REVIEW-EVENT-V1",
  "actions": ["accept", "decline", "assign", "complete", "move_l10"],
  "plantWritableReferenceFields": [
    "x_studio_linked_task",
    "x_studio_status",
    "x_studio_date_stop",
    "x_studio_completed_by",
    "x_studio_notes"
  ]
}
```

Copy this JSON byte-for-byte into all three repositories. Each codec must emit
escaped visible HTML paragraphs beginning with
`GPI-REVIEW-EVENT-V1`, reject booleans as IDs, accept only positive integer IDs,
require an RFC3339 UTC timestamp, require Detail for decline/complete, and require
`targetOdooUserId` only for assign. Parsing malformed blocks returns no event and
never executes or evaluates description text.

Each encoded event has this exact HTML shape; `Target Odoo user ID` is present
only for Assign, and all values are escaped text nodes:

```html
<p><strong>GPI-REVIEW-EVENT-V1</strong></p>
<ul>
  <li>Event ID: 018f2f2e-1234-7abc-8def-1234567890ab</li>
  <li>Action: complete</li>
  <li>Actor Odoo user ID: 7</li>
  <li>Actor employee ID: 41</li>
  <li>Time UTC: 2026-09-02T18:30:00Z</li>
  <li>Detail: Guard fixed &lt; safely</li>
</ul>
```

- [ ] **Step 4: Prove all three codecs and contract constants match**

Run the three Step 2 commands again.
Expected: all pass and each asserts the same marker, action order, task states,
project, stages, Repair URL, and 60-second interval.

- [ ] **Step 5: Add child-readable release notes and commit each repository**

Plant changelog sentence: `The three task apps now agree on the safe labels and history used by the new review buttons. The buttons are still hidden.`
Sales note: `Sales Manager now understands the shared review-button rules, but the buttons stay hidden until every app is ready.`
OS note: `OS Manager now understands the shared review-button rules, but the buttons stay hidden until every app is ready.`

Commit subjects:

```text
feat: define the app-owned review task contract
```

Expected: focused tests pass, only contract/codec/tests/release-note files are staged, and each commit remains unpushed until its task review is clean.

## Task 2: Add Plant feedback types and exact submitter identity

**Repository:** Plant Manager

**Files:**
- Modify: `src/zira_dashboard/feedback_types.py`
- Modify: `src/zira_dashboard/_schema.py`
- Modify: `src/zira_dashboard/feedback_store.py`
- Modify: `src/zira_dashboard/feedback_rollout.py`
- Create: `src/zira_dashboard/feedback_submitters.py`
- Modify: `src/zira_dashboard/routes/feedback.py`
- Modify: `tests/test_feedback_types.py`
- Modify: `tests/test_feedback_routes.py`
- Modify: `tests/test_feedback_rollout.py`
- Create: `tests/test_feedback_submitters.py`

**Interfaces:**
- `FeedbackType` gains `group: Literal["reporting", "ready"]`, `behavior: Literal["coding", "review", "external"]`, and nullable `odoo_value`; `FEEDBACK_TYPES` remains the one display catalog and Odoo-contract consumers filter out the external entry.
- `feedback_submitters.active_choices() -> tuple[SubmitterChoice, ...]` returns unique active employees sorted by name.
- `feedback_submitters.resolve_timeclock(employee_id) -> ResolvedSubmitter` rejects absent, inactive, unknown, and duplicate IDs.
- The local `feedback.submitter_employee_odoo_id BIGINT` column stores the exact positive employee ID beside the normalized email.
- `POST /feedback` accepts optional multipart `submitter_employee_id`; it is required whenever no authenticated private `request.state.user_upn` exists and is ignored as authority when that private identity exists.
- Existing rollout/contract projections include only catalog entries whose `odoo_value` is not null, so Repair can never become an Odoo reference type.

- [ ] **Step 1: Write failing catalog and submitter tests**

```python
def test_feedback_catalog_has_exact_six_routes():
    assert [(item.label, item.group, item.behavior) for item in FEEDBACK_TYPES] == [
        ("Bug", "reporting", "coding"),
        ("New Feature", "reporting", "coding"),
        ("Floor Issue", "reporting", "review"),
        ("Floor Suggestion", "reporting", "review"),
        ("Repair", "ready", "external"),
        ("2s Improvement", "ready", "review"),
    ]

def test_timeclock_submitter_requires_one_exact_active_employee(monkeypatch):
    monkeypatch.setattr(odoo_client, "fetch_employee_statuses", lambda: [
        {"id": 41, "name": "Ana", "active": True, "work_email": "ana@gruberpallets.com"}
    ])
    assert resolve_timeclock(41).employee_id == 41
    with pytest.raises(SubmitterError):
        resolve_timeclock(99)

def test_feedback_schema_keeps_exact_submitter_employee_id():
    assert "submitter_employee_odoo_id BIGINT" in SCHEMA_DDL
```

- [ ] **Step 2: Run the focused Plant tests and verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_feedback_types.py tests/test_feedback_submitters.py tests/test_feedback_routes.py`
Expected: failures for the missing Repair/2s catalog entries and submitter resolver.

- [ ] **Step 3: Implement the catalog and server-side identity boundary**

Use this catalog shape and add the two ready choices:

```python
@dataclass(frozen=True)
class FeedbackType:
    value: str
    label: str
    description: str
    odoo_value: str | None
    group: Literal["reporting", "ready"]
    behavior: Literal["coding", "review", "external"]

FeedbackType("repair", "Repair", "Create a maintenance request", None, "ready", "external")
FeedbackType("two_s_improvement", "2s Improvement", "Work ready for the floor team", "2s Improvement", "ready", "review")
```

The route treats a present authenticated `request.state.user_upn` as private and
uses that identity regardless of a posted employee ID. When no authenticated UPN
exists, it requires and resolves the posted positive employee ID. It never trusts
`page_url` or a client boolean to choose the identity boundary. Save the normalized
work email and exact employee ID in the same local transaction. Repair is rejected
by `POST /feedback` because it must never create data.

- [ ] **Step 4: Run focused tests and the existing feedback schema tests**

Run: `.venv/bin/python -m pytest -q tests/test_feedback_types.py tests/test_feedback_submitters.py tests/test_feedback_routes.py tests/test_feedback_schema.py tests/test_feedback_store.py tests/test_feedback_rollout.py`
Expected: PASS, including invalid/duplicate/inactive employee cases and no Repair insert.

- [ ] **Step 5: Update `CHANGELOG.md`, commit, and review**

Add: `Plant Manager can now tell which review button was pressed and can safely identify the person sending it from the timeclock.`
Commit: `feat: validate light-bulb review submitters`

## Task 3: Render the six-button Plant chooser and Repair link

**Repository:** Plant Manager

**Files:**
- Modify: `src/zira_dashboard/templates/_feedback.html`
- Modify: `src/zira_dashboard/static/feedback.js`
- Modify: `src/zira_dashboard/static/feedback.css`
- Modify: `src/zira_dashboard/routes/feedback.py`
- Modify: `tests/test_timeclock_feedback_static.py`
- Create: `tests/test_feedback_chooser_static.py`
- Modify: `tests/test_feedback_routes.py`

**Interfaces:**
- `GET /api/feedback/submitters` returns `{ok: true, people: [{employee_id, name}]}` on timeclock pages.
- The template renders `FEEDBACK_TYPES` from Python; JavaScript contains no second label/type catalog.
- `data-behavior="external"` and `data-url` drive Repair without submitting the form.

- [ ] **Step 1: Write failing chooser and Repair tests**

```python
def test_chooser_renders_exact_groups_and_buttons(client):
    html = client.get("/").text
    assert "Reporting — the 2s board triages it" in html
    assert "Ready to create work — straight to the floor team" in html
    for label in ("Bug", "New Feature", "Floor Issue", "Floor Suggestion", "Repair", "2s Improvement"):
        assert html.count(f">{label}<") == 1

def test_repair_is_external_and_never_posts_feedback(client):
    html = client.get("/").text
    assert 'data-type="repair"' in html
    assert 'data-behavior="external"' in html
    assert 'https://www.gpimaintenance.com/request' in html
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_feedback_chooser_static.py tests/test_timeclock_feedback_static.py tests/test_feedback_routes.py`
Expected: missing group headings, Repair, 2s Improvement, and submitter picker assertions fail.

- [ ] **Step 3: Implement data-driven markup and behavior**

Render two catalog groups in Jinja. On a review type, show Description and the
existing screenshot control. On timeclock paths, fetch active choices, show the
exact label `Who is submitting this?`, and include `submitter_employee_id` in the
multipart request. On Repair, call `window.open(REPAIR_URL, "_blank", "noopener")`;
if it returns null, keep the modal open and reveal an ordinary anchor with the
same URL. Preserve Escape, focus trapping, opener focus restoration, idle timer
pause/resume, screenshot paste/removal, and accessible pressed states.

- [ ] **Step 4: Run focused tests and browser-level static checks**

Run: `.venv/bin/python -m pytest -q tests/test_feedback_chooser_static.py tests/test_timeclock_feedback_static.py tests/test_feedback_routes.py tests/test_feedback_image.py`
Expected: PASS. Also run `git diff --check`.

- [ ] **Step 5: Add the Plant What's New note and commit**

Add: `The light-bulb menu now shows all six clear choices. Repair opens the Maintenance request page, and floor reviews ask only for a description and optional picture.`
Commit: `feat: add the six-button light-bulb chooser`

## Task 4: Create, adopt, and link one Plant review task

**Repository:** Plant Manager

**Files:**
- Modify: `src/zira_dashboard/odoo_client.py`
- Modify: `src/zira_dashboard/odoo_improvements.py`
- Modify: `src/zira_dashboard/feedback_task_worker.py`
- Modify: `src/zira_dashboard/feedback_task_delivery.py`
- Modify: `src/zira_dashboard/feedback_sync.py`
- Modify: `src/zira_dashboard/feedback_projection.py`
- Modify: `tests/test_feedback_odoo.py`
- Modify: `tests/test_odoo_improvements.py`
- Modify: `tests/test_feedback_task_worker.py`
- Modify: `tests/test_feedback_task_delivery.py`
- Modify: `tests/test_feedback_sync.py`

**Interfaces:**
- `odoo_client.ensure_review_project() -> int` resolves exactly one active `GPI OS Manager - TASKS` project.
- `odoo_client.ensure_review_stage(project_id, "General" | "L10") -> int` resolves exactly one active associated stage.
- `ImprovementsClient.link_task_once(remote_id, task_id, *, feedback_id, expected_contract) -> None` permits only empty-to-exact compare-and-set.
- `feedback_task_worker.task_owner(snapshot) -> Literal["coding", "review"]` routes only the three review types to the review project.
- Review delivery is create-only after its task and optional image exist. It never pushes Plant's local lifecycle back over a review task; coding-task delivery keeps its current lifecycle behavior.

- [ ] **Step 1: Write failing idempotency and no-work-order tests**

```python
def test_review_delivery_creates_one_general_task_for_dale(monkeypatch, review_claim):
    result = worker.run_one(review_claim)
    assert result == "delivered"
    assert created_task["project"] == "GPI OS Manager - TASKS"
    assert created_task["stage"] == "General"
    assert created_task["assignee"] == "dale@gruberpallets.com"
    assert created_work_orders == []

def test_link_task_once_rejects_conflicting_existing_link(client):
    with pytest.raises(ContractError, match="conflicting linked task"):
        client.link_task_once(71, 902, feedback_id=17, expected_contract=V3)
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_feedback_odoo.py tests/test_odoo_improvements.py tests/test_feedback_task_worker.py tests/test_feedback_task_delivery.py tests/test_feedback_sync.py`
Expected: review routing and one-time link tests fail because V2 forbids the app-owned link.

- [ ] **Step 3: Implement exact lookup, creation, adoption, and linking**

For review types, use the immutable task name prefix
`[GPI-PM-FB-{feedback_id}] [{label}]`. Search by exact full name in the exact
project with `active_test=False`, limit 3. Zero matches permits one create; one
match is adopted; more than one blocks. Create with General stage, Dale's one
exact active Odoo user, open state, and the original escaped request description.
The description includes exact escaped lines `Source: GPI Plant Manager` and
`Source ID: GPI-PM-FB-{feedback_id}`; these identify the task independently of
its editable title.

The reference writer first reads `x_studio_linked_task`. Empty permits one write;
the same ID is success; a different ID or duplicate reference blocks. Read back
both records after the write. Never write `x_studio_linked_wo`.

Before creating anything, read the active Odoo selection values and require the
exact review type (`Physical - Issue`, `Physical - Suggestion`, or
`2s Improvement`). A missing value stops safely with a clear setup message; it
does not create a partial reference or task. Adding a missing selection value is
an ordinary no-code Odoo setting and is outside this implementation.

- [ ] **Step 4: Run focused tests and contract safety tests**

Run: `.venv/bin/python -m pytest -q tests/test_feedback_task_worker.py tests/test_feedback_task_delivery.py tests/test_feedback_sync.py tests/test_feedback_odoo_safety_contract.py tests/test_odoo_improvements.py`
Expected: PASS for retries after unknown create, exact adoption, duplicate quarantine,
same-link idempotency, conflicting-link refusal, and zero work-order calls.

- [ ] **Step 5: Add the Plant note and commit**

Add: `Each floor review now gets one shared Odoo task assigned to Dale. Retries find the same task instead of making another one.`
Commit: `feat: create one linked review task`

## Task 5: Reconcile review task state into the reference row

**Repository:** Plant Manager

**Files:**
- Create: `src/zira_dashboard/feedback_review_reconciler.py`
- Create: `tests/test_feedback_review_reconciler.py`
- Modify: `src/zira_dashboard/feedback_store.py`
- Modify: `src/zira_dashboard/feedback_projection.py`
- Modify: `src/zira_dashboard/odoo_client.py`
- Modify: `src/zira_dashboard/app.py`
- Modify: `src/zira_dashboard/routes/feedback_admin.py`
- Modify: `src/zira_dashboard/templates/admin_feedback.html`
- Modify: `tests/test_feedback_warmer.py`
- Modify: `tests/test_feedback_projection.py`
- Modify: `tests/test_feedback_admin_routes.py`

**Interfaces:**
- `feedback_store.review_reconcile_candidates(limit) -> list[ReviewCandidate]` returns review rows that have both exact local Odoo IDs, including rows whose remote linked-task field is still empty.
- `feedback_store.adopt_review_lifecycle(...)` records a verified task-to-reference result without enqueueing any task write or generic reference retry.
- `feedback_review_reconciler.run_batch(limit=50) -> ReconcileResult` runs every 60 seconds.
- `task_lifecycle(task, events) -> Requested | In-Progress | Declined | Completed | attention` is pure and exhaustively tested.

- [ ] **Step 1: Write failing lifecycle mapping tests**

```python
@pytest.mark.parametrize((state, action, expected), [
    ("01_in_progress", None, "Requested"),
    ("03_approved", "accept", "In-Progress"),
    ("03_approved", "assign", "In-Progress"),
    ("03_approved", "move_l10", "In-Progress"),
    ("1_canceled", "decline", "Declined"),
    ("1_done", "complete", "Completed"),
])
def test_task_lifecycle_requires_matching_event(state, action, expected):
    assert task_lifecycle(task(state), events(action)) == expected

def test_terminal_state_without_required_detail_needs_attention():
    assert task_lifecycle(task("1_done"), events("complete", detail="")) == "attention"

def test_admin_cannot_mutate_review_lifecycle(client, review_feedback):
    response = client.post(
        f"/admin/feedback/{review_feedback.id}/status",
        data={"status": "completed", "resolution_note": "local result"},
    )
    assert response.status_code == 422
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest -q tests/test_feedback_review_reconciler.py tests/test_feedback_projection.py tests/test_feedback_warmer.py`
Expected: import failure for the absent reconciler.

- [ ] **Step 3: Implement the pure mapper and 60-second worker**

Read the exact task with ID, active, project, stage, state, assignee, description,
and write date. Validate its immutable source marker and linked reference before
mapping. Decline/Complete use the latest valid matching event for reason/result,
UTC date, and employee. Write only status and applicable terminal fields; never
rewrite request text, image, source, source ID, submitter, type, or work-order link.
Terminal reference rows are never reopened. A mismatch records a fixed attention
code without storing request text or secrets.

For each candidate, read the exact task and exact reference, derive one projection,
first call `link_task_once` when the remote link is empty, then call the guarded
reference lifecycle write and immediately read the same reference back.
Only after that readback proves the requested lifecycle does Plant atomically save
the confirmed local lifecycle and align its generic reference-sync bookkeeping.
That local adoption never enqueues an outbound task update, preventing a
task-to-Plant-to-task loop. If the write outcome is unknown, re-read that exact
reference before retrying; never issue a blind second write.

The Plant admin feedback page labels review rows `Managed in Odoo`, links to the
one task when its ID is known, and renders no local Start, Complete, or Decline
forms for them. Its status POST rejects review types. This keeps the review task
as the only human work item while Bug and New Feature retain their current forms.

Register in `app.py`:

```python
async def _tick_feedback_review_reconcile():
    from . import feedback_review_reconciler
    await asyncio.to_thread(feedback_review_reconciler.run_batch)

("feedback review reconciliation", _tick_feedback_review_reconcile, 60),
```

- [ ] **Step 4: Run focused and lifecycle regression tests**

Run: `.venv/bin/python -m pytest -q tests/test_feedback_review_reconciler.py tests/test_feedback_projection.py tests/test_feedback_warmer.py tests/test_feedback_task_lifecycle_reconcile.py tests/test_feedback_sync.py tests/test_feedback_admin_routes.py`
Expected: PASS for all mappings, retries, no reopen, no unrelated field writes,
and the exact 60-second registration.

- [ ] **Step 5: Add the Plant note and commit**

Add: `The reference table now follows the shared review task. It normally catches up within one minute and safely retries when Odoo is busy.`
Commit: `feat: sync review tasks to the reference table`

## Task 6: Add Sales review metadata and the standard task-action endpoint

**Repository:** Sales Manager

**Files:**
- Create: `src/lib/improvements/review-metadata.ts`
- Create: `src/lib/improvements/review-metadata.test.ts`
- Create: `src/lib/improvements/review-action.ts`
- Create: `src/lib/improvements/review-action.test.ts`
- Create: `src/app/api/tasks/[id]/review/route.ts`
- Create: `src/app/api/tasks/[id]/review/route.test.ts`
- Modify: `src/lib/tasks/types.ts`
- Modify: `src/lib/tasks/feedback-metadata.ts`

**Interfaces:**
- `ImprovementReview` contains exact `referenceId`, `source`, `sourceId`, `type`, `referenceStatus`, `linkedTaskId`, `pending`, allowed actions, and opaque `taskVersion`. Both apps compute it as lowercase hexadecimal SHA-256 of UTF-8 canonical JSON with keys in this exact order: `writeDate`, `state`, `stageId`, `assigneeOdooUserIds`, `descriptionHtml`; assignee IDs are sorted ascending and all missing scalar values are `null`.
- `listImprovementReviews(taskIds: number[]): Promise<Map<number, ImprovementReview>>` uses one bounded batch query.
- `applyReviewAction(input: ReviewActionInput): Promise<ReviewActionResult>` writes only `project.task` and verifies exact readback.
- `POST /api/tasks/[id]/review` accepts `{action, detail?, targetOdooUserId?, taskVersion}`.

- [ ] **Step 1: Write failing metadata and action tests**

```ts
it("maps one exact Plant review reference by linked task", async () => {
  mockSearchRead([{ id: 71, x_studio_source: "GPI Plant Manager", x_studio_source_id: "GPI-PM-FB-17", x_studio_type: "2s Improvement", x_studio_status: "Requested", x_studio_linked_task: [902, "Review"] }]);
  expect(await listImprovementReviews([902])).toEqual(new Map([[902, expect.objectContaining({ linkedTaskId: 902, allowedActions: ["accept", "decline"] })]]));
});

it("writes Accept and its event in one task write then verifies readback", async () => {
  const result = await applyReviewAction(acceptInput);
  expect(odooWrite).toHaveBeenCalledTimes(1);
  expect(odooWrite).toHaveBeenCalledWith("project.task", 902, expect.objectContaining({ state: "03_approved", description: expect.stringContaining("GPI-REVIEW-EVENT-V1") }));
  expect(result.taskState).toBe("03_approved");
});
```

- [ ] **Step 2: Verify RED**

Run: `npm test -- --run src/lib/improvements/review-metadata.test.ts src/lib/improvements/review-action.test.ts 'src/app/api/tasks/[id]/review/route.test.ts'`
Expected: missing module/route failures.

- [ ] **Step 3: Implement strict metadata and server-only task actions**

Query `x_2s_improvements` by `x_studio_linked_task in taskIds`, source exactly
`GPI Plant Manager`, and the three review types. Reject duplicate references for
one task. Read the exact task directly from Odoo immediately before writing.
Require active task, exact linked ID, exact review project/stage, expected current
state, authenticated actor's exact Odoo user/employee IDs, and matching opaque
`taskVersion`. Recompute the version from a fresh direct Odoo read immediately
before the write; never trust client-supplied task fields.

Accept/Decline/Complete/Move L10 write state/stage plus the appended event in one
`odooWrite`. Assign writes owner plus event. Read back task ID, state, stage,
assignee, description, active, and write date. If readback does not prove the
requested result, return conflict. On timeout, read back before any retry. The
route returns 400 for invalid input, 401 for no session, 403 for unresolved actor,
404 for no exact review, and 409 for stale/conflicting state.

- [ ] **Step 4: Run focused tests and Sales type/lint checks**

Run: `npm test -- --run src/lib/improvements/review-metadata.test.ts src/lib/improvements/review-action.test.ts 'src/app/api/tasks/[id]/review/route.test.ts' src/lib/improvements/inbound.test.ts`
Run: `npm run typecheck`
Run: `npm run lint`
Expected: all exit 0.

- [ ] **Step 5: Add Sales What's New and commit**

Add: `Sales Manager can safely read the shared floor-review task and send its review choices through the normal Odoo task connection. The buttons remain hidden until OS Manager is ready.`
Commit: `feat: add Sales review task actions`

## Task 7: Show the review controls in Sales Manager

**Repository:** Sales Manager

**Files:**
- Create: `src/components/tasks/ImprovementReviewActions.tsx`
- Create: `src/components/tasks/ImprovementReviewActions.test.tsx`
- Modify: `src/components/tasks/TaskDetailsContent.tsx`
- Modify: `src/components/tasks/TaskDetailsContent.test.tsx`
- Modify: `src/lib/tasks/hooks.ts`

**Interfaces:**
- `ImprovementReviewActions` receives `{task, review, disabled, onChanged}`.
- Successful actions refetch task and review metadata before displaying success.
- The component emits no optimistic lifecycle state.

- [ ] **Step 1: Write failing component tests**

```tsx
it("shows only Accept and Decline for Requested review tasks", () => {
  render(<ImprovementReviewActions task={requestedTask} review={requestedReview} disabled={false} onChanged={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Accept" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Decline" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Complete" })).toBeNull();
});

it("requires a result before Complete", async () => {
  render(<ImprovementReviewActions task={acceptedTask} review={acceptedReview} disabled={false} onChanged={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "Complete" }));
  expect(screen.getByRole("button", { name: "Complete review" })).toBeDisabled();
});
```

- [ ] **Step 2: Verify RED**

Run: `npm test -- --run src/components/tasks/ImprovementReviewActions.test.tsx src/components/tasks/TaskDetailsContent.test.tsx`
Expected: missing component and controls.

- [ ] **Step 3: Implement the accessible action panel**

Requested renders Accept and Decline. Decline opens a labeled reason dialog.
Accepted renders Assign, Complete, and Move to L10. Complete opens a labeled
result dialog. Assign reuses the active Odoo user picker. Disable generic Status,
Complete, Decline, and owner controls for review tasks so there is only one action
path. Show `Reference update pending` while task state and reference status differ.
On 409, show `This review changed in Odoo. The latest state is shown.` and refetch.

- [ ] **Step 4: Run Sales UI checks**

Run: `npm test -- --run src/components/tasks/ImprovementReviewActions.test.tsx src/components/tasks/TaskDetailsContent.test.tsx src/lib/tasks/hooks.test.ts`
Run: `npm run typecheck`
Run: `npm run lint`
Run: `npm run build`
Expected: all exit 0; desktop and narrow-width component cases keep every button reachable.

- [ ] **Step 5: Add Sales note, commit, and keep the gate off**

Add: `Floor review tasks now have clear Accept, Decline, Assign, Complete, and Move to L10 controls. The matching reference may show that it is catching up for about one minute.`
Commit: `feat: show shared review actions in Sales`

## Task 8: Add OS review metadata and the standard task-action endpoint

**Repository:** OS Manager

**Files:**
- Create: `src/features/feedback/domain/reviewMetadata.ts`
- Create: `tests/unit/feedback/reviewMetadata.test.ts`
- Create: `src/features/feedback/services/reviewActionService.ts`
- Create: `tests/unit/feedback/reviewActionService.test.ts`
- Create: `src/app/api/tasks/[taskId]/review/route.ts`
- Create: `tests/unit/api/taskReviewRoute.test.ts`
- Modify: `src/integrations/odoo/OdooAdapter.ts`
- Modify: `src/integrations/odoo/types.ts`
- Modify: `src/integrations/odoo/odooJsonRpcAdapter.ts`

**Interfaces:**
- `listImprovementReviewsByTaskIds(taskIds: string[]): Promise<Map<string, ImprovementReview>>` uses the existing Odoo adapter and returns the same opaque `taskVersion` algorithm as Sales: lowercase hexadecimal SHA-256 over the Task 6 canonical JSON.
- `reviewActionService.apply(input): Promise<ReviewActionResult>` matches the Sales contract exactly.
- `POST /api/tasks/[taskId]/review` uses `requireCurrentActor()` and the same request/response shape and status codes as Sales.

- [ ] **Step 1: Write failing adapter, service, and route tests**

```ts
it("quarantines duplicate linked references", async () => {
  adapter.listImprovementReviewsByTaskIds.mockResolvedValue(twoRowsForTask("902"));
  await expect(service.load("902")).rejects.toThrow("more than one review reference");
});

it("moves an accepted review to L10 without changing its assignee", async () => {
  const result = await service.apply(moveL10Input);
  expect(adapter.updateTask).toHaveBeenCalledWith("902", expect.objectContaining({ stageId: "44", state: "03_approved" }));
  expect(result.stageLabel).toBe("L10");
});
```

- [ ] **Step 2: Verify RED**

Run: `npm test -- --run tests/unit/feedback/reviewMetadata.test.ts tests/unit/feedback/reviewActionService.test.ts tests/unit/api/taskReviewRoute.test.ts tests/unit/odoo/odooJsonRpcAdapter.test.ts`
Expected: missing adapter/service/route behavior.

- [ ] **Step 3: Implement the narrow adapter and action service**

Use `execute_kw` only on ordinary `project.task` reads/writes and read-only
`x_2s_improvements` metadata. Add no Odoo action or schema setup. Enforce the same
source/type/project/stage/state/actor/write-date/event/readback rules and exact
HTTP error mapping as Sales. Resolve General and L10 from the existing management
project configuration and require one exact active stage.

- [ ] **Step 4: Run OS focused and health checks**

Run: `npm test -- --run tests/unit/feedback/reviewMetadata.test.ts tests/unit/feedback/reviewActionService.test.ts tests/unit/api/taskReviewRoute.test.ts tests/unit/odoo/odooJsonRpcAdapter.test.ts tests/unit/odoo/odooJsonRpcMappers.test.ts`
Run: `npm run typecheck`
Run: `npm run lint`
Expected: all exit 0.

- [ ] **Step 5: Add OS What's New and commit**

Add: `OS Manager can now use the normal Odoo task connection for the shared floor-review choices. No special code is installed in Odoo.`
Commit: `feat: add OS review task actions`

## Task 9: Show the same review controls in OS Manager

**Repository:** OS Manager

**Files:**
- Create: `src/features/tasks/components/ImprovementReviewActions.tsx`
- Create: `tests/unit/tasks/ImprovementReviewActions.test.tsx`
- Modify: `src/features/tasks/domain/unifiedTask.ts`
- Modify: `src/features/tasks/components/TaskDetailPanel.tsx`
- Modify: `tests/unit/tasks/TaskDetailPanelSubtasks.test.tsx`
- Modify: `tests/unit/tasks/unifiedTask.test.ts`

**Interfaces:**
- `UnifiedOdooTaskRow` gains `improvementReview: ImprovementReview | null`.
- OS `ImprovementReviewActions` matches Sales labels, dialogs, pending copy, conflict copy, and action visibility.

- [ ] **Step 1: Write failing parity tests**

```tsx
it.each([
  ["Requested", ["Accept", "Decline"]],
  ["In-Progress", ["Assign", "Complete", "Move to L10"]],
])("shows the exact %s review actions", (status, labels) => {
  renderReview(status);
  for (const label of labels) expect(screen.getByRole("button", { name: label })).toBeVisible();
});
```

- [ ] **Step 2: Verify RED**

Run: `npm test -- --run tests/unit/tasks/ImprovementReviewActions.test.tsx tests/unit/tasks/TaskDetailPanelSubtasks.test.tsx tests/unit/tasks/unifiedTask.test.ts`
Expected: missing review presentation and controls.

- [ ] **Step 3: Implement controls without bypassing normal task sync**

Decorate ordinary Odoo task rows with exact review metadata. Render the action
panel in `TaskDetailPanel` and suppress generic lifecycle/assignee controls for
review tasks. POST the same payload as Sales, refresh the selected task and review
metadata after every result, and use the exact same pending/conflict copy. Keep
ordinary tasks, Rocks, and maintenance-feedback rows unchanged.

- [ ] **Step 4: Run OS UI and build checks**

Run: `npm test -- --run tests/unit/tasks/ImprovementReviewActions.test.tsx tests/unit/tasks/TaskDetailPanelSubtasks.test.tsx tests/unit/tasks/unifiedTask.test.ts tests/unit/tasks/TaskWorkspace.test.tsx`
Run: `npm run typecheck`
Run: `npm run lint`
Run: `npm run build`
Expected: all exit 0; no regression to Rocks, subtasks, recurring tasks, or maintenance feedback.

- [ ] **Step 5: Add OS note, commit, and keep the gate off**

Add: `The same floor-review buttons now appear in OS Manager, so the task works the same way in both manager apps.`
Commit: `feat: show shared review actions in OS`

## Task 10: Prove the cross-app workflow, enable it, and ship

**Repositories:** Plant Manager, Sales Manager, OS Manager

**Files:**
- Modify: Plant `CHANGELOG.md`
- Modify: Sales `src/lib/releases/notes.ts`
- Modify: Sales `src/lib/releases/notes.test.ts`
- Modify: OS `src/features/releases/releaseNotes.ts`
- Modify: OS `tests/unit/releases/releaseService.test.ts`
- Modify: Plant `src/zira_dashboard/feedback_types.py` to flip `REVIEW_WORKFLOW_ENABLED` from false to true
- Modify: Sales `src/lib/improvements/review-metadata.ts` to flip `REVIEW_WORKFLOW_ENABLED` from false to true
- Modify: OS `src/features/feedback/domain/reviewMetadata.ts` to flip `REVIEW_WORKFLOW_ENABLED` from false to true
- Modify: `docs/superpowers/specs/2026-09-02-simple-lightbulb-task-sync-design.md` status to `Implemented and verified`
- Modify: this plan's task checkboxes only after their evidence exists

**Interfaces:**
- A byte-for-byte comparison of the three V3 JSON files proves exact marker, actions, state mapping, labels, Repair URL, project/stages, and 60-second interval.
- Enabling V3 changes no Odoo configuration and invokes no Odoo code installation.

- [ ] **Step 1: Run the cross-app contract drift check**

Run these exact comparisons against the three execution worktrees:

```text
cmp /Users/dalegruber/Projects/gpi-plant-manager/.worktrees/odoo-owned-lightbulb-review/docs/odoo/contracts/2s-review-workflow-v3.json /Users/dalegruber/Projects/gpi-sales-manager/.worktrees/odoo-owned-lightbulb-review/docs/odoo/contracts/2s-review-workflow-v3.json
cmp /Users/dalegruber/Projects/gpi-plant-manager/.worktrees/odoo-owned-lightbulb-review/docs/odoo/contracts/2s-review-workflow-v3.json /Users/dalegruber/Projects/gpi-os-manager/.worktrees/odoo-owned-lightbulb-review/docs/odoo/contracts/2s-review-workflow-v3.json
```

Expected: both commands exit 0 with no output. Then run each Task 1 codec test;
each local test also proves its runtime constants match its checked-in JSON.

- [ ] **Step 2: Run every focused workflow suite**

Plant:

```text
.venv/bin/python -m pytest -q tests/test_feedback_review_events.py tests/test_feedback_submitters.py tests/test_feedback_chooser_static.py tests/test_feedback_routes.py tests/test_feedback_task_worker.py tests/test_feedback_task_delivery.py tests/test_feedback_review_reconciler.py tests/test_feedback_projection.py tests/test_feedback_sync.py tests/test_feedback_warmer.py
```

Sales:

```text
npm test -- --run src/lib/improvements/review-event.test.ts src/lib/improvements/review-metadata.test.ts src/lib/improvements/review-action.test.ts src/components/tasks/ImprovementReviewActions.test.tsx src/components/tasks/TaskDetailsContent.test.tsx 'src/app/api/tasks/[id]/review/route.test.ts'
```

OS:

```text
npm test -- --run tests/unit/feedback/reviewEvent.test.ts tests/unit/feedback/reviewMetadata.test.ts tests/unit/feedback/reviewActionService.test.ts tests/unit/api/taskReviewRoute.test.ts tests/unit/tasks/ImprovementReviewActions.test.tsx tests/unit/tasks/unifiedTask.test.ts
```

Expected: every command exits 0.

- [ ] **Step 3: Run full repository verification before enabling**

Plant: `.venv/bin/python -m pytest -q`
Sales: `npm run typecheck`, `npm run lint`, `npm test`, `npm run build`
OS: `npm run doctor`, `npm run prisma:generate`, `npm run typecheck`, `npm run lint`, `npm test`, `npm run build`
Expected: all commands exit 0. Any failure keeps every V3 gate off.

- [ ] **Step 4: Enable V3 in code and repeat affected smoke tests**

Set the three code-level gates to V3 only after Step 3. Do not require Dale to
set an environment variable. Re-run the cross-app drift check, each route test,
each action component test, and `git diff --check` in all three repositories.
Expected: all pass and no webhook, server action, custom Odoo module, database
copy, or credential value appears in the diffs.

- [ ] **Step 5: Commit, independently review, and push foundations before consumers**

Push in dependency order:

1. Plant contract/task creation/reconciler commits.
2. Sales metadata/actions/UI commits.
3. OS metadata/actions/UI commits.
4. The three small gate-enabling commits after all earlier pushes are on main.

Each push must fast-forward current `origin/main`, include its repository's
child-readable note, and pass the repository's pre-push hooks. Never force-push.

- [ ] **Step 6: Verify deployment and one disposable production review**

After all three Railway deployments are healthy, ask Dale for confirmation to
create one disposable production review record. Only after confirmation, submit one clearly titled
`2s Improvement` from Plant Manager. Verify:

1. Repair separately opens Maintenance and creates no Plant/Odoo records.
2. One reference row and one review task exist for the test source ID.
3. `x_studio_linked_wo` is empty and linked task equals the one task ID.
4. The same task appears in Sales and OS with Accept/Decline only.
5. Accept in one app appears in the other after refresh.
6. Assign retains In-Progress; Move to L10 retains assignee and approved state.
7. Complete requires a result and the reference becomes Completed within one minute.
8. The original request description and screenshot remain unchanged.

Use only that disposable test identity. Do not edit unrelated Odoo rows. If any
check fails, disable the V3 code gate in all apps and leave a fixed, non-secret
attention record.

- [ ] **Step 7: Close documentation and report exact evidence**

Set the new design status to `Implemented and verified` only after Step 6.
Record test counts, deployed commit SHAs, the disposable source ID, and the
observed reference delay without credentials, webhook values, or request text.
Do not mark the superseded Odoo-code plan complete.
