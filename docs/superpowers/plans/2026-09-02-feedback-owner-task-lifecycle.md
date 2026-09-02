# Feedback Owner Task Lifecycle Synchronization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep each locally managed feedback record, its Odoo 2s Improvement, and its exact Odoo owner task synchronized through Requested, In Progress, Completed, and Declined.

**Architecture:** Extend `feedback_task_delivery` with desired and verified lifecycle versions. Local transitions atomically advance both Odoo intents; the existing task worker reconciles the stored task relationship with idempotent stage/note writes and readback. A bounded reconciliation queues existing mismatches such as feedback 44/task 3755 through the same worker.

**Tech Stack:** Python 3, PostgreSQL, FastAPI, psycopg2, Odoo XML-RPC, pytest.

## Global Constraints

- Plant Manager's local feedback row remains lifecycle authority.
- Never trust a pasted Task ID; verify the stored delivery relationship, project, and identifier-bearing name.
- Requested maps to New; In Progress maps to In Progress; Completed and Declined map to Done.
- Completed posts one result note; Declined posts one clearly labeled decline note.
- Local transitions never wait for Odoo and atomically advance both sync intents.
- Recover unknown write outcomes through readback before another write.
- Never create, delete, archive, or merge an Odoo Improvement row.
- Do not claim completion until both Odoo copies verify the same local version.

---

### Task 1: Persist owner-task lifecycle intent

**Files:**
- Modify: `src/zira_dashboard/_schema.py`
- Modify: `src/zira_dashboard/feedback_task_delivery.py`
- Test: `tests/test_feedback_schema.py`
- Test: `tests/test_feedback_task_delivery.py`

**Interfaces:**
- Produces: `TaskDeliveryClaim.desired_version`, `desired_status`, `last_synced_version`.
- Produces: `enqueue_submission(cur, feedback_id, *, desired_version, desired_status)`.
- Produces: `enqueue_lifecycle(cur, feedback_id, *, desired_version, desired_status, now)`.

- [ ] **Step 1: Write failing schema and persistence tests**

Assert the schema and claim contain positive `desired_version`, nonnegative
`last_synced_version`, allowlisted `desired_status`, and
`last_synced_version <= desired_version`. Add:

```python
def test_lifecycle_enqueue_advances_existing_intent(monkeypatch):
    cursor = use_cursor(monkeypatch, [{"feedback_id": 42}])
    delivery.enqueue_lifecycle(
        cursor, 42, desired_version=3, desired_status="completed", now=NOW
    )
    sql, params = cursor.calls[0]
    assert "desired_version = %s" in sql
    assert "desired_status = %s" in sql
    assert "state = CASE WHEN state = 'in_flight' THEN state ELSE 'pending' END" in sql
    assert params == (3, "completed", NOW, NOW, 42)
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/pytest -q tests/test_feedback_schema.py tests/test_feedback_task_delivery.py`

Expected: missing lifecycle columns and APIs fail.

- [ ] **Step 3: Implement the minimal persistence contract**

Add table columns and idempotent `ALTER TABLE` statements equivalent to:

```sql
desired_version BIGINT NOT NULL DEFAULT 1 CHECK (desired_version > 0),
last_synced_version BIGINT NOT NULL DEFAULT 0 CHECK (last_synced_version >= 0),
desired_status TEXT NOT NULL DEFAULT 'requested'
  CHECK (desired_status IN ('requested','in_progress','completed','declined')),
CHECK (last_synced_version <= desired_version)
```

Extend claim SELECT/RETURNING/predicates with all three fields. Make lifecycle
enqueue retain an in-flight claim; otherwise set pending, due now, and clear
safe error/block fields without changing task or attachment IDs.

- [ ] **Step 4: Run GREEN and commit**

Run: `.venv/bin/pytest -q tests/test_feedback_schema.py tests/test_feedback_task_delivery.py`

```bash
git add src/zira_dashboard/_schema.py src/zira_dashboard/feedback_task_delivery.py tests/test_feedback_schema.py tests/test_feedback_task_delivery.py
git commit -m "feat: persist feedback task lifecycle intent"
git push origin main
```

### Task 2: Queue both Odoo copies atomically

**Files:**
- Modify: `src/zira_dashboard/feedback_store.py`
- Test: `tests/test_feedback_store.py`
- Test: `tests/test_feedback_admin_routes.py`

**Interfaces:**
- Consumes: `enqueue_lifecycle` from Task 1.
- Produces: `feedback_store.transition` advancing both intents to one version.

- [ ] **Step 1: Write failing transition tests**

Cover Requested -> In Progress and In Progress -> Completed/Declined for every
canonical feedback type. Assert the final two statements advance Improvement
and task intents with the same version. Script a missing task-delivery row and
assert `InvalidTransition`, proving the transaction cannot commit half a change.

- [ ] **Step 2: Run RED**

Run: `.venv/bin/pytest -q tests/test_feedback_store.py tests/test_feedback_admin_routes.py`

Expected: tests fail because task intent is not advanced.

- [ ] **Step 3: Add task enqueue to the existing transaction**

```python
feedback_task_delivery.enqueue_lifecycle(
    cur, feedback_id, desired_version=version, desired_status=status, now=now
)
```

Pass version 1/requested explicitly from `create_submission`.

- [ ] **Step 4: Run GREEN and commit**

Run: `.venv/bin/pytest -q tests/test_feedback_store.py tests/test_feedback_admin_routes.py tests/test_feedback_routes.py`

```bash
git add src/zira_dashboard/feedback_store.py tests/test_feedback_store.py tests/test_feedback_admin_routes.py tests/test_feedback_routes.py
git commit -m "feat: queue both feedback lifecycle copies"
git push origin main
```

### Task 3: Add exact Odoo task-stage helpers

**Files:**
- Modify: `src/zira_dashboard/_odoo_feedback.py`
- Modify: `src/zira_dashboard/odoo_client.py`
- Test: `tests/test_feedback_odoo.py`

**Interfaces:**
- Produces: `find_feedback_stage_ids(project_id, name) -> list[int]`.
- Produces: `read_feedback_task(task_id) -> dict | None`, including identity, active, and stage.

- [ ] **Step 1: Write failing bounded lookup tests**

Require exact project/name stage lookup, `order="id asc"`, and `limit=2`.
Require task readback fields `id`, `name`, `project_id`, `active`, `stage_id`.
Reject booleans, extra rows, wrong IDs, malformed relations, and bad names.

- [ ] **Step 2: Run RED**

Run: `.venv/bin/pytest -q tests/test_feedback_odoo.py`

Expected: missing helper failures.

- [ ] **Step 3: Implement strict helpers and facade wrappers**

Use `search_read`; validate every result. The lifecycle path never calls
`ensure_feedback_stages` and never creates a stage.

- [ ] **Step 4: Run GREEN and commit**

Run: `.venv/bin/pytest -q tests/test_feedback_odoo.py tests/test_odoo_task_helpers.py`

```bash
git add src/zira_dashboard/_odoo_feedback.py src/zira_dashboard/odoo_client.py tests/test_feedback_odoo.py
git commit -m "feat: verify feedback task lifecycle targets"
git push origin main
```

### Task 4: Reconcile stages and terminal notes idempotently

**Files:**
- Modify: `src/zira_dashboard/feedback_task_delivery.py`
- Modify: `src/zira_dashboard/feedback_task_worker.py`
- Test: `tests/test_feedback_task_delivery.py`
- Test: `tests/test_feedback_task_worker.py`

**Interfaces:**
- Extends: `FeedbackTaskSnapshot` with `status`, `projection_version`, `resolution_note`.
- Produces: `task_stage_for(status)`, `terminal_note_marker(feedback_id, version)`, and `terminal_note_html(snapshot)`.
- Produces: settlement that advances the claimed version and requeues newer intent.

- [ ] **Step 1: Write mapping and identity RED tests**

Cover every status/type. For completed work assert one exact Done stage lookup,
one `update_task(task_id, stage_id=...)`, one note, and settlement. Add no-write
tests for missing/duplicate stages, wrong task/project/name, inactive tasks, and
an already-correct stage.

- [ ] **Step 2: Write note and timeout RED tests**

Completed HTML begins `Completed:` and Declined HTML begins `Declined:`. Both
contain `<!-- GPI-PM-FB-42:v3 -->`. A found marker skips posting. After a post
timeout, recheck the marker and settle or retry without a second post.

- [ ] **Step 3: Run RED**

Run: `.venv/bin/pytest -q tests/test_feedback_task_delivery.py tests/test_feedback_task_worker.py`

Expected: missing reconciliation behavior fails.

- [ ] **Step 4: Implement the worker flow**

After creation/attachment delivery, verify the exact stored task, resolve one
target stage, renew the lease, write only if needed, ensure the terminal marker
once, and read back stage plus marker. Retry transport errors and use only fixed
allowlisted block reasons for ambiguity. Settle only the claim version; leave
pending when a newer desired version arrived in flight.

- [ ] **Step 5: Run GREEN and commit**

Run: `.venv/bin/pytest -q tests/test_feedback_task_delivery.py tests/test_feedback_task_worker.py tests/test_feedback_odoo.py`

```bash
git add src/zira_dashboard/feedback_task_delivery.py src/zira_dashboard/feedback_task_worker.py tests/test_feedback_task_delivery.py tests/test_feedback_task_worker.py
git commit -m "feat: sync owner task lifecycle stages"
git push origin main
```

### Task 5: Queue existing mismatches safely

**Files:**
- Create: `scripts/reconcile_feedback_task_lifecycle.py`
- Create: `tests/test_feedback_task_lifecycle_reconcile.py`

**Interfaces:**
- Produces: bounded dry-run-by-default reconciliation with `--yes`.

- [ ] **Step 1: Write reconciliation RED tests**

Preview returns safe counts without mutation. Apply queues at most 100 locally
authoritative rows whose delivered stored task lags feedback. Exclude null
legacy lifecycle, missing delivery, blocked ambiguity, and missing task ID.

- [ ] **Step 2: Run RED**

Run: `.venv/bin/pytest -q tests/test_feedback_task_lifecycle_reconcile.py`

Expected: module import fails.

- [ ] **Step 3: Implement bounded idempotent reconciliation**

Lock candidates ordered by feedback ID with `FOR UPDATE SKIP LOCKED LIMIT 100`.
Apply desired version/status, pending/due-now state, and retain remote IDs.
Output only `eligible`, `queued`, and `applied` counts.

- [ ] **Step 4: Run GREEN and commit**

Run: `.venv/bin/pytest -q tests/test_feedback_task_lifecycle_reconcile.py tests/test_feedback_task_delivery.py`

```bash
git add scripts/reconcile_feedback_task_lifecycle.py tests/test_feedback_task_lifecycle_reconcile.py
git commit -m "feat: reconcile existing feedback owner tasks"
git push origin main
```

### Task 6: Report both sync states safely

**Files:**
- Modify: `scripts/feedback_lifecycle.py`
- Modify: `src/zira_dashboard/feedback_store.py`
- Modify: `src/zira_dashboard/templates/admin_feedback.html`
- Test: `tests/test_feedback_lifecycle_scripts.py`
- Test: `tests/test_feedback_admin_routes.py`

**Interfaces:**
- Produces: safe command fields `task_sync_state`, `proposed_task_stage`, `task_queued`.
- Produces: separate Improvement and owner-task status on the admin page.

- [ ] **Step 1: Write command/UI RED tests**

Start previews In Progress; finish previews Done; applied transitions report
queued. Terminal task lag displays `Task update pending`. Assert no task title,
note, credentials, raw exception, or Odoo payload appears in command output.

- [ ] **Step 2: Run RED**

Run: `.venv/bin/pytest -q tests/test_feedback_lifecycle_scripts.py tests/test_feedback_admin_routes.py`

Expected: missing task status output fails.

- [ ] **Step 3: Add safe read models and fixed labels**

Join desired/verified task versions into `lifecycle_state` and `for_admin`.
Keep `admin_status_for` closed to fixed messages. Render a separate Owner task
sync line and do not call a terminal card fully synchronized while either lags.

- [ ] **Step 4: Run GREEN and commit**

Run: `.venv/bin/pytest -q tests/test_feedback_lifecycle_scripts.py tests/test_feedback_admin_routes.py tests/test_feedback_store.py`

```bash
git add scripts/feedback_lifecycle.py src/zira_dashboard/feedback_store.py src/zira_dashboard/templates/admin_feedback.html tests/test_feedback_lifecycle_scripts.py tests/test_feedback_admin_routes.py
git commit -m "feat: report feedback task sync status"
git push origin main
```

### Task 7: Verify and repair feedback 44 through the generalized path

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: deployed worker, feedback `GPI-PM-FB-44`, stored task relationship.
- Produces: Improvement Completed, task 3755 Done, and one marked result note.

- [ ] **Step 1: Add the shipped note**

```markdown
### Keep feedback cards and owner tasks together

- **Starting feedback work now moves its owner task to In Progress too.** Finishing or declining feedback closes the same task with a clear note, so both lists stay in step.
```

- [ ] **Step 2: Run full focused verification**

Run:

```bash
.venv/bin/pytest -q tests/test_feedback_schema.py tests/test_feedback_store.py tests/test_feedback_admin_routes.py tests/test_feedback_lifecycle_scripts.py tests/test_feedback_task_delivery.py tests/test_feedback_task_worker.py tests/test_feedback_task_lifecycle_reconcile.py tests/test_feedback_odoo.py tests/test_feedback_sync.py tests/test_feedback_projection.py
git diff --check
```

Expected: zero failures and no whitespace errors.

- [ ] **Step 3: Commit and push**

```bash
git add CHANGELOG.md
git commit -m "docs: explain synchronized feedback tasks"
git push origin main
```

- [ ] **Step 4: Preview and apply existing reconciliation after deployment**

```bash
python -m scripts.reconcile_feedback_task_lifecycle
python -m scripts.reconcile_feedback_task_lifecycle --yes
```

- [ ] **Step 5: Verify production readback**

Read the exact `GPI-PM-FB-44` Improvement and confirm Completed with matching
sync versions. Resolve its stored task relationship, then read task 3755 and
confirm Plant Manager project, exact identifier-bearing name, active state,
Done stage, and exactly one marked result note. A failed readback leaves the
task active and is reported as a blocker.
