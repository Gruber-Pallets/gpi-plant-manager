# Feedback Task Contract Versioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Requeue and readback-verify owner tasks whenever the task synchronization contract changes, then close the exact task associated with feedback 43.

**Architecture:** Keep feedback lifecycle versions and task-contract versions as independent monotonic pairs on `feedback_task_delivery`. The existing bounded reconciler raises old task rows to the current contract, and the existing worker settles both pairs only after its safeguarded Odoo readback. Completion reporting and repository instructions require both the Improvement and its durably associated task to verify terminal.

**Tech Stack:** Python 3.12, FastAPI service modules, PostgreSQL idempotent bootstrap DDL, psycopg2, pytest, Ruff, Railway, Odoo XML-RPC.

## Global Constraints

- The authoritative feedback identity is `GPI-PM-FB-<positive id>`; a pasted Task ID is never authoritative.
- The current owner-task synchronization contract version is exactly `2`.
- Completed feedback maps to task stage `Done` and task Status `1_done`; declined feedback maps to task stage `Done` and task Status `1_canceled`.
- Never create, guess, replace, archive, merge, or delete an Odoo Improvement or owner task during lifecycle repair.
- Blocked task relationships remain blocked for human review.
- A worker claim settles only after fresh readback verifies task identity, stage, Status, and any required terminal-note marker.
- Existing unrelated worktree changes must remain untouched.
- Every new `CHANGELOG.md` entry uses short, common words and explains the user benefit.

---

## File map

- `src/zira_dashboard/_schema.py`: persist desired and verified task-contract versions with safe fresh/existing-database defaults.
- `src/zira_dashboard/feedback_task_delivery.py`: own the current contract constant, claim data, reconciliation, settlement, and admin status rules.
- `src/zira_dashboard/feedback_store.py`: expose contract versions to lifecycle commands and admin presentation.
- `scripts/feedback_lifecycle.py`: report task synchronization only when lifecycle and contract versions are current.
- `scripts/reconcile_feedback_task_lifecycle.py`: preview and queue contract-stale rows through the bounded local reconciler.
- `tests/test_feedback_schema.py`: lock the idempotent schema and migration contract.
- `tests/test_feedback_task_delivery.py`: cover enqueue, claim propagation, reconciliation, settlement races, and admin status.
- `tests/test_feedback_lifecycle_scripts.py`: prevent false `synced` command output.
- `tests/test_feedback_task_lifecycle_reconcile.py`: cover dry-run/apply contract-version eligibility.
- `tests/test_feedback_task_worker.py`: retain the observed task-3656 regression shape through the generalized worker.
- `AGENTS.md`: require exact owner-task readback before reporting Plant Manager feedback complete.
- `CHANGELOG.md`: explain the repaired completion process in plain language.

---

### Task 1: Persist task-contract versions

**Files:**
- Modify: `src/zira_dashboard/_schema.py`
- Modify: `tests/test_feedback_schema.py`

**Interfaces:**
- Produces database columns `desired_contract_version BIGINT` and `last_synced_contract_version BIGINT` on `feedback_task_delivery`.
- Fresh rows target contract 2; existing rows migrate below contract 2 so reconciliation can verify them.

- [ ] **Step 1: Write the failing schema tests**

Add assertions to `test_feedback_task_delivery_schema_is_durable_and_safe` (or the current equivalent):

```python
assert "desired_contract_version BIGINT NOT NULL DEFAULT 2" in ddl
assert "last_synced_contract_version BIGINT NOT NULL DEFAULT 0" in ddl
assert (
    "ADD COLUMN IF NOT EXISTS desired_contract_version BIGINT NOT NULL DEFAULT 1"
    in ddl
)
assert "ALTER COLUMN desired_contract_version SET DEFAULT 2" in ddl
assert "last_synced_contract_version <= desired_contract_version" in ddl
```

- [ ] **Step 2: Run the schema test and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_feedback_schema.py -q
```

Expected: FAIL because the two task-contract columns and constraints are absent.

- [ ] **Step 3: Add the idempotent schema migration**

Extend the fresh table definition with:

```sql
desired_contract_version BIGINT NOT NULL DEFAULT 2
  CHECK (desired_contract_version > 0),
last_synced_contract_version BIGINT NOT NULL DEFAULT 0
  CHECK (last_synced_contract_version >= 0),
CHECK (last_synced_contract_version <= desired_contract_version),
```

After the existing lifecycle-version `ALTER TABLE` statements, add:

```sql
ALTER TABLE feedback_task_delivery
  ADD COLUMN IF NOT EXISTS desired_contract_version BIGINT NOT NULL DEFAULT 1
  CHECK (desired_contract_version > 0);
ALTER TABLE feedback_task_delivery
  ADD COLUMN IF NOT EXISTS last_synced_contract_version BIGINT NOT NULL DEFAULT 0
  CHECK (last_synced_contract_version >= 0);
ALTER TABLE feedback_task_delivery
  ALTER COLUMN desired_contract_version SET DEFAULT 2;
ALTER TABLE feedback_task_delivery
  DROP CONSTRAINT IF EXISTS feedback_task_delivery_contract_version_order;
ALTER TABLE feedback_task_delivery
  ADD CONSTRAINT feedback_task_delivery_contract_version_order
  CHECK (last_synced_contract_version <= desired_contract_version);
```

The `ADD COLUMN` default of 1 deliberately marks existing rows as needing the
new contract. The later default of 2 affects only future inserts. Fresh tables
already contain the column with default 2, so the guarded add is a no-op.

- [ ] **Step 4: Run schema tests GREEN**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_feedback_schema.py -q
.venv/bin/ruff check src/zira_dashboard/_schema.py tests/test_feedback_schema.py
```

Expected: all tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 5: Commit the schema slice**

```bash
git add src/zira_dashboard/_schema.py tests/test_feedback_schema.py
git commit -m "feat: version owner task sync contracts"
git push origin main
```

---

### Task 2: Requeue and settle contract-stale owner tasks

**Files:**
- Modify: `src/zira_dashboard/feedback_task_delivery.py`
- Modify: `tests/test_feedback_task_delivery.py`

**Interfaces:**
- Produces: `TASK_SYNC_CONTRACT_VERSION: int = 2`.
- Extends `TaskDeliveryClaim` with `desired_contract_version: int` and `last_synced_contract_version: int`.
- `queue_existing_lifecycle_mismatches(limit: int = 100) -> int` queues lifecycle or contract mismatches.
- `mark_delivered(claim, now=None) -> None` settles both verified version pairs atomically.

- [ ] **Step 1: Write failing model and enqueue tests**

Add tests that construct a claim with contract versions and reject inverted or
nonpositive values:

```python
claim = delivery.TaskDeliveryClaim(
    feedback_id=42,
    claim_token=UUID("11111111-1111-1111-1111-111111111111"),
    task_id=3656,
    before_attachment_id=None,
    expires_at=NOW,
    desired_version=3,
    last_synced_version=3,
    desired_status="completed",
    desired_contract_version=2,
    last_synced_contract_version=1,
)
assert claim.desired_contract_version == delivery.TASK_SYNC_CONTRACT_VERSION
```

Update the enqueue SQL test to require both contract columns and the current
constant as inserted parameters.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest \
  tests/test_feedback_task_delivery.py -k "contract or enqueue" -q
```

Expected: FAIL because the constant and claim fields do not exist.

- [ ] **Step 3: Add the current contract and claim fields**

Add near the existing limits:

```python
TASK_SYNC_CONTRACT_VERSION = 2
```

Extend the dataclass:

```python
desired_contract_version: int = TASK_SYNC_CONTRACT_VERSION
last_synced_contract_version: int = 0
```

Validate desired as a positive signed 64-bit integer, verified as nonnegative,
and require verified not to exceed desired. Extend `_claim_from_row`, every
claim `SELECT`/`RETURNING`, and every reconstructed `TaskDeliveryClaim` so both
values survive claim, renewal, task-ID persistence, and attachment persistence.

Extend `enqueue_submission` so its explicit column list ends with
`desired_contract_version, last_synced_contract_version`, its value list ends
with `%s, 0`, and `TASK_SYNC_CONTRACT_VERSION` is the final parameter.

- [ ] **Step 4: Run the model and enqueue tests GREEN**

Run the same focused command. Expected: PASS.

- [ ] **Step 5: Write failing reconciliation tests**

Cover these SQL behaviors with the existing recording cursor:

```python
assert delivery.queue_existing_lifecycle_mismatches() == 1
sql, params = cursor.executions[0]
assert "td.desired_contract_version <> %s" in sql
assert "td.last_synced_contract_version < %s" in sql
assert "desired_contract_version = %s" in sql
assert params.count(delivery.TASK_SYNC_CONTRACT_VERSION) >= 3
assert "td.state <> 'blocked'" in sql
```

Also lock `enqueue_lifecycle` so a normal lifecycle transition raises
`desired_contract_version` with `GREATEST(desired_contract_version, %s)` and can
queue a row whose lifecycle version already matches but contract is stale.

- [ ] **Step 6: Run reconciliation tests and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest \
  tests/test_feedback_task_delivery.py -k "lifecycle or mismatch or contract" -q
```

Expected: FAIL because reconciliation ignores the task contract.

- [ ] **Step 7: Implement bounded contract reconciliation**

Extend candidate eligibility with:

```sql
OR td.desired_contract_version <> %s
OR td.last_synced_contract_version < %s
```

and the update with:

```sql
desired_contract_version = %s,
```

Pass `TASK_SYNC_CONTRACT_VERSION` for all three placeholders. Keep
`td.state <> 'blocked'`, exact local ownership, the stored `odoo_task_id`
requirement, `FOR UPDATE OF td SKIP LOCKED`, and the 100-row bound unchanged.

In `enqueue_lifecycle`, set:

```sql
desired_contract_version = GREATEST(desired_contract_version, %s)
```

and allow the guarded update when either the lifecycle version advances or the
desired contract is older than the current contract.

- [ ] **Step 8: Run reconciliation tests GREEN**

Run the Step 6 command. Expected: PASS.

- [ ] **Step 9: Write failing settlement-race tests**

Extend `mark_delivered` SQL assertions to require:

```python
assert "last_synced_contract_version = %s" in sql
assert "desired_contract_version = %s" in sql
assert "last_synced_contract_version = %s" in sql
```

Cover both outcomes: matching desired contract becomes `delivered`; a newer
desired contract leaves the row `pending` and due immediately.

- [ ] **Step 10: Run settlement tests and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest \
  tests/test_feedback_task_delivery.py -k "mark_delivered or completion" -q
```

Expected: FAIL because settlement only fences the feedback lifecycle version.

- [ ] **Step 11: Settle both version pairs atomically**

Update `mark_delivered` to set:

```sql
last_synced_version = %s,
last_synced_contract_version = %s,
state = CASE
  WHEN desired_version = %s AND desired_contract_version = %s
  THEN 'delivered' ELSE 'pending' END,
due_at = CASE
  WHEN desired_version > %s OR desired_contract_version > %s
  THEN %s ELSE due_at END
```

The `WHERE` fence must require desired versions not older than the claim and
verified versions exactly equal to the claim's starting verified versions.
This prevents an old lease from settling new lifecycle or contract intent.

- [ ] **Step 12: Run the task-delivery module GREEN**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_feedback_task_delivery.py -q
.venv/bin/ruff check src/zira_dashboard/feedback_task_delivery.py \
  tests/test_feedback_task_delivery.py
```

Expected: all tests pass and Ruff is clean.

- [ ] **Step 13: Commit the durable queue behavior**

```bash
git add src/zira_dashboard/feedback_task_delivery.py tests/test_feedback_task_delivery.py
git commit -m "fix: recheck tasks after sync contract changes"
git push origin main
```

---

### Task 3: Stop lifecycle commands and the admin page from claiming false synchronization

**Files:**
- Modify: `src/zira_dashboard/feedback_store.py`
- Modify: `src/zira_dashboard/feedback_task_delivery.py`
- Modify: `scripts/feedback_lifecycle.py`
- Modify: `scripts/reconcile_feedback_task_lifecycle.py`
- Modify: `tests/test_feedback_lifecycle_scripts.py`
- Modify: `tests/test_feedback_task_delivery.py`
- Modify: `tests/test_feedback_task_lifecycle_reconcile.py`

**Interfaces:**
- `feedback_store.lifecycle_state()` returns desired and verified task-contract versions.
- Command JSON keeps its existing safe shape; `task_sync_state` becomes `pending` until both version pairs are current.
- Admin status says `Owner task synced` only when both pairs match the current contract.

- [ ] **Step 1: Write failing lifecycle-command tests**

Extend the lifecycle state fixture with:

```python
"task_desired_contract_version": 2,
"task_last_synced_contract_version": 1,
```

Assert a delivered row with equal lifecycle versions but verified contract 1
reports `"task_sync_state":"pending"`. Add the matching contract-2 case and
assert it reports `synced`.

- [ ] **Step 2: Run command tests and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_feedback_lifecycle_scripts.py -q
```

Expected: FAIL because lifecycle state does not expose or check contract versions.

- [ ] **Step 3: Expose and validate contract state**

In `feedback_store.lifecycle_state`, select aliases:

```sql
td.desired_contract_version AS task_desired_contract_version,
td.last_synced_contract_version AS task_last_synced_contract_version
```

Require the two keys, positive desired value, nonnegative verified value, and
verified `<=` desired.

In `scripts/feedback_lifecycle.py`, classify a task as synced only when:

```python
raw_task_state == "delivered"
and state["task_last_synced_version"] == state["task_desired_version"]
and state["task_desired_contract_version"]
    == feedback_task_delivery.TASK_SYNC_CONTRACT_VERSION
and state["task_last_synced_contract_version"]
    == state["task_desired_contract_version"]
```

Do not add raw IDs or Odoo content to command JSON.

- [ ] **Step 4: Run command tests GREEN**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 5: Write failing admin-status tests**

Add one row with equal lifecycle versions, delivered state, desired contract 2,
and verified contract 1. Assert `admin_status_for` returns `("Task update pending", None)`.
Add the fully matched contract-2 row and assert `("Owner task synced", None)`.

- [ ] **Step 6: Run admin tests and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest \
  tests/test_feedback_task_delivery.py -k "admin_status" -q
```

Expected: FAIL because admin status checks only lifecycle versions.

- [ ] **Step 7: Include contract state in admin shaping**

Add contract aliases to the feedback admin query in `feedback_store.py`, pass
them through to `admin_status_for`, and remove the raw aliases before returning
the owner-facing row. Treat any older desired or verified contract as pending.
Return `Owner task synced` only for delivered rows whose lifecycle versions
match and whose desired and verified contract both equal
`TASK_SYNC_CONTRACT_VERSION`.

- [ ] **Step 8: Run admin tests GREEN**

Run the Step 6 command. Expected: PASS.

- [ ] **Step 9: Write failing standalone-reconciler tests**

Update the recording-cursor tests to assert both `_ELIGIBLE` and `_APPLY`
contain contract-version predicates and `_APPLY` raises
`desired_contract_version` to contract 2 without touching blocked rows.

- [ ] **Step 10: Run reconciler tests and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest \
  tests/test_feedback_task_lifecycle_reconcile.py -q
```

Expected: FAIL because the standalone script checks only lifecycle versions.

- [ ] **Step 11: Version the standalone reconciler**

Import `feedback_task_delivery` and add `%s` contract placeholders to
`_ELIGIBLE` and `_APPLY` matching the service reconciler. Execute each query
with `TASK_SYNC_CONTRACT_VERSION` parameters. Keep preview read-only and apply
local-only.

- [ ] **Step 12: Run all reporting and reconciliation tests GREEN**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest \
  tests/test_feedback_lifecycle_scripts.py \
  tests/test_feedback_task_delivery.py \
  tests/test_feedback_task_lifecycle_reconcile.py -q
.venv/bin/ruff check src/zira_dashboard/feedback_store.py \
  src/zira_dashboard/feedback_task_delivery.py scripts/feedback_lifecycle.py \
  scripts/reconcile_feedback_task_lifecycle.py \
  tests/test_feedback_lifecycle_scripts.py tests/test_feedback_task_delivery.py \
  tests/test_feedback_task_lifecycle_reconcile.py
```

Expected: all tests pass and Ruff is clean.

- [ ] **Step 13: Commit synchronization reporting**

```bash
git add src/zira_dashboard/feedback_store.py \
  src/zira_dashboard/feedback_task_delivery.py scripts/feedback_lifecycle.py \
  scripts/reconcile_feedback_task_lifecycle.py \
  tests/test_feedback_lifecycle_scripts.py tests/test_feedback_task_delivery.py \
  tests/test_feedback_task_lifecycle_reconcile.py
git commit -m "fix: report owner tasks synced only after readback"
git push origin main
```

---

### Task 4: Lock the completion process and observed regression

**Files:**
- Modify: `tests/test_feedback_task_worker.py`
- Modify: `AGENTS.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Repository agents must verify both the exact Improvement and the durably associated task before completion.
- Worker regression coverage models task 3656's observed Done-stage/In-Progress-Status mismatch.

- [ ] **Step 1: Strengthen the worker regression name and assertions**

Rename or extend the existing terminal repair test so its fixture explicitly
documents the production shape without hard-coding task identity into product
logic:

```python
def test_contract_resync_repairs_done_stage_with_open_status(monkeypatch):
    # Production feedback 43 exposed this shape: lifecycle and stage were done,
    # while project.task.state still said In Progress.
    item = snapshot(status="completed", projection_version=3, resolution_note="Fixed")
    claim = _terminal_claim("completed")
    assert claim.desired_contract_version == 2
    assert claim.last_synced_contract_version == 1
    stub_delivery(monkeypatch, item)
    stub_odoo(monkeypatch)
    worker.odoo_client.find_feedback_stage_ids.return_value = [8]
    worker.odoo_client.find_task_message_ids.side_effect = [[901], [901]]
    worker.odoo_client.read_feedback_task.side_effect = [
        {
            "id": 55,
            "name": worker.task_name(item),
            "project_id": 3,
            "active": True,
            "stage_id": 8,
            "stage_name": "Done",
            "state": "01_in_progress",
        },
        {
            "id": 55,
            "name": worker.task_name(item),
            "project_id": 3,
            "active": True,
            "stage_id": 8,
            "stage_name": "Done",
            "state": "1_done",
        },
    ]

    assert worker.process_claim(claim, now=NOW) == "delivered"
    worker.odoo_client.update_task.assert_called_once_with(55, state="1_done")
    worker.task_delivery.mark_delivered.assert_called_once_with(claim, now=NOW)
```

Set the claim's desired contract to 2 and verified contract to 1. Verify
`mark_delivered` receives that exact claim only after the second task read
returns `state="1_done"`.

- [ ] **Step 2: Run the regression test GREEN**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest \
  tests/test_feedback_task_worker.py \
  -k "contract_resync_repairs_done_stage_with_open_status" -q
```

Expected: PASS using the generalized worker path.

- [ ] **Step 3: Update repository completion instructions**

Add to the Plant Manager lifecycle section in `AGENTS.md`:

```markdown
- Plant Manager feedback completion includes its durably associated Odoo owner
  task. After the normal workers run, obtain the task only from the local
  `feedback_task_delivery` relationship, read that exact task back, and verify
  both records: Completed feedback requires Improvement Status `Completed`,
  task stage `Done`, and task Status `Done`; declined feedback requires
  Improvement Status `Declined`, task stage `Done`, and task Status `Cancelled`.
  Never trust local sync-version equality without the final Odoo task readback,
  and never use a pasted Task ID as the relationship authority.
```

Extend the existing failure rule so a missing relationship, task write failure,
or task readback mismatch keeps the task incomplete.

- [ ] **Step 4: Add the plain-language patch note**

At the top of `CHANGELOG.md` under `2026-09-02`, add:

```markdown
### Finish both parts of feedback work

- **Finished feedback now checks its matching task again when the rules change.** Plant Manager does not call the work finished until both the feedback card and its task show as done.
```

- [ ] **Step 5: Run focused verification**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest \
  tests/test_feedback_schema.py \
  tests/test_feedback_task_delivery.py \
  tests/test_feedback_lifecycle_scripts.py \
  tests/test_feedback_task_lifecycle_reconcile.py \
  tests/test_feedback_task_worker.py \
  tests/test_feedback_store.py \
  tests/test_feedback_admin_routes.py -q
.venv/bin/ruff check src/zira_dashboard/_schema.py \
  src/zira_dashboard/feedback_store.py \
  src/zira_dashboard/feedback_task_delivery.py \
  src/zira_dashboard/feedback_task_worker.py \
  scripts/feedback_lifecycle.py scripts/reconcile_feedback_task_lifecycle.py \
  tests/test_feedback_schema.py tests/test_feedback_task_delivery.py \
  tests/test_feedback_lifecycle_scripts.py \
  tests/test_feedback_task_lifecycle_reconcile.py tests/test_feedback_task_worker.py
git diff --check
```

Expected: all available focused tests pass, Postgres-only tests skip when
`DATABASE_URL` is empty, Ruff is clean, and `git diff --check` is silent.

- [ ] **Step 6: Commit and push the process guard**

```bash
git add AGENTS.md CHANGELOG.md tests/test_feedback_task_worker.py
git commit -m "docs: require owner task completion readback"
git push origin main
```

---

### Task 5: Full validation, deployment, and feedback 43 repair

**Files:**
- Modify after successful verification: `docs/superpowers/specs/2026-09-02-feedback-task-contract-version-design.md` (status only)
- Modify after successful verification: `docs/superpowers/plans/2026-09-02-feedback-task-contract-versioning.md` (checkboxes only)

**Interfaces:**
- Production must migrate, queue, process, and read back the existing durable relationship for feedback 43.
- Final evidence must identify exactly one Improvement and the locally associated task, without exposing credentials or unrelated records.

- [ ] **Step 1: Run the full available suite**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest -q
.venv/bin/ruff check src scripts tests
git diff --check
```

Expected: zero failures; Postgres-gated tests may skip only because
`DATABASE_URL` is unset; Ruff and whitespace checks pass.

- [ ] **Step 2: Confirm only intended files changed**

Run:

```bash
git status --short
git diff --stat origin/main...HEAD
```

Expected: unrelated pre-existing files remain unstaged and unchanged by this
work. All intended implementation commits are on `main` and pushed.

- [ ] **Step 3: Wait for the production deployment**

Run:

```bash
railway status
```

Expected: project `GPI-Plant-Manager`, service `web`, repository
`Gruber-Pallets/gpi-plant-manager`, and status `Online` after the newest main
deployment.

- [ ] **Step 4: Preview and apply bounded local reconciliation**

Run inside the Railway web service:

```bash
python -m scripts.reconcile_feedback_task_lifecycle
python -m scripts.reconcile_feedback_task_lifecycle --yes
```

Expected: preview reports at least one eligible contract-stale relationship;
apply queues bounded rows and performs no direct Odoo write.

- [ ] **Step 5: Let the normal task worker settle feedback 43**

Wait for the scheduled worker. Poll the sanitized local lifecycle state for
feedback 43 until all are true:

```text
status = completed
odoo_task_id = 3656
state = delivered
desired_version = last_synced_version = 3
desired_contract_version = last_synced_contract_version = 2
```

Do not write the Odoo task directly. If the worker enters attention or blocked
state, stop and report the sanitized reason.

- [ ] **Step 6: Read back the exact Odoo Improvement**

Using `ImprovementsClient`, find exact source ID `GPI-PM-FB-43`, require exactly
one result, and read only `x_studio_source_id` plus `x_studio_status`.

Expected: one row with source ID `GPI-PM-FB-43` and Status `Completed`.

- [ ] **Step 7: Read back the locally associated Odoo task**

Use the task ID obtained from `feedback_task_delivery`, not the pasted request.
Call `odoo_client.read_feedback_task(3656)` and verify:

```text
id = 3656
active = true
stage_name = Done
state = 1_done
name begins with [GPI-PM-FB-43]
```

Expected: the task is closed in both Odoo representations.

- [ ] **Step 8: Mark design and plan implemented**

Only after Steps 1-7 succeed, change the design status to `Implemented` and
check every completed plan step. Then run:

```bash
git diff --check
git add docs/superpowers/specs/2026-09-02-feedback-task-contract-version-design.md \
  docs/superpowers/plans/2026-09-02-feedback-task-contract-versioning.md
git commit -m "docs: record durable feedback task repair"
git push origin main
```

- [ ] **Step 9: Final readback after the documentation deployment**

Repeat Steps 6 and 7 after the final push. Expected: Improvement remains
`Completed`; task 3656 remains in stage `Done` with Status `1_done`.
