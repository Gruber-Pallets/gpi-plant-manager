# Restore Shadow Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the coherent attendance Shadow/Live rollout so its background warmer records a verified complete Shadow day and the existing Live gate works without bypasses.

**Architecture:** Reapply the Task 13 attendance-rollout implementation from `15d86881` only to files that a later merge regressed. Preserve later feedback work by three-way inspecting every overlapping file. The app warmer and readiness module must share the no-argument `tick()` boundary; aggregate-only Shadow state remains local and Live stays gated.

**Tech Stack:** Python 3.13, FastAPI/asyncio warmers, PostgreSQL, pytest, Railway.

## Global Constraints

- Never enable or schedule Live in production as part of this repair.
- Do not synthesize a Shadow observation for a past day.
- Preserve the existing salaried no-work-center exemption.
- Use `apply_patch` for source changes and keep all unrelated feedback changes.
- Add a short child-readable `CHANGELOG.md` note before pushing code to main.

---

### Task 1: Test-first restoration of the coherent rollout

**Files:**

- Modify: `tests/test_attendance_readiness.py`
- Read: `src/zira_dashboard/app.py`, `src/zira_dashboard/attendance_readiness.py`

**Interfaces:**

- Consumes: `app._tick_attendance_readiness()`
- Produces: a regression assertion that the app invokes `attendance_readiness.tick()` without positional arguments.

- [ ] **Step 1: Write the failing test**

```python
def test_exactly_one_nonblocking_readiness_warmer_runs_every_30_seconds(monkeypatch):
    from zira_dashboard import app as app_module

    matches = [
        item
        for item in app_module._WARMERS
        if item[1] is app_module._tick_attendance_readiness
    ]
    calls = []
    monkeypatch.setattr(attendance_readiness, "tick", lambda: calls.append("tick"))

    asyncio.run(app_module._tick_attendance_readiness())

    assert matches == [("attendance readiness", app_module._tick_attendance_readiness, 30)]
    assert calls == ["tick"]
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
/Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest \
  tests/test_attendance_readiness.py::test_exactly_one_nonblocking_readiness_warmer_runs_every_30_seconds -q
```

Expected: failure because the regressed module has no `tick` API and the app calls the incompatible older warmer method.

**Continue Task 1: Restore the coherent rollout implementation**

**Files:**

- Modify: `src/zira_dashboard/attendance_readiness.py`
- Modify: `src/zira_dashboard/attendance_location_policy.py`
- Modify: `src/zira_dashboard/attendance_exceptions.py`
- Modify: `src/zira_dashboard/exception_inbox.py`
- Modify: `src/zira_dashboard/inbox_reconcile.py`
- Modify: `src/zira_dashboard/precompute.py`
- Modify: `src/zira_dashboard/attendance_mirror.py`
- Modify: `src/zira_dashboard/attendance_corrections.py`
- Modify: `src/zira_dashboard/production_history.py`
- Modify: `src/zira_dashboard/_schema.py`
- Modify: `src/zira_dashboard/routes/settings.py`
- Modify: `src/zira_dashboard/app.py`
- Modify: `src/zira_dashboard/templates/settings.html`
- Modify: `scripts/check_attendance_location_readiness.py`
- Modify: `tests/test_attendance_readiness.py`
- Modify: `tests/test_attendance_location_end_to_end.py`
- Modify: `tests/test_attendance_location_failure_modes.py`
- Modify: `tests/test_attendance_location_policy.py`
- Modify: `tests/test_settings_timeclock_layout.py`
- Modify: matching Task 13 mirror, correction, precompute, and production-history test files

**Interfaces:**

- Consumes: the known-good Task 13 file versions at `15d86881` and the current `origin/main` feedback implementation.
- Produces: `attendance_readiness.tick(now_utc: datetime | None = None)`, a compatible `app._tick_attendance_readiness`, and current setting-key readers for `odoo_attendance_location_shadow_epoch` and `odoo_attendance_location_shadow_health`.

- [ ] **Step 1: Inspect each Task 13 file against the known-good baseline**

Run:

```bash
git diff -- 15d86881 origin/main -- \
  src/zira_dashboard/attendance_readiness.py \
  src/zira_dashboard/attendance_location_policy.py \
  src/zira_dashboard/attendance_exceptions.py \
  src/zira_dashboard/exception_inbox.py \
  src/zira_dashboard/inbox_reconcile.py \
  src/zira_dashboard/precompute.py \
  src/zira_dashboard/routes/settings.py \
  src/zira_dashboard/app.py \
  scripts/check_attendance_location_readiness.py
```

Expected: verify each difference is the older rollout regression or an unrelated feedback change that must survive.

- [ ] **Step 2: Apply the minimal three-way restoration**

Restore the Task 13 implementations from `15d86881` for the owned rollout logic while retaining later non-rollout additions. The app adapter must be:

```python
async def _tick_attendance_readiness():
    """Refresh shadow health and decide one due attendance cutover."""
    from . import attendance_readiness

    await asyncio.to_thread(attendance_readiness.tick)
```

The restored readiness public boundary must be:

```python
def tick(now_utc: datetime | None = None) -> CutoverActivationResult:
    now = _aware_utc(now_utc or _utc_now(), "now_utc")
    refresh_shadow_comparison(now)
    return activate_due_cutover(now)
```

Use the matching Task 13 tests from `15d86881`, then retain any later tests that cover unrelated feedback behavior. Restore the required mirror generation field and lock API, correction/cache-readiness hooks, strict-production Shadow snapshot inputs, additive schema state, and Settings explanation markup as one compatible Task 13 boundary.

- [ ] **Step 3: Run focused GREEN tests**

Run:

```bash
/Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest \
  tests/test_attendance_readiness.py \
  tests/test_attendance_location_end_to_end.py \
  tests/test_attendance_location_failure_modes.py \
  tests/test_attendance_location_policy.py \
  tests/test_settings_timeclock_layout.py -q
```

Expected: all focused tests pass; PostgreSQL-specific tests may skip only when the local database is unavailable.

### Task 2: Verify, document, deploy, and observe Shadow safely

**Files:**

- Modify: `CHANGELOG.md`
- Test: all Task 13 test files and the repository suite

**Interfaces:**

- Consumes: the restored rollout and warmer contract.
- Produces: a deployed repair whose aggregate Shadow record refreshes while Live remains unscheduled.

- [ ] **Step 1: Add the child-readable release note**

Under the current date in `CHANGELOG.md`, add:

```markdown
### Shadow safety checks keep running

- **The app now keeps its practice-day checks up to date.** This helps us make sure attendance is right before turning on the new live system.
```

- [ ] **Step 2: Run static and broad verification**

Run:

```bash
/Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m ruff check src tests scripts
/Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest -q
git diff --check
```

Expected: tests pass except the documented macOS Chromium sandbox case, which must be rerun outside that sandbox before reporting success.

- [ ] **Step 3: Commit and push the repair**

```bash
git add CHANGELOG.md src/zira_dashboard scripts tests
git commit -m "fix: restore attendance shadow refresh"
git push origin HEAD:main
```

- [ ] **Step 4: Verify the deployed repair without enabling Live**

Run read-only production checks:

```bash
railway deployment list --limit 1 --json
railway ssh "python scripts/check_attendance_location_readiness.py"
```

Expected: the deployed Shadow aggregate becomes current, the mirror remains healthy, and the rollout remains `shadow` until a valid completed day makes the normal readiness report ready.
