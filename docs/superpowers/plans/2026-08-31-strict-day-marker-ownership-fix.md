# Strict-Day Marker Ownership Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent attendance synchronization from activating strict production, preserve successful strict snapshots, and safely remove the false production markers that filled the live Exception Inbox.

**Architecture:** Make the attendance recalculation queue queue-only by removing its ability to write `attendance_strict_days`. Keep `precompute._upsert_production_daily_cur` as the sole strict-marker writer, atomically coupled to a successful strict snapshot. Deploy the code before deleting the verified false markers and requeuing their exact dates through the legacy matcher.

**Tech Stack:** Python 3.11, FastAPI application modules, psycopg2/PostgreSQL, pytest, Ruff, Railway.

## Global Constraints

- The Odoo attendance-location rollout remains `off` throughout this work.
- Do not alter or dismiss employee Odoo attendance records.
- Do not weaken strict production consistency checks.
- Preserve unrelated dirty-worktree files and changes.
- New `CHANGELOG.md` text must use short, common words and explain the user benefit.
- Push the finalized plan and completed implementation to `origin/main`.
- Do not modify production data until the corrected deployment is healthy and the candidate rows are read back under a transaction lock.

---

### Task 1: Make attendance recalculation queue-only

**Files:**
- Modify: `tests/test_attendance_mirror.py`
- Modify: `src/zira_dashboard/attendance_mirror.py`
- Modify: `src/zira_dashboard/precompute.py`

**Interfaces:**
- Consumes: `attendance_mirror._enqueue_recalc_cur(cur, days, reason, *, requested_at)` and `attendance_mirror.enqueue_recalc(days, reason)`.
- Produces: recalculation queue writes that cannot create `attendance_strict_days`; successful strict snapshot storage remains unchanged in `precompute._upsert_production_daily_cur`.

- [ ] **Step 1: Write failing unit tests for incremental and sweep queue writes**

Add a small cursor double to `tests/test_attendance_mirror.py` that can exercise the new-row path of `_upsert_rows_cur`:

```python
class _InsertCursor:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def fetchone(self):
        return None
```

Add an incremental regression test that records keyword arguments passed to the queue writer:

```python
def test_incremental_attendance_change_queues_without_marking_strict(monkeypatch):
    calls = []
    monkeypatch.setattr(
        attendance_mirror,
        "_enqueue_recalc_cur",
        lambda _cur, days, reason, **kwargs: calls.append(
            (frozenset(days), reason, kwargs)
        ),
    )

    result = attendance_mirror._upsert_rows_cur(
        _InsertCursor(),
        (attendance_mirror._normalize_row(_row()),),
        sync_completed_at=SYNCED_AT,
        observed_at=SYNCED_AT,
        baseline_completed=True,
    )

    assert result == {date(2026, 8, 28)}
    assert calls == [
        (
            frozenset({date(2026, 8, 28)}),
            "odoo_attendance_changed",
            {"requested_at": SYNCED_AT},
        )
    ]
```

Extend the existing full-sweep recovery/deletion unit test so its captured calls include keyword arguments and assert the deletion call contains `requested_at` but no `mark_strict` key.

- [ ] **Step 2: Run the two unit tests and verify the regression fails**

Run:

```bash
.venv/bin/pytest tests/test_attendance_mirror.py::test_incremental_attendance_change_queues_without_marking_strict tests/test_attendance_mirror.py::test_sweep_keeps_recovery_and_deletion_recalc_reasons_separate -v
```

Expected: both tests fail because the current call sites pass `mark_strict=True`.

- [ ] **Step 3: Remove strict-marker authority from the attendance queue**

Change `_enqueue_recalc_cur` to accept only the queue inputs and delete its `attendance_strict_days` insert:

```python
def _enqueue_recalc_cur(
    cur,
    days: Iterable[date],
    reason: str,
    *,
    requested_at: datetime,
) -> None:
    unique_days = sorted(set(days))
    for day in unique_days:
        if not isinstance(day, date) or isinstance(day, datetime):
            raise TypeError("recalculation days must be date values")
        cur.execute(
            "INSERT INTO attendance_recalc_queue "
            "(day, reason, requested_at, started_at, completed_at, "
            "attempt_count, last_error) VALUES (%s, %s, %s, NULL, NULL, 0, NULL) "
            "ON CONFLICT (day) DO UPDATE SET "
            "reason = EXCLUDED.reason, "
            "requested_at = CASE "
            "WHEN attendance_recalc_queue.completed_at IS NULL "
            "THEN LEAST(attendance_recalc_queue.requested_at, EXCLUDED.requested_at) "
            "ELSE EXCLUDED.requested_at END, "
            "started_at = NULL, completed_at = NULL, "
            "attempt_count = CASE "
            "WHEN attendance_recalc_queue.completed_at IS NULL "
            "THEN attendance_recalc_queue.attempt_count ELSE 0 END, "
            "last_error = NULL",
            (day, reason, requested_at),
        )
```

Change the public wrapper to:

```python
def enqueue_recalc(days: Iterable[date], reason: str) -> None:
    requested_at = datetime.now(UTC)
    with db.cursor() as cur:
        _enqueue_recalc_cur(
            cur,
            days,
            reason,
            requested_at=requested_at,
        )
```

Remove `mark_strict=True` from the incremental and full-sweep call sites. Remove `mark_strict=False` from the failed-precompute call in `src/zira_dashboard/precompute.py`.

- [ ] **Step 4: Update PostgreSQL-backed expectations**

In `tests/test_attendance_mirror.py`, keep every recalculation queue assertion but change the affected strict-marker assertions to an empty result:

```python
assert db.query("SELECT * FROM attendance_strict_days") == []
```

Update these cases:

- close/reopen after baseline;
- sweep tombstone deletion;
- sweep tombstone recovery;
- recovery plus unrelated deletion.

Remove the no-longer-valid `mark_strict=True` argument from the health test's direct `enqueue_recalc` call. Do not change the strict snapshot tests in `tests/test_production_history_odoo_strict.py`; they prove the canonical writer remains intact.

- [ ] **Step 5: Run focused tests and verify green**

Run:

```bash
.venv/bin/pytest tests/test_attendance_mirror.py tests/test_attendance_recalc.py tests/test_production_history_odoo_strict.py tests/test_attendance_exceptions.py tests/test_exception_inbox_attendance.py -v
```

Expected: all runnable tests pass; tests requiring an unset local PostgreSQL service may be skipped by their existing marker.

---

### Task 2: Explain the fix and verify the release

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: the completed marker-ownership behavior from Task 1.
- Produces: a plain-language What's New entry and a verified implementation ready for `origin/main`.

- [ ] **Step 1: Add the release note**

Insert this entry at the top of the 2026-08-31 section:

```markdown
### Keep new Odoo work-area checks off until they are ready

- **Plant Manager now waits for a full, safe production match before it turns on the new Odoo work-area checks.** Normal clock-ins will no longer fill the Exception Inbox with early warnings.
```

- [ ] **Step 2: Run formatting, focused verification, and the complete suite**

Run:

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest -q
git diff --check
```

Expected: Ruff exits 0, pytest reports zero failures, and `git diff --check` exits 0.

- [ ] **Step 3: Review the exact diff and requirement coverage**

Run:

```bash
git diff -- src/zira_dashboard/attendance_mirror.py src/zira_dashboard/precompute.py tests/test_attendance_mirror.py CHANGELOG.md
git status --short
```

Confirm:

- `attendance_mirror.py` no longer writes `attendance_strict_days`;
- `precompute.py` still writes it only with a successfully prepared strict day;
- recalculation queue behavior and strict snapshot behavior both have tests;
- unrelated dirty files remain unstaged and unchanged.

- [ ] **Step 4: Commit and push the implementation**

Run:

```bash
git add src/zira_dashboard/attendance_mirror.py src/zira_dashboard/precompute.py tests/test_attendance_mirror.py CHANGELOG.md
git commit -m "fix: keep attendance sync from enabling strict production"
git push origin main
```

Expected: the implementation commit reaches `origin/main` and Railway begins the production deployment.

---

### Task 3: Clean false production state and verify the live Inbox

**Files:**
- Modify: no repository files.

**Interfaces:**
- Consumes: deployed queue-only behavior, production PostgreSQL, Railway logs.
- Produces: removal of only the verified false markers, queued legacy recalculation, and live readback evidence.

- [ ] **Step 1: Wait for and verify the corrected deployment**

Run:

```bash
railway status
railway logs --service web --environment production --since 10m --lines 200
```

Expected: the web service is online on the new deployment and startup completes without a traceback.

- [ ] **Step 2: Lock and verify the false marker set before deletion**

Load `.env` without printing its values, map `DATABASE_PUBLIC_URL` to `DATABASE_URL`, and run a one-transaction Python command that:

```python
candidate_days = (date(2026, 8, 28), date(2026, 8, 29), date(2026, 8, 31))
config = attendance_location_policy.get_rollout_config()
assert config.mode == "off"
cur.execute(
    "SELECT day, reason, source_changed_at FROM attendance_strict_days "
    "WHERE day = ANY(%s) ORDER BY day FOR UPDATE",
    (list(candidate_days),),
)
rows = list(cur.fetchall())
assert [row["day"] for row in rows] == list(candidate_days)
assert all(row["reason"] == "odoo_attendance_changed" for row in rows)
```

If any assertion fails, roll back automatically and stop without changing production.

- [ ] **Step 3: Delete the exact false markers and queue legacy recalculation**

In the same guarded transaction, execute:

```python
cur.execute(
    "DELETE FROM attendance_strict_days WHERE day = ANY(%s)",
    (list(candidate_days),),
)
assert cur.rowcount == len(candidate_days)
```

After commit, call:

```python
attendance_mirror.enqueue_recalc(
    candidate_days,
    "false_strict_marker_cleanup",
)
```

- [ ] **Step 4: Verify recalculation and marker readback**

Poll the exact candidate rows at intervals shorter than 60 seconds until all are complete or a real error appears. Verify:

```sql
SELECT day, reason, completed_at, attempt_count, last_error
FROM attendance_recalc_queue
WHERE day = ANY(%s)
ORDER BY day;

SELECT day, reason
FROM attendance_strict_days
WHERE day = ANY(%s)
ORDER BY day;
```

Expected: all three queue rows complete successfully and the strict-marker readback is empty.

- [ ] **Step 5: Verify the live attendance snapshot and Inbox**

Build the production attendance exception snapshot for August 31 with fresh UTC time and verify:

```python
assert snapshot.mode == "off"
assert snapshot.production_mode == "legacy"
assert snapshot.issues == ()
assert snapshot.source_errors == ()
```

Read the live `/api/exceptions` response through the authenticated application path and confirm it contains neither Odoo Location Missing rows nor a Strict Production source error. Unrelated Inbox items may remain.

If strict markers recur, stop and report that the deployment does not contain the fix. If legacy recalculation fails, leave the rollout off and preserve the queue error for diagnosis.
