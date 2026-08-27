# Feedback Owner Task Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every new bug or feature request create exactly one Odoo task assigned to the app owner, while preserving local-first submission and the independent Improvements mirror.

**Architecture:** Each submission saves its local feedback row, the existing Improvements intent, and a separate task-delivery outbox row in one transaction. A 60-second worker leases due rows, finds or creates a uniquely named Odoo task for `ODOO_LOGIN`, attaches the normalized screenshot once, and persists a safe outcome.

**Tech Stack:** Python 3.11, FastAPI, PostgreSQL/psycopg2, Odoo XML-RPC, Jinja2, vanilla JavaScript, pytest, Ruff.

## Global Constraints

- Assign every Odoo task to `odoo_client.authenticate()` for `ODOO_LOGIN`, never to the submitter.
- Use the existing **Plant Manager** project and **Bug** / **Feature request** tags.
- Do not change `feedback_odoo_sync`, its `x_2s_improvements` target, or its exact write gates.
- A failed Odoo call must not make the submitter resubmit.
- Do not create tasks for historical feedback.
- Use `[GPI-PM-FB-<feedback id>]` in the remote task and screenshot name. One match is adopted; more than one blocks the record with no further remote writes.
- Keep the feedback lifecycle and Odoo task stage independent.
- Persist/display fixed safe errors only—never raw Odoo errors, credentials, or payloads.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/zira_dashboard/_schema.py` | Create an idempotent task-delivery outbox. |
| `src/zira_dashboard/feedback_task_delivery.py` | Claim leases, local snapshots, outcomes, retry timing, and admin status mapping. |
| `src/zira_dashboard/feedback_task_worker.py` | Idempotent Odoo task/screenshot delivery. |
| `src/zira_dashboard/feedback_content.py` | Shared safe page URL validation for the route and task description. |
| `src/zira_dashboard/feedback_store.py` | Atomically enqueue task delivery and select its owner-facing status. |
| `src/zira_dashboard/_odoo_feedback.py`, `src/zira_dashboard/odoo_client.py` | Bounded exact lookup helpers. |
| `src/zira_dashboard/app.py` | Run task delivery every 60 seconds. |
| `src/zira_dashboard/routes/feedback.py`, `src/zira_dashboard/static/feedback.js` | Keep submission local-only and explain it is queued. |
| `src/zira_dashboard/templates/admin_feedback.html` | Show super-admin delivery state. |
| `tests/test_feedback_odoo.py`, `tests/test_feedback_task_delivery.py`, `tests/test_feedback_task_worker.py` | Odoo helper, outbox, and worker contracts. |
| `tests/test_feedback_schema.py`, `tests/test_feedback_store.py`, `tests/test_feedback_routes.py`, `tests/test_feedback_admin_routes.py` | Schema, transaction, route, and UI regressions. |

### Task 1: Provide bounded exact Odoo identity lookups

**Files:**

- Modify: `src/zira_dashboard/_odoo_feedback.py`
- Modify: `src/zira_dashboard/odoo_client.py`
- Modify: `tests/test_feedback_odoo.py`

**Interfaces:**

- Produces `find_feedback_task_ids(project_id: int, name: str) -> list[int]`.
- Produces `find_feedback_attachment_ids(task_id: int, name: str) -> list[int]`.
- Leaves existing `find_feedback_task()` active-only for calendar/payroll callers.

- [x] **Step 1: Write failing lookup tests**

Using the current `_stub(monkeypatch)` fixture in `tests/test_feedback_odoo.py`, assert task lookup calls `project.task.search_read` with an exact project/name domain, `fields=['id']`, `order='id asc'`, `limit=2`, and `context={'active_test': False}`. Assert an attachment lookup calls `ir.attachment.search_read` with exactly `res_model='project.task'`, `res_id=901`, and `name='GPI-PM-FB-42-before.jpg'`, using the same two-record bound.

```python
assert odoo_client.find_feedback_task_ids(
    7, "[GPI-PM-FB-42] [Bug] Save fails"
) == [901]
assert odoo_client.find_feedback_attachment_ids(
    901, "GPI-PM-FB-42-before.jpg"
) == [18, 19]
```

- [x] **Step 2: Confirm failure**

Run: `pytest tests/test_feedback_odoo.py -q`

Expected: FAIL because the two facade functions do not exist.

- [x] **Step 3: Implement exact private helpers and facades**

Add this implementation to `_odoo_feedback.py`; preserve the current `find_feedback_task()` unchanged:

```python
def find_feedback_task_ids(execute_fn, project_id: int, name: str) -> list[int]:
    rows = execute_fn(
        "project.task", "search_read",
        [("project_id", "=", project_id), ("name", "=", name)],
        fields=["id"], order="id asc", limit=2,
        context={"active_test": False},
    ) or []
    return [int(row["id"]) for row in rows]


def find_feedback_attachment_ids(execute_fn, task_id: int, name: str) -> list[int]:
    rows = execute_fn(
        "ir.attachment", "search_read",
        [("res_model", "=", "project.task"), ("res_id", "=", task_id),
         ("name", "=", name)],
        fields=["id"], order="id asc", limit=2,
        context={"active_test": False},
    ) or []
    return [int(row["id"]) for row in rows]
```

Expose the same two signatures from `odoo_client.py` by delegating to the private functions with `execute`.

- [x] **Step 4: Verify and commit**

Run:

```bash
pytest tests/test_feedback_odoo.py -q
ruff check src/zira_dashboard/_odoo_feedback.py src/zira_dashboard/odoo_client.py tests/test_feedback_odoo.py
```

Expected: PASS with no Ruff findings.

Commit:

```bash
git add src/zira_dashboard/_odoo_feedback.py src/zira_dashboard/odoo_client.py tests/test_feedback_odoo.py
git commit -m "feat: add feedback task identity lookups"
```

### Task 2: Persist an independent durable owner-task request

**Files:**

- Modify: `src/zira_dashboard/_schema.py`
- Create: `src/zira_dashboard/feedback_task_delivery.py`
- Modify: `src/zira_dashboard/feedback_store.py`
- Modify: `tests/test_feedback_schema.py`
- Modify: `tests/test_feedback_store.py`
- Create: `tests/test_feedback_task_delivery.py`

**Interfaces:**

- Produces `TaskDeliveryClaim` and `FeedbackTaskSnapshot` data classes.
- Produces `enqueue_submission`, `claim_due`, `load_snapshot`, `record_task_id`, `record_before_attachment`, `mark_delivered`, `schedule_retry`, and `block`.
- `create_submission()` consumes `enqueue_submission(cur, feedback_id)` in its current transaction.

- [x] **Step 1: Add failing schema and transaction tests**

Add this contract in `tests/test_feedback_schema.py`:

```python
def test_schema_has_owner_task_delivery_outbox():
    ddl = " ".join(SCHEMA_DDL.split())
    assert "CREATE TABLE IF NOT EXISTS feedback_task_delivery" in ddl
    assert "feedback_id BIGINT PRIMARY KEY REFERENCES feedback(id)" in ddl
    assert "state TEXT NOT NULL DEFAULT 'pending'" in ddl
    assert "state IN ('pending', 'in_flight', 'attention', 'delivered', 'blocked')" in ddl
    assert "claim_token UUID" in ddl
    assert "odoo_task_id BIGINT" in ddl
    assert "before_attachment_id BIGINT" in ddl
```

Extend both existing `create_submission` recording-cursor tests. They must assert an `INSERT INTO feedback_task_delivery` appears in the same transaction after the existing `feedback_odoo_sync` intent, with `(42,)` parameters.

- [x] **Step 2: Confirm failure**

Run: `pytest tests/test_feedback_schema.py tests/test_feedback_store.py -q`

Expected: FAIL because submission does not create a task-delivery row.

- [x] **Step 3: Add the idempotent schema**

Add this DDL immediately after `feedback_odoo_sync`:

```sql
CREATE TABLE IF NOT EXISTS feedback_task_delivery (
  feedback_id BIGINT PRIMARY KEY REFERENCES feedback(id),
  state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'in_flight', 'attention', 'delivered', 'blocked')),
  due_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  claim_owner TEXT,
  claim_token UUID,
  claim_expires_at TIMESTAMPTZ,
  odoo_task_id BIGINT,
  before_attachment_id BIGINT,
  last_error_summary TEXT,
  blocked_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK ((state = 'in_flight') = (claim_token IS NOT NULL AND claim_expires_at IS NOT NULL)),
  CHECK (state <> 'delivered' OR odoo_task_id IS NOT NULL),
  CHECK (state <> 'blocked' OR blocked_reason IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS feedback_task_delivery_due_idx
  ON feedback_task_delivery (due_at, feedback_id)
  WHERE state IN ('pending', 'attention');
```

- [x] **Step 4: Implement the focused outbox module**

Create `feedback_task_delivery.py` with no FastAPI or Odoo import:

```python
@dataclass(frozen=True)
class TaskDeliveryClaim:
    feedback_id: int
    claim_token: UUID
    task_id: int | None
    before_attachment_id: int | None

@dataclass(frozen=True)
class FeedbackTaskSnapshot:
    feedback_id: int
    task_type: Literal["bug", "feature"]
    message: str
    submitter: str | None
    page_url: str | None
    before_image: NormalizedImage | None
```

`enqueue_submission()` executes only:

```python
cur.execute(
    "INSERT INTO feedback_task_delivery (feedback_id, state, due_at) "
    "VALUES (%s, 'pending', now())", (feedback_id,),
)
```

`claim_due(now, worker_id, limit=10)` caps at 10, selects due `pending`/`attention` plus expired `in_flight` rows using `FOR UPDATE SKIP LOCKED`, and sets a fresh UUID, worker ID, two-minute lease, incremented attempt count, and `in_flight` state. Every later mutation must predicate on both `feedback_id` and `claim_token`.

`schedule_retry()` clears the lease, records only `Odoo task delivery needs attention and will retry.`, sets `attention`, and schedules `min(60 * 2 ** min(attempt_count, 6), 3600)` seconds later. At attempt count 6 and above, the retry waits one hour. `block()` clears the lease, sets `blocked`, writes its fixed reason, and sets no new due time. `mark_delivered()` requires a task ID and requires either no before image or a stored attachment ID.

`load_snapshot()` joins feedback to its `before` image, requires local lifecycle and task type `bug`/`feature`, validates all image metadata/bytes using the same validation policy as `feedback_store`, and returns detached immutable data. It must not read or mutate `feedback_odoo_sync`.

Implement `admin_status_for()` with an exact allowlist: `pending`/`in_flight` return `("Queued for app owner", None)`; `attention` returns `("Needs attention", "Odoo task delivery needs attention and will retry.")`; `delivered` returns `("Assigned to app owner", None)`; `blocked` returns `("Needs attention", "Task delivery needs owner review.")`; and a missing or malformed state returns `("Needs attention", "Task delivery record is missing.")`. This prevents arbitrary database text from reaching the template.

- [x] **Step 5: Hook it into existing atomic submission**

In `feedback_store.create_submission()`, import the new module and call this directly after the existing `feedback_odoo_sync` insert:

```python
feedback_task_delivery.enqueue_submission(cur, feedback_id)
```

No Odoo client call belongs in `feedback_store` or the request path.

- [x] **Step 6: Write focused outbox tests**

Create `tests/test_feedback_task_delivery.py` using the recording-cursor/context-manager pattern already in `tests/test_feedback_store.py`. Implement these concrete cases:

- `test_claim_due_uses_skip_locked_and_returns_two_minute_lease`: return one valid database row, call `claim_due()` with `2026-08-26T12:00:00Z`, assert the SQL includes `FOR UPDATE SKIP LOCKED`, the update uses a UUID token and `2026-08-26T12:02:00Z`, and the returned claim carries feedback ID 42.
- `test_schedule_retry_clears_lease_and_caps_backoff_at_one_hour`: use a claim with attempt count 8, call `schedule_retry()`, and assert state `attention`, null lease columns, fixed safe summary, and a due time exactly one hour later.
- `test_mark_delivered_requires_current_claim_token`: make the guarded update return no row and assert the function raises its state-transition error rather than reporting delivery.
- `test_block_does_not_schedule_another_attempt`: call `block()` and assert `state='blocked'`, a null claim, the fixed ambiguity reason, and no `due_at` update.
- `test_load_snapshot_rejects_nonlocal_and_malformed_rows`: supply respectively a legacy-origin row and an image whose stored byte count differs from `byte_length`; assert both raise the snapshot-validation exception before the worker can call Odoo.

Every state-change SQL assertion must include both `feedback_id` and `claim_token`.

- [x] **Step 7: Verify and commit**

Run:

```bash
pytest tests/test_feedback_schema.py tests/test_feedback_store.py tests/test_feedback_task_delivery.py -q
ruff check src/zira_dashboard/_schema.py src/zira_dashboard/feedback_store.py src/zira_dashboard/feedback_task_delivery.py tests/test_feedback_schema.py tests/test_feedback_store.py tests/test_feedback_task_delivery.py
```

Expected: PASS with no Ruff findings.

Commit:

```bash
git add src/zira_dashboard/_schema.py src/zira_dashboard/feedback_store.py src/zira_dashboard/feedback_task_delivery.py tests/test_feedback_schema.py tests/test_feedback_store.py tests/test_feedback_task_delivery.py
git commit -m "feat: queue owner feedback tasks locally"
```

### Task 3: Deliver one assigned Odoo task and screenshot safely

**Files:**

- Create: `src/zira_dashboard/feedback_task_worker.py`
- Create: `src/zira_dashboard/feedback_content.py`
- Modify: `src/zira_dashboard/app.py`
- Create: `tests/test_feedback_task_worker.py`

**Interfaces:**

- Consumes the Task 1 Odoo helpers and Task 2 outbox functions.
- Produces `run_batch(now=None, worker_id=None, limit=10) -> BatchResult`.
- Produces pure `task_name(snapshot)`, `task_description(snapshot)`, and `before_attachment_name(feedback_id)` helpers.

- [x] **Step 1: Write failing worker tests**

Stub delivery functions and normal `odoo_client` methods—never call live Odoo. Add tests for creation, same-owner feature assignment, adoption of one matching task, blocking two matching tasks, creation/adoption/blocking of before-image attachments, a transport retry, and HTML escaping.

The core owner-assignment assertion is:

```python
create.assert_called_once_with(
    project_id=3,
    name="[GPI-PM-FB-42] [Bug] Save fails",
    description_html=ANY,
    assignee_uid=9,
    tag_id=4,
    deadline="2026-08-26",
)
```

The before-image assertion is `add_task_attachment(55, 'GPI-PM-FB-42-before.jpg', 'image/jpeg', b'jpeg')`.

- [x] **Step 2: Confirm failure**

Run: `pytest tests/test_feedback_task_worker.py -q`

Expected: FAIL because the module does not exist.

- [x] **Step 3: Implement deterministic task content**

Implement:

```python
def task_name(snapshot: FeedbackTaskSnapshot) -> str:
    label = "Feature" if snapshot.task_type == "feature" else "Bug"
    first = snapshot.message.strip().splitlines()[0] or "feedback"
    if len(first) > 70:
        first = first[:69].rstrip() + "…"
    return f"[GPI-PM-FB-{snapshot.feedback_id}] [{label}] {first}"

def before_attachment_name(feedback_id: int) -> str:
    return f"GPI-PM-FB-{feedback_id}-before.jpg"
```

Create `feedback_content.py` with `safe_page_url(value: str | None) -> str | None`, moving the current route validation unchanged: allow only `http`/`https` absolute URLs and single-slash relative paths, reject protocol-relative and all other schemes. Import it in both `routes/feedback.py` and the worker. `task_description()` must use `html.escape()`, `<br>` line breaks, source app, submitter, the helper-validated page link, and feedback ID; the worker must not import a route module.

- [x] **Step 4: Implement exact adoption/create behavior**

`process_claim()` must first call `find_feedback_task_ids(project_id, task_name)`. For zero matches it calls `create_feedback_task()` using `ensure_feedback_project()`, owner `authenticate()`, the appropriate tag, and `_local_today().isoformat()`. For one match it adopts that ID; for two it calls `block(claim, 'More than one matching owner task exists.', now)` with no create.

Persist the task ID immediately. For a before image, repeat the same zero/one/two matching policy with its identifier-derived attachment name. Only call `mark_delivered()` after task and attachment state are durable.

For a create/attachment `TimeoutError`, `ConnectionError`, `OSError`, or `xmlrpc.client.Error`, repeat the exact lookup once before retrying. This adopts remote writes with lost responses. For expected Odoo configuration/auth/transport failures call `schedule_retry(claim, 'Odoo task delivery needs attention and will retry.', now)` and log detail only server-side. Isolate each claim and return `BatchResult(attempted, delivered, retried, blocked, isolated_errors)`.

- [x] **Step 5: Register a separate worker tick**

Add this next to `_tick_feedback_sync()`:

```python
async def _tick_feedback_task_delivery():
    """Create due local-feedback Odoo tasks for the app owner."""
    from . import feedback_task_worker
    await asyncio.to_thread(feedback_task_worker.run_batch)
```

Add `('feedback owner task delivery', _tick_feedback_task_delivery, 60)` to `_WARMERS`. Keep the existing `feedback Odoo mirror` entry unchanged.

- [x] **Step 6: Verify and commit**

Run:

```bash
pytest tests/test_feedback_task_worker.py tests/test_feedback_odoo.py tests/test_feedback_store.py -q
ruff check src/zira_dashboard/feedback_task_worker.py src/zira_dashboard/app.py tests/test_feedback_task_worker.py
```

Expected: PASS with no Ruff findings.

Commit:

```bash
git add src/zira_dashboard/feedback_task_worker.py src/zira_dashboard/app.py tests/test_feedback_task_worker.py
git commit -m "feat: deliver feedback tasks to app owner"
```

### Task 4: Surface queued and delivered state safely

**Files:**

- Modify: `src/zira_dashboard/feedback_store.py`
- Modify: `src/zira_dashboard/routes/feedback.py`
- Modify: `src/zira_dashboard/templates/admin_feedback.html`
- Modify: `src/zira_dashboard/static/feedback.js`
- Modify: `tests/test_feedback_routes.py`
- Modify: `tests/test_feedback_admin_routes.py`
- Modify: `CHANGELOG.md`

**Interfaces:**

- Consumes `admin_status_for(row) -> tuple[str, str | None]` from the Task 2 module.
- Produces `task_delivery_label` and `task_delivery_note` on every `for_admin()` row.
- Produces a submission response `{ok: True, id: int, task_delivery: 'queued'}`.

- [x] **Step 1: Write failing response and template tests**

In `tests/test_feedback_routes.py`, change the happy path to:

```python
assert response.json() == {"ok": True, "id": 12, "task_delivery": "queued"}
```

Keep `_fail_if_odoo_is_called(monkeypatch)`. In `tests/test_feedback_admin_routes.py`, add representative delivery fields and assert `Queued for app owner`, `Assigned to app owner`, `Needs attention`, and the fixed safe retry message are rendered. Add a source assertion that `feedback.js` says `Thanks — saved and sending it to the app owner.`.

- [x] **Step 2: Confirm failure**

Run: `pytest tests/test_feedback_routes.py tests/test_feedback_admin_routes.py -q`

Expected: FAIL because delivery API/UI data and copy do not exist.

- [x] **Step 3: Add safe admin read data and template output**

Extend `feedback_store.for_admin()` with a left join to `feedback_task_delivery td`, retaining the current Improvements join. Select task delivery state, task ID, attachment ID, safe error, and block reason, then pass those selected fields to `feedback_task_delivery.admin_status_for(row)`. Its Task 2 allowlist supplies `task_delivery_label` and `task_delivery_note`; do not render any selected error/reason field directly.

Add this autoescaped template block before the current shared-mirror Sync metadata:

```jinja2
<div>
  <dt>Owner task</dt>
  <dd>{{ item.task_delivery_label }}{% if item.task_delivery_note %}<br><span class="muted">{{ item.task_delivery_note }}</span>{% endif %}</dd>
</div>
```

Do not add `|safe`.

- [x] **Step 4: Keep submitter flow local-only and update copy**

Change only the successful route response:

```python
return JSONResponse({"ok": True, "id": new_id, "task_delivery": "queued"})
```

Keep its validation/image/persistence order and Odoo-free request path. Change only the JavaScript success copy:

```javascript
if (status) status.textContent = 'Thanks — saved and sending it to the app owner.';
```

Replace the existing planning-only `2026-08-26` changelog bullet with:

```markdown
- **New bug reports and ideas now make a task for the app owner.** If Odoo is busy, Plant Manager saves the report and tries again, so the person who sent it does not have to start over.
```

- [x] **Step 5: Run final validation and update checkboxes**

Run:

```bash
pytest tests/test_feedback_odoo.py tests/test_feedback_schema.py tests/test_feedback_store.py tests/test_feedback_task_delivery.py tests/test_feedback_task_worker.py tests/test_feedback_routes.py tests/test_feedback_mine_route.py tests/test_feedback_admin_routes.py -q
ruff check src/zira_dashboard tests
pytest -q
git diff --check
```

Expected: PASS with only documented skips. Never run DB-backed tests against a Railway database; `tests/conftest.py` aborts intentionally.

Mark this plan's completed checkboxes. Preserve unrelated worktree changes.

Validation completed with the focused feedback suite, repository-wide Ruff,
`git diff --check`, and an elevated full-suite run. The elevated run reported
3,923 passed and 399 skipped.

- [x] **Step 6: Commit the isolated feature branch**

Commit the planned files on the isolated feature branch:

```bash
git add CHANGELOG.md docs/superpowers/plans/2026-08-26-feedback-owner-task-delivery.md src/zira_dashboard/feedback_content.py src/zira_dashboard/feedback_store.py src/zira_dashboard/routes/feedback.py src/zira_dashboard/static/feedback.js src/zira_dashboard/templates/admin_feedback.html tests/test_feedback_routes.py tests/test_feedback_admin_routes.py
git commit -m "feat: show feedback owner task delivery"
```

The implementation commit is `2ed2b673 feat: show feedback owner task delivery`.

- [ ] **Step 7: Merge and push handoff**

An integration owner must first merge the isolated feature branch into the
current `main`, then push the resulting `main` commit. If `origin/main` has
advanced, fetch, rebase with `--autostash`, verify unrelated changes are
restored, rerun `git diff --check`, and push without force. This handoff is not
complete merely because the feature-branch commit and its validation are
complete.
