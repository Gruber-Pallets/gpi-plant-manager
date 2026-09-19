# Quick-Punch Odoo Fixer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically merge quick-punch mistakes in Odoo `hr.attendance` itself (Off / Preview / Live, ships in Preview), reusing the app-side smoothing rules and the existing verified correction engine.

**Architecture:**
- **Pure planning:** `quick_punch_fixes.find_fixes` runs `quick_punch_smoothing` over one day's Odoo location spans. It returns *settled* merges as `PlannedFix` records.
- **I/O tick:** `quick_punch_fixer.tick` runs every 60 s. It applies the payroll, active-job, and dedupe guards, then either records Preview events or creates correction jobs.
- **Correction engine:** gains an explicit, integrity-checked `merge` request mode (gap-fill; keep the open row's ID). It also gains a no-overlap-safe operation order, and an attempt cap plus an alert for fixer jobs.
- **Setting:** a new audited setting mirrors Auto-Lunch.
- **Pay-period cleanup:** a dry-run-first backfill script.

**Tech Stack:** Python 3.12, FastAPI, Postgres (psycopg2), Odoo XML-RPC via `odoo_client`, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-18-quick-punch-odoo-fixer-design.md` (authoritative). Background: `docs/superpowers/specs/2026-09-18-quick-punch-smoothing-design.md`.

**Global rules for every task:**
- Work on `main` and don't push until Task 8.
- Never set `DATABASE_URL` to production. DB-gated tests use the local embedded pgserver recipe or skip.
- Tests: `ZIRA_API_KEY=test .venv/bin/python -m pytest …`. Lint: `.venv/bin/ruff check src tests scripts`.
- Run experiments and mutation checks on scratchpad copies only.
- Don't touch the untracked `.cursorignore`, `.python-version`, `uv.lock`, or `.claude/`.
- Legacy correction behavior must be byte-for-byte unchanged when `merge` is absent. Existing correction tests must pass without edits, unless a task says otherwise.

---

## File Structure

- **Modify** `src/zira_dashboard/attendance_corrections.py`: `merge` request mode (Task 1), operation order, fixer attempt cap, completion audit, and failure hook (Task 2).
- **Create** `src/zira_dashboard/quick_punch_fix_settings.py`: the Off/Preview/Live singleton with audit (Task 3).
- **Modify** `src/zira_dashboard/_schema.py`: two setting tables (Task 3).
- **Modify** `src/zira_dashboard/routes/settings.py`, `settings_context.py`, `templates/settings.html`, `static/settings.js`: the setting UI (Task 3).
- **Create** `src/zira_dashboard/quick_punch_fixes.py`: pure detection (Task 4).
- **Modify** `src/zira_dashboard/assignment_windows.py`: expose `blocking_windows` publicly (Task 4).
- **Create** `src/zira_dashboard/quick_punch_fixer.py`: the tick (Task 5).
- **Modify** `src/zira_dashboard/app.py`: register the warmer (Task 5).
- **Modify** `src/zira_dashboard/inbox_log.py`, `routes/exceptions.py`, `static/exceptions.js`, `exception_inbox.py`: visibility and the failure alert (Task 6).
- **Create** `scripts/quick_punch_backfill.py` (Task 7).
- **Modify** `CHANGELOG.md` (Task 8).
- **Tests:**
  - `tests/test_attendance_correction_planner.py` (Task 1)
  - `tests/test_attendance_corrections_merge_execution.py` (new, Task 2)
  - `tests/test_quick_punch_fix_settings.py` (new, Task 3)
  - `tests/test_quick_punch_fixes.py` (new, Task 4)
  - `tests/test_quick_punch_fixer.py` (new, Task 5)
  - `tests/test_quick_punch_inbox.py` (new, Task 6)
  - `tests/test_quick_punch_backfill.py` (new, Task 7)

---

### Task 1: Correction engine — `merge` request mode (planning and validation)

**Files:** Modify `src/zira_dashboard/attendance_corrections.py`. Test `tests/test_attendance_correction_planner.py`.

**Behavior to add:**
- `plan_correction(..., merge: bool = False)`. When `merge` is true, the plan's `request` mapping carries the extra key `"merge": True`. When false, the mapping is exactly today's five keys: no `merge` key, not `merge: False`.
- `_REQUEST_FIELDS` stays the legacy set. Request validation (`_validate_request`, ~line 1019) accepts exactly the legacy set, or the legacy set plus `merge` with value `True`, and nothing else.
- `_pieces_for_request(..., merge=False)` threads the flag. `_validate_plan` (~line 1230) passes `plan.request.get("merge", False)` so re-derivation matches.
- **Merge planning:**
  - **No-op:** a merge is a no-op only when exactly one source row overlaps `[start, end)` and it covers the whole range with the target station and department. For open ranges it must be the single open row, starting at or before `start`.
  - **Closed merge** (`end` is not None): all rows overlapping `[start, end)` form ONE group regardless of gaps. The left remainder comes from the first overlapping row if it starts before `start`, and the right remainder from the last row if it ends after `end` (or is open). Both keep today's ID reuse rules. There is a single target piece `[start, end)`. It reuses the first row fully inside the range that isn't already used, else it is a create. Every other overlapping row is deleted.
  - **Open merge** (`end` is None): as `_open_pieces` today, except the survivor is the affected row whose `check_out` is null when one exists. Otherwise it is today's rule (earliest affected row starting at or after `start`). The left-remainder handling of the first affected row is unchanged.
- **Plumbing:** `merge` also flows through the helpers below. `correction_preview` and `_build_preview` take `merge: bool = False` and forward it to `plan_correction`.
  - `plan_to_json` and `plan_from_json`, plus the integrity hash, cover it automatically if they serialize `request`. Verify with a round-trip test.
  - `_validated_request` and `correction_preview`.
  - `CorrectionPreview`. Add `merge: bool = False`, or derive it from the plans. Keep a single source of truth.
  - `preview_job_binding` and `find_reusable_job_for_binding`.
  - `create_job_from_preview`, `_preview_from_persisted_job`, and `_validate_saved_job_plans`.
  - The persisted plan JSON already carries the request, so no job-table column is needed. `_validate_saved_job_plans` must accept a stored plan whose request has `merge: True`.

- [ ] **Step 1: Write failing planner tests**

Add to `tests/test_attendance_correction_planner.py`, reusing its `row`/`core`/`at` helpers. `EMPLOYEE=44`, `WORK_CENTER=72`, `DEPARTMENT=8`, and `row()` defaults to work center 11 and department 3.

```python
def _ops(plan):
    return [(op.kind, op.attendance_id, dict(op.after) if op.after else None) for op in plan.operations]


def test_merge_open_keeps_the_live_row_and_deletes_the_short_rows():
    # Christian 2026-09-18 shape: D3 short, D2 detour, back on D3 and still clocked in.
    rows = [
        row(1, at(7), at(7, 2), work_center=WORK_CENTER, department=DEPARTMENT),
        row(2, at(7, 2), at(7, 4), work_center=99, department=DEPARTMENT),
        row(3, at(7, 4), None, work_center=WORK_CENTER, department=DEPARTMENT),
    ]
    plan = plan_correction(
        rows=rows, employee_odoo_id=EMPLOYEE, start_utc=at(7), end_utc=None,
        odoo_work_center_id=WORK_CENTER, odoo_department_id=DEPARTMENT, merge=True,
    )
    assert plan.request["merge"] is True
    kinds = sorted((op.kind, op.attendance_id) for op in plan.operations)
    assert kinds == [("delete", 1), ("delete", 2), ("update", 3)]
    update = next(op for op in plan.operations if op.kind == "update")
    assert dict(update.after) == {"check_in_utc": at(7)}
    assert [(e["odoo_attendance_id"], e["check_in_utc"], e["check_out_utc"]) for e in plan.expected_intervals] == [
        (3, at(7), None)
    ]


def test_merge_closed_fills_a_same_station_gap():
    rows = [
        row(1, at(8), at(9), work_center=WORK_CENTER, department=DEPARTMENT),
        row(2, at(9, 3), at(10), work_center=WORK_CENTER, department=DEPARTMENT),
    ]
    plan = plan_correction(
        rows=rows, employee_odoo_id=EMPLOYEE, start_utc=at(8), end_utc=at(10),
        odoo_work_center_id=WORK_CENTER, odoo_department_id=DEPARTMENT, merge=True,
    )
    assert [(e["odoo_attendance_id"], e["check_in_utc"], e["check_out_utc"]) for e in plan.expected_intervals] == [
        (1, at(8), at(10))
    ]
    assert sorted((op.kind, op.attendance_id) for op in plan.operations) == [("delete", 2), ("update", 1)]


def test_legacy_closed_request_still_never_bridges_a_gap():
    rows = [
        row(1, at(8), at(9), work_center=WORK_CENTER, department=DEPARTMENT),
        row(2, at(9, 3), at(10), work_center=WORK_CENTER, department=DEPARTMENT),
    ]
    plan = plan_correction(
        rows=rows, employee_odoo_id=EMPLOYEE, start_utc=at(8), end_utc=at(10),
        odoo_work_center_id=WORK_CENTER, odoo_department_id=DEPARTMENT,
    )
    assert "merge" not in plan.request
    assert plan.operations == ()


def test_merge_is_a_no_op_only_for_one_covering_row():
    rows = [row(1, at(8), at(10), work_center=WORK_CENTER, department=DEPARTMENT)]
    plan = plan_correction(
        rows=rows, employee_odoo_id=EMPLOYEE, start_utc=at(8), end_utc=at(10),
        odoo_work_center_id=WORK_CENTER, odoo_department_id=DEPARTMENT, merge=True,
    )
    assert plan.operations == ()


def test_merge_plan_round_trips_through_json_and_rejects_tampering():
    rows = [
        row(1, at(8), at(9), work_center=WORK_CENTER, department=DEPARTMENT),
        row(2, at(9, 3), at(10), work_center=WORK_CENTER, department=DEPARTMENT),
    ]
    plan = plan_correction(
        rows=rows, employee_odoo_id=EMPLOYEE, start_utc=at(8), end_utc=at(10),
        odoo_work_center_id=WORK_CENTER, odoo_department_id=DEPARTMENT, merge=True,
    )
    assert plan_from_json(plan_to_json(plan)) == plan
    payload = json.loads(json.dumps(plan_to_json(plan)))
    # Dropping the flag must not validate: the operations no longer implement the request.
    _drop_request_key(payload, "merge")  # helper: remove the key wherever the request is serialized
    with pytest.raises((ValueError, TypeError)):
        plan_from_json(payload)
```

Write `_drop_request_key` against the actual JSON shape of `plan_to_json` (inspect it first). Also add:
- **Survivor preference:** an open merge where an earlier closed row starts exactly at `start`. The open row must still be the survivor.
- **Remainders:** a closed merge whose first row starts before `start` (it gets a left remainder) and whose last row ends after `end` (it gets a right remainder).
- **Station change:** a closed merge across a detour at another station (rows at 72, 99, 72, no gaps) becomes one row at 72.

- [ ] **Step 2: Run to verify they fail**

Run `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_attendance_correction_planner.py -q -k merge`. Expected: failures on the unexpected `merge` keyword.

- [ ] **Step 3: Implement** the behavior above in `attendance_corrections.py`. Add `_closed_merge_pieces` and an open-merge survivor rule next to `_closed_pieces` and `_open_pieces`, rather than changing those legacy functions.

- [ ] **Step 4: Run the whole correction test surface**

Run `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/ -q -k "correction or attendance_correction or exceptions" -p no:cacheprovider`. Every pre-existing test must pass unedited.

- [ ] **Step 5: Add preview and job plumbing tests.** Use the fakes the existing correction-job tests use; find them with `grep -rln create_job_from_preview tests`. Test that a merge preview creates a job whose persisted plan validates on reload. Test that a legacy job persisted before this change, as a fixture JSON without `merge`, still validates.

- [ ] **Step 6: Lint and commit**

```bash
git add src/zira_dashboard/attendance_corrections.py tests/
git commit -m "feat: add merge mode to Odoo attendance corrections

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Correction engine — no-overlap order, fixer attempt cap, fixer audit and failure hook

**Files:** Modify `src/zira_dashboard/attendance_corrections.py`. Test `tests/test_attendance_corrections_merge_execution.py` (new).

**Behavior:**
1. **`_ordered_operations` (~line 3026):** an `update` whose after-interval extends beyond its source interval, with an earlier `check_in` or a later non-null `check_out`, runs after deletes, and only if it isn't open-producing. That makes the phases:
   - 0: close an open row;
   - 1: non-growing updates;
   - 2: creates;
   - 3: deletes;
   - 3.5: growing closed updates;
   - 4: open-producing.

   Use an integer phase (e.g. 0, 1, 2, 3, 4, 5). Check `_validate_operation_progress` and any reservation logic for assumptions about order.
2. **Fixer jobs:** a job whose `item_key` starts with `quick_punch_fixes.ITEM_KEY_PREFIX` (`"quick-punch:"`) gets special handling. Import the constant lazily, or define the prefix in `attendance_corrections` and have `quick_punch_fixes` import it. Pick one owner and document it.
   - **(a) Attempt cap:** once its `attempt_count` reaches `QUICK_PUNCH_MAX_ATTEMPTS = 6` and it hits another recoverable failure, it transitions to `failed` with a job event (outcome `failed`, reason `attempt_limit`) instead of scheduling a retry.

     *As built, after review:* the cap applies only while no Odoo write has completed and the job hasn't been verified. After the first write, or after verification, fixer jobs retry uncapped until they converge, like manager jobs. A job counts as a fixer job only when the `item_key` prefix AND `actor_email == QUICK_PUNCH_ACTOR_UPN` both match. The constants `QUICK_PUNCH_ITEM_KEY_PREFIX`, `QUICK_PUNCH_ACTOR_UPN` and `QUICK_PUNCH_ACTOR_NAME` are owned by `attendance_corrections`. Fixer failures are left out of the attendance readiness blockers. The failure event detail carries the completed/total operations, the deleted IDs, and the original cause.
   - **(b) Completion audit:** `_complete_with_audit` records `action="quick_punch_merged"`, `item_kind="quick_punch_fix"`, `category_label="Quick-punch auto-fix"`, `person_name` (from the job's stored person name, if the job carries one; otherwise leave it None), `actor_upn`/`actor_name` from the job, `before_value`/`after_value` from the job's stored summaries, and `source="auto"`.
   - **Summaries:** store both on the job at creation, in a new nullable `audit_summary` JSONB column on `attendance_correction_jobs`. Add it with `ALTER TABLE … ADD COLUMN IF NOT EXISTS` in `_schema.py`, and pass it through `create_job_from_preview(..., audit_summary=None)`.
   - **Manager jobs:** they keep their exact current audit event.
   - **(c) Failure hook:** when a fixer job reaches `failed` for any reason, record an `inbox_events` row: `item_kind="quick_punch_fix"`, `action="quick_punch_failed"`, the same actor/person, and `outcome` = the failure reason. Task 6 turns it into an alert.
3. **Fake-Odoo end-to-end:** write the new test file against the fake facade the existing job tests use. Extend or wrap it so that `create`/`update` raise the same error Odoo raises for overlapping attendance, whenever the result would overlap another existing row of that employee. Then prove:
   - Christian's open merge completes, the open row keeps its ID, and there are no overlaps at any step.
   - A closed same-station gap merge completes.
   - A legacy manager closed correction that previously extended before deleting now completes too.
   - The attempt cap fails a fixer job after 6 recoverable errors and writes the failure event. A manager job under the same errors keeps retrying.
   - A fixer job completion writes `quick_punch_merged` with before/after. A manager job still writes `corrected_odoo_attendance` unchanged.

- [ ] Steps: write the failing tests, run them red, implement, run the correction test surface plus the new file green, lint, then commit `feat: run Odoo merges without overlaps and cap fixer retries`.

---

### Task 3: Off / Preview / Live setting

**Files:**
- Create `src/zira_dashboard/quick_punch_fix_settings.py`.
- Modify `src/zira_dashboard/_schema.py`, `routes/settings.py`, `settings_context.py`, `templates/settings.html`, `static/settings.js`.
- Test `tests/test_quick_punch_fix_settings.py`.

**Schema** (add next to the `auto_lunch_settings` block in `_schema.py`, ~line 1611):

```sql
CREATE TABLE IF NOT EXISTS quick_punch_fix_settings (
  id    INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  mode  TEXT NOT NULL DEFAULT 'preview' CHECK (mode IN ('off','preview','live'))
);
INSERT INTO quick_punch_fix_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS quick_punch_fix_setting_events (
  id          BIGSERIAL PRIMARY KEY,
  before_mode TEXT CHECK (before_mode IN ('off','preview','live')),
  after_mode  TEXT NOT NULL CHECK (after_mode IN ('off','preview','live')),
  actor_upn   TEXT,
  actor_name  TEXT,
  source      TEXT NOT NULL CHECK (source IN ('settings','external','baseline')),
  changed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS quick_punch_fix_setting_events_changed_at_idx
  ON quick_punch_fix_setting_events (changed_at DESC, id DESC);
```

**Module:** mirror `auto_lunch_settings.py` exactly in structure:
- A frozen `Settings(mode: str = "preview")` with `MODES = ("off", "preview", "live")`.
- `CachedSingleton` loader, falling back to `DEFAULT` when the table has no row.
- A distinct advisory-lock key (ASCII `"QPFIXSET"` as a signed 64-bit int).
- `save(settings, *, actor_upn, actor_name, source="settings") -> bool`, which audits only on change and rejects unknown modes and sources.
- `recent_events(limit)`.
- `reconcile_external_change()`, which logs an `external` event when the row differs from the latest event's `after_mode`, a `baseline` when there are no events, and refreshes the cache.
- Call `reconcile_external_change()` from the same 20 s inbox-warm path that calls `auto_lunch_guard.refresh()` (`page_warmer.warm_inbox_once`). Wrap it in try/except so it can never break that tick.

**UI:**
- **Section:** Settings → Timeclock, directly after the Auto-Lunch form. Title "Quick-punch fixer".
- **Radio** `name=mode` with three labeled options:
  - **Off:** "Don't touch Odoo."
  - **Preview:** "List what would be fixed in the Exception Inbox archive, but don't change Odoo."
  - **Live:** "Fix quick sign-in mistakes in Odoo automatically."
- **Help text:** "Merges sign-outs and wrong-station taps fixed within 5 minutes into one continuous Odoo record. Never touches days payroll has processed."
- **History:** a change list like Auto-Lunch's.
- **Route** `POST /settings/quick_punch_fix` records the actor from `inbox_log.actor_from(request)`. Follow the Auto-Lunch route (`routes/settings.py` ~921) for response and redirect conventions and autosave (`static/settings.js` ~318).

**Tests:**
- The default is Preview.
- Saving a change writes one audit event with before and after, and saving the same value writes none.
- An invalid mode or source raises.
- Reconcile logs an `external` event after a direct DB change, and a `baseline` when there is no history.
- The settings page renders the three options with the current one checked, and the POST round-trips.

DB-backed tests need local Postgres. Follow `tests/test_auto_lunch_settings*.py` for their fixtures and skip markers.

- [ ] Steps: red, implement, green, lint, then commit `feat: add Off/Preview/Live setting for the quick-punch fixer`.

---

### Task 4: Pure detection — `quick_punch_fixes.find_fixes`

**Files:**
- Create `src/zira_dashboard/quick_punch_fixes.py`.
- Modify `src/zira_dashboard/assignment_windows.py`: rename `_blocking_windows` to public `blocking_windows`, keeping the private name as an alias for existing callers or updating them.
- Test `tests/test_quick_punch_fixes.py`.

- [ ] **Step 1: Write failing tests** (full file):

```python
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from zira_dashboard.attendance_timeline import LocationSpan
from zira_dashboard.quick_punch_fixes import (
    ITEM_KEY_PREFIX,
    MeterFreshness,
    find_fixes,
)

CT = ZoneInfo("America/Chicago")


def ct(hour, minute=0, second=0):
    return datetime(2026, 9, 18, hour, minute, second, tzinfo=CT).astimezone(UTC)


def span(att_id, wc, start, end, *, emp=8, name="Christian C.", status="valid", is_open=False):
    return LocationSpan(
        employee_odoo_id=emp, employee_name=name, start_utc=start, end_utc=end,
        status=status, app_work_center_name=wc if status == "valid" else None,
        odoo_work_center_id=None, odoo_work_center_name=wc,
        attendance_ids=(att_id,), department_repair=None, is_open=is_open,
    )


FRESH = {
    name: MeterFreshness(last_reading_at=ct(23), truncated=False)
    for name in ("Dismantler 1", "Dismantler 2", "Dismantler 3", "Dismantler 4")
}
IDLE = {name: () for name in FRESH}
BREAKS = ((ct(9), ct(9, 15)), (ct(11), ct(11, 30)))


def christian(now):
    return (
        span(6190, "Dismantler 3", ct(7), ct(7, 2, 10)),
        span(6207, "Dismantler 2", ct(7, 2, 10), ct(7, 4, 27)),
        span(6208, "Dismantler 3", ct(7, 4, 27), now, is_open=True),
    )


def scan(spans, now, **kw):
    kw.setdefault("production_times_by_wc", IDLE)
    kw.setdefault("meter_freshness_by_wc", FRESH)
    kw.setdefault("breaks", BREAKS)
    return find_fixes(spans, now_utc=now, **kw)


def test_christians_detour_becomes_one_open_fix_after_the_settle_delay():
    now = ct(7, 7)
    result = scan(christian(now), now)
    (fix,) = result.fixes
    assert (fix.employee_odoo_id, fix.wc_name, fix.start_utc, fix.end_utc) == (8, "Dismantler 3", ct(7), None)
    assert fix.source_attendance_ids == (6190, 6207, 6208)
    assert fix.item_key.startswith(f"{ITEM_KEY_PREFIX}8:")
    assert [(s.wc_name, s.start_utc) for s in fix.before] == [
        ("Dismantler 3", ct(7)), ("Dismantler 2", ct(7, 2, 10)), ("Dismantler 3", ct(7, 4, 27)),
    ]


def test_fix_waits_for_the_settle_delay():
    now = ct(7, 5)  # only 33 s after the return at 07:04:27
    result = scan(christian(now), now)
    assert result.fixes == ()
    assert [s.reason for s in result.skipped] == ["settling"]


def test_unsettled_first_pick_at_0703_is_not_fixed():
    now = ct(7, 3)
    spans = (
        span(6190, "Dismantler 3", ct(7), ct(7, 2, 10)),
        span(6207, "Dismantler 2", ct(7, 2, 10), now, is_open=True),
    )
    result = scan(spans, now)
    assert result.fixes == ()
    assert "first_pick_not_settled" in [s.reason for s in result.skipped] or "settling" in [
        s.reason for s in result.skipped
    ]


def test_first_pick_is_fixed_once_the_new_station_sticks():
    now = ct(7, 9)
    spans = (
        span(6190, "Dismantler 3", ct(7), ct(7, 2, 10)),
        span(6207, "Dismantler 2", ct(7, 2, 10), now, is_open=True),
    )
    (fix,) = scan(spans, now).fixes
    assert (fix.wc_name, fix.start_utc, fix.end_utc) == ("Dismantler 2", ct(7), None)


def test_stale_meter_for_the_blip_station_waits():
    now = ct(7, 7)
    fresh = dict(FRESH, **{"Dismantler 2": MeterFreshness(last_reading_at=ct(7, 3), truncated=False)})
    result = scan(christian(now), now, meter_freshness_by_wc=fresh)
    assert result.fixes == ()
    assert [s.reason for s in result.skipped] == ["meter_not_current"]


def test_truncated_or_missing_meter_waits():
    now = ct(7, 7)
    for fresh in (
        dict(FRESH, **{"Dismantler 2": MeterFreshness(last_reading_at=ct(23), truncated=True)}),
        {k: v for k, v in FRESH.items() if k != "Dismantler 2"},
    ):
        assert scan(christian(now), now, meter_freshness_by_wc=fresh).fixes == ()


def test_plain_same_station_sign_out_gap_needs_no_meter():
    now = ct(8, 30)
    spans = (
        span(1, "Dismantler 3", ct(7), ct(8)),
        span(2, "Dismantler 3", ct(8, 3), now, is_open=True),
    )
    (fix,) = scan(spans, now, meter_freshness_by_wc={}).fixes
    assert (fix.start_utc, fix.end_utc, fix.source_attendance_ids) == (ct(7), None, (1, 2))


def test_closed_merge_has_an_end():
    now = ct(10)
    spans = (
        span(1, "Dismantler 3", ct(7), ct(8)),
        span(2, "Dismantler 3", ct(8, 3), ct(8, 50)),
        span(3, "Dismantler 1", ct(8, 50), now, is_open=True),
    )
    (fix,) = scan(spans, now).fixes
    assert (fix.wc_name, fix.start_utc, fix.end_utc) == ("Dismantler 3", ct(7), ct(8, 50))


def test_blip_or_gap_touching_a_break_is_never_fixed():
    now = ct(10)
    spans = (
        span(1, "Dismantler 3", ct(8), ct(9, 1)),
        span(2, "Dismantler 3", ct(9, 3), now, is_open=True),
    )
    result = scan(spans, now)
    assert result.fixes == ()
    assert [s.reason for s in result.skipped] == ["break"]


def test_nothing_to_fix_when_one_row_was_split_into_two_spans():
    now = ct(10)
    spans = (
        span(1, "Dismantler 3", ct(7), ct(8)),
        span(1, "Dismantler 3", ct(8), now, is_open=True),
    )
    assert scan(spans, now).fixes == ()


def test_item_key_is_stable_and_changes_with_new_rows():
    now = ct(7, 7)
    first = scan(christian(now), now).fixes[0].item_key
    assert scan(christian(ct(7, 8)), ct(7, 8)).fixes[0].item_key == first
    spans = (*christian(now)[:2], span(6208, "Dismantler 3", ct(7, 4, 27), ct(8)),
             span(6300, "Dismantler 1", ct(8), ct(8, 2)), span(6301, "Dismantler 3", ct(8, 2), ct(9)))
    assert scan(spans, ct(9, 30)).fixes[0].item_key != first


def test_conflict_spans_block_as_in_the_dashboards():
    now = ct(10)
    spans = (
        span(1, "Dismantler 3", ct(7), ct(8)),
        span(9, None, ct(8), ct(8, 2), status="conflicting_location"),
        span(2, "Dismantler 3", ct(8, 2), now, is_open=True),
    )
    assert scan(spans, now).fixes == ()
```

- [ ] **Step 2: Run red.** `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_quick_punch_fixes.py -q` fails on import.

- [ ] **Step 3: Implement** `src/zira_dashboard/quick_punch_fixes.py`:

```python
"""Plan quick-punch fixes for Odoo from one day's location spans.

Runs the dashboards' smoothing (``quick_punch_smoothing``) over valid Odoo
location spans and turns each merged stint into a ``PlannedFix`` -- but only
once it is settled, i.e. future punches can no longer change the answer.

Pure -- no DB, no network, no clock.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
from typing import TYPE_CHECKING

from . import assignment_windows, quick_punch_smoothing

if TYPE_CHECKING:
    from .attendance_timeline import LocationSpan

ITEM_KEY_PREFIX = "quick-punch:"
SETTLE_DELAY = timedelta(minutes=2)


@dataclass(frozen=True)
class MeterFreshness:
    last_reading_at: datetime | None
    truncated: bool


@dataclass(frozen=True)
class SourceStint:
    wc_name: str
    start_utc: datetime
    end_utc: datetime
    is_open: bool
    attendance_ids: tuple[int, ...]


@dataclass(frozen=True)
class PlannedFix:
    employee_odoo_id: int
    person_name: str
    wc_name: str
    start_utc: datetime
    end_utc: datetime | None  # None: the person is still clocked into it
    source_attendance_ids: tuple[int, ...]
    before: tuple[SourceStint, ...]
    item_key: str


@dataclass(frozen=True)
class SkippedFix:
    employee_odoo_id: int
    person_name: str
    reason: str  # settling | first_pick_not_settled | meter_not_current | break
    before: tuple[SourceStint, ...]


@dataclass(frozen=True)
class FixScan:
    fixes: tuple[PlannedFix, ...]
    skipped: tuple[SkippedFix, ...]


def item_key_for(employee_odoo_id: int, attendance_ids: Sequence[int]) -> str:
    joined = ",".join(str(value) for value in sorted(set(attendance_ids)))
    digest = hashlib.sha256(joined.encode()).hexdigest()[:24]
    return f"{ITEM_KEY_PREFIX}{employee_odoo_id}:{digest}"


def _overlaps(start: datetime, end: datetime, windows: Sequence[tuple[datetime, datetime]]) -> bool:
    return end > start and any(ws < end and we > start for ws, we in windows)


def find_fixes(
    spans: Sequence[LocationSpan],
    *,
    production_times_by_wc: Mapping[str, Sequence[datetime]] | None,
    meter_freshness_by_wc: Mapping[str, MeterFreshness],
    breaks: Sequence[tuple[datetime, datetime]],
    now_utc: datetime,
    limit: timedelta = quick_punch_smoothing.QUICK_PUNCH_LIMIT,
    settle_delay: timedelta = SETTLE_DELAY,
) -> FixScan:
    stints: list[tuple[assignment_windows.WorkSegment, SourceStint]] = []
    for span in spans:
        if span.status != "valid" or not span.app_work_center_name:
            continue
        segment = assignment_windows.WorkSegment(
            wc_name=span.app_work_center_name,
            person_name=span.employee_name,
            start_utc=span.start_utc,
            end_utc=span.end_utc,
            source="odoo",
            person_odoo_id=span.employee_odoo_id,
        )
        stints.append(
            (
                segment,
                SourceStint(
                    wc_name=span.app_work_center_name,
                    start_utc=span.start_utc,
                    end_utc=span.end_utc,
                    is_open=bool(span.is_open),
                    attendance_ids=tuple(span.attendance_ids),
                ),
            )
        )
    inputs = [segment for segment, _stint in stints]
    smoothed = quick_punch_smoothing.smooth_quick_punches(
        inputs,
        blocked_windows=assignment_windows.blocking_windows(spans),
        production_times_by_wc=production_times_by_wc,
        limit=limit,
    )
    input_ids = {id(segment) for segment in inputs}
    fixes: list[PlannedFix] = []
    skipped: list[SkippedFix] = []
    for merged in smoothed:
        if id(merged) in input_ids:
            continue  # untouched real stint
        absorbed = sorted(
            (
                stint
                for segment, stint in stints
                if segment.person_odoo_id == merged.person_odoo_id
                and segment.start_utc >= merged.start_utc
                and segment.end_utc <= merged.end_utc
            ),
            key=lambda item: (item.start_utc, item.end_utc),
        )
        ids = tuple(sorted({value for stint in absorbed for value in stint.attendance_ids}))
        if len(ids) < 2:
            continue  # one Odoo row split into spans: nothing to fix in Odoo
        before = tuple(absorbed)
        employee = int(merged.person_odoo_id)
        name = merged.person_name

        def skip(reason: str) -> None:
            skipped.append(SkippedFix(employee, name, reason, before))

        changed_windows = [
            (stint.start_utc, stint.end_utc) for stint in absorbed if stint.wc_name != merged.wc_name
        ] + [
            (left.end_utc, right.start_utc)
            for left, right in zip(absorbed, absorbed[1:])
            if right.start_utc > left.end_utc
        ]
        if any(_overlaps(start, end, breaks) for start, end in changed_windows):
            skip("break")
            continue
        last = max(absorbed, key=lambda item: item.end_utc)
        is_open = last.is_open and last.end_utc == merged.end_utc
        boundaries = [stint.start_utc for stint in absorbed[1:]] + [
            stint.end_utc for stint in absorbed if stint.end_utc != merged.end_utc
        ]
        if boundaries and now_utc - max(boundaries) < settle_delay:
            skip("settling")
            continue
        if absorbed[0].wc_name != merged.wc_name:
            final_start = min(s.start_utc for s in absorbed if s.wc_name == merged.wc_name)
            effective_end = now_utc if is_open else merged.end_utc
            if effective_end - final_start <= limit:
                skip("first_pick_not_settled")
                continue
        stale = False
        for stint in absorbed:
            if stint.wc_name == merged.wc_name:
                continue
            fresh = meter_freshness_by_wc.get(stint.wc_name)
            if (
                fresh is None
                or fresh.truncated
                or fresh.last_reading_at is None
                or fresh.last_reading_at < stint.end_utc
            ):
                stale = True
                break
        if stale:
            skip("meter_not_current")
            continue
        fixes.append(
            PlannedFix(
                employee_odoo_id=employee,
                person_name=name,
                wc_name=merged.wc_name,
                start_utc=merged.start_utc,
                end_utc=None if is_open else merged.end_utc,
                source_attendance_ids=ids,
                before=before,
                item_key=item_key_for(employee, ids),
            )
        )
    return FixScan(tuple(fixes), tuple(skipped))
```

Note one case explicitly in the docstring or tests. For a first pick whose short stint is at a station with no fresh meter, the smoothing already refuses (unknown means not idle), so no merge and no skip is reported. That's expected.

- [ ] **Step 4: Run green.** Also run `tests/test_quick_punch_smoothing.py tests/test_production_history_odoo_strict.py` after the `blocking_windows` rename.
- [ ] **Step 5: Lint and commit** `feat: plan settled quick-punch fixes for Odoo`.

---

### Task 5: The fixer tick

**Files:**
- Create `src/zira_dashboard/quick_punch_fixer.py`.
- Modify `src/zira_dashboard/app.py`: add `("quick-punch fixer", _tick_quick_punch_fixer, 60)` to `_WARMERS`, with an async wrapper using `asyncio.to_thread` like `_tick_attendance_corrections`.
- Test `tests/test_quick_punch_fixer.py`.

**`quick_punch_fixer.py` API:**
- The system actor is `attendance_corrections.QUICK_PUNCH_ACTOR_UPN` / `QUICK_PUNCH_ACTOR_NAME` (`"system:quick-punch"` / `"Quick-punch auto-fix"`). Import them; don't redefine. `quick_punch_fixes.ITEM_KEY_PREFIX` must likewise be `attendance_corrections.QUICK_PUNCH_ITEM_KEY_PREFIX` (import it).
- `run_for_day(day, *, now_utc, mode, client, apply: bool) -> FixRun` is shared by the tick and the backfill. `FixRun` holds the scan, the payroll-skipped employees, the active-job-skipped employees, the deduped item keys, and the created or previewed keys.
- `tick(now_utc=None) -> None`. It reads `quick_punch_fix_settings.current().mode` and returns immediately when `off`. Otherwise it calls `run_for_day(today, mode=mode, client=deps.client, apply=True)`. It catches and logs any exception (a warning naming the exception type), so the warmer never dies.

**`run_for_day` steps** (each with a unit test using monkeypatched sources; no Odoo, DB, or Zira):
1. **Mirror freshness:** read `attendance_location_snapshot.read_location_snapshot(day, as_of_utc=now)`. If `policy.mirror_owned` is false, or it's unavailable or stale, return an empty run. The fixer only works on the mirror-owned canonical data. Use `snapshot.spans`.
2. **Meters:** `production_history._metered_leaderboard(client, day, now_utc=now if day is today else None)`. Build `production_times_by_wc` with `quick_punch_smoothing.production_times_from_samples`. Build `meter_freshness_by_wc = {r.station.name: MeterFreshness(r.last_reading_at, bool(r.truncated))}`. If the leaderboard fetch raises, return an empty run, because meter-dependent fixes can't be proven.
3. **Breaks:** from `shift_config.breaks_for(day)`, converted to UTC windows the same way `routes/departments.py` does (~line 646).
4. `scan = quick_punch_fixes.find_fixes(...)`.
5. **Pay period:** skip every fix if `day < staffing_hours.current_pay_period_bounds(today)[0]`.
6. **Payroll:** one call, `odoo_client.fetch_payroll_work_entries(employee_ids, day, day)`. Skip employees with any entry whose `state != "draft"` or whose `conflict` is true. On an Odoo error, skip all, since payroll status can't be proven.
7. **Active jobs:** skip employees with any active correction job (`status IN planned, applying, verifying, recalculating`) whose persisted request or plan covers that employee. Add a small query helper in `attendance_corrections`, `active_job_employee_ids() -> set[int]`.
8. **Dedupe:** skip a fix whose `item_key` already has a correction job in any status, or an `inbox_events` row with action `quick_punch_would_merge`.
9. **Preview** (`mode == "preview"`, `apply` true): `inbox_log.record_event` with:
   - `item_kind="quick_punch_fix"`, `item_key=fix.item_key`, `person_name=fix.person_name`;
   - `category_label="Quick-punch auto-fix"`, `action="quick_punch_would_merge"`, `outcome="Preview — Odoo not changed"`;
   - `before_value` = `before_summary(fix)`, `after_value` = `after_summary(fix)`;
   - the system actor, and `source="auto"`.
10. **Live** (`mode == "live"`, `apply` true):
    - Call `attendance_corrections.correction_preview(item_key=fix.item_key, employee_odoo_ids=[fix.employee_odoo_id], target_work_center_name=fix.wc_name, start_utc=fix.start_utc, end_utc=fix.end_utc, merge=True)`.
    - Then `create_job_from_preview(preview, actor_upn=SYSTEM_ACTOR_UPN, actor_name=SYSTEM_ACTOR_NAME, audit_summary={"person_name": ..., "before": ..., "after": ...})`. Match the real signature: `grep -n "def create_job_from_preview" -A20`.
    - If the preview's plan has no operations (already fixed), do nothing.
    - **Pin the source rows** (added after the Task 1 review). The preview re-reads live Odoo, which can differ from the mirror the fix was detected on. After `correction_preview`, create the job only if both of these hold:
      - The Odoo IDs of the plan's source rows that overlap `[fix.start_utc, fix.end_utc or ∞)` equal `fix.source_attendance_ids`.
      - An open row is among them exactly when `fix.end_utc is None`.

      Otherwise skip this fix for the tick and log it at info level. The next tick re-detects from fresh data. This protects against a real station move, or a clock-out such as auto-lunch, that lands during the mirror's lag. Test both cases.
    - `plan_correction` also raises for an open merge without the live row in range (commit `7a98ae8b`). Treat that `ValueError` as a skip.
    - Any exception for one fix is logged and that fix is skipped; others continue.
11. **Summaries:**
    - `before_summary`: `"D3 7:00–7:02 · D2 7:02–7:04 · D3 7:04–now"`. Use Central time and the station's short form: "Dismantler 3" becomes "D3", "Repair 2" becomes "R2", and any other name stays as it is. Use `–` between times, and "now" for an open end.
    - `after_summary`: `"D3 7:00–now"`.
    - Put both helpers in `quick_punch_fixes` (pure) with their own tests.

**Tests:**
- Off does nothing and calls nothing.
- Preview records exactly one event per item key across two ticks, and never touches `correction_preview`.
- Live calls `correction_preview(merge=True)` and `create_job_from_preview` once per key, including across two ticks with the job present.
- A payroll-validated day is skipped, and a payroll error skips all.
- An active job skips the employee.
- A stale mirror or a leaderboard error gives an empty run.
- A day before the pay period is skipped.
- A per-fix exception doesn't stop the others.
- The summary formatting is correct.
- The warmer is registered: assert it is in `app._WARMERS`.

- [ ] Steps: red, implement, green, lint, then commit `feat: fix quick punches in Odoo on a 60-second tick`.

---

### Task 6: Exception Inbox — show fixer events and alert on failures

**Files:**
- Modify `src/zira_dashboard/inbox_log.py`, `routes/exceptions.py` (archive grouping ~995-1033), `static/exceptions.js` (`glyphFor` ~1122, `renderArchiveEvent` ~1160-1212), `static/exceptions.css` if needed, and `exception_inbox.py`.
- Test `tests/test_quick_punch_inbox.py`.

**Behavior:**
1. **Visible by default:** events with `actor_upn = "system:quick-punch"` show in the archive even when "Hide auto-resolved" is checked. They have a non-NULL actor, so `inbox_log.archive` already includes them. Verify, and make sure the actor filter lists "Quick-punch auto-fix".
2. **Rendering** for `quick_punch_would_merge` and `quick_punch_merged`:
   - The line reads "`<outcome>` by Quick-punch auto-fix: `<before_value>` → `<after_value>`".
   - Pass `after_value` (and `source`) through `_group_archive_by_day`.
   - Use a distinct glyph (e.g. ⤳) and label each event: Preview ("Would merge") or Live ("Merged").
   - `quick_punch_failed` renders as a failure line.
   - Other events render exactly as before.
3. **Failure alert:** use an alert key namespace distinct from the job's `quick-punch:` key (e.g. `quick-punch-alert:<job_id>`). Read the failure event's detail (`completed_operations`, `total_operations`, `deleted_attendance_ids`, `cause`). When any operation completed, say plainly that Odoo was partly changed and name the time range to check. Translate reason codes into plain words: `attempt_limit` becomes "Odoo kept refusing the change", and `*source_changed*` becomes "Someone changed these punches in Odoo first". Add an urgent Exception Inbox section, "Quick-punch fixes that need a look", listing fixer jobs in `failed` status from the last 14 days. Each shows the person, the before summary, and the failure reason, with a link to the correction job page or `/exceptions?…` if one exists.
   - Each row has a "Mark checked" action that records `inbox_events` action `quick_punch_failure_ack` with the real actor. Acknowledged failures leave the section.
   - Hide the section when it is empty, like the other inbox categories.
   - Follow how `auto_lunch_guard` publishes its alert (`auto_lunch_guard._alert_for` / `current_snapshot`, wired at `exception_inbox.py` ~48-59, ~648, section ~907-917).
3b. **Also alert on stuck partial fixes** (from the Task 2 re-review). List active fixer jobs that have at least one completed Odoo write and are either at or past `QUICK_PUNCH_MAX_ATTEMPTS` attempts or older than 30 minutes. Label them "Odoo partly changed, still retrying". These jobs retry without a cap, so without this row they would be invisible.
3c. **Engine touch-ups** in `attendance_corrections.py`:
   - `_fixer_progress` catches `Exception`, not only `TypeError`/`ValueError`/`KeyError`. It is diagnostic only and must never stop a job from reaching `failed`.
   - It flags an operation that is reserved but not confirmed completed as `uncertain_operation: true` in the failure detail, and the alert words that as "Odoo may have been changed".
   - Test both: a landed-but-unconfirmed write followed by the cap or a `source_changed` failure.
4. **Don't break auto-resolve logic:** `inbox_log.has_human_event_since` treats any action other than `auto_resolved`/`undo` as human. Fixer events use their own `item_key` namespace (`quick-punch:`), so they can't collide with real inbox items. Add a test proving `has_human_event_since` for an unrelated real item is unaffected.

**Tests:**
- The archive query returns the fixer events with the default filter.
- The grouped archive payload carries `after_value`.
- The JS rendering: follow the repo's existing JS test approach. Check `grep -rln "renderArchiveEvent\|exceptions.js" tests`, or assert the template/static contract the way other inbox tests do.
- The failure section appears for a failed fixer job and disappears after acknowledgement.
- Manager correction events render unchanged.

- [ ] Steps: red, implement, green, lint, then commit `feat: show quick-punch fixes and failures in the Exception Inbox`.

---

### Task 7: Pay-period cleanup script

**Files:** Create `scripts/quick_punch_backfill.py`. Test `tests/test_quick_punch_backfill.py`.

**Behavior:**
- Run it as `python -m scripts.quick_punch_backfill [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--yes]`. It defaults to the current pay period start (`staffing_hours.current_pay_period_bounds(today)[0]`) through yesterday, and clamps `--start` to no earlier than the period start.
- For each day it calls `quick_punch_fixer.run_for_day(day, now_utc=<end of that plant day>, mode="live", client=deps.client, apply=args.yes)`.
  - With `apply=False` the run creates nothing and prints only.
  - Past days use stored meter data through the leaderboard's persisted day path. Truncated or missing meter data is "unknown", which the scan reports as `meter_not_current` skips.
  - For past days the mirror freshness check uses the same snapshot policy. If that policy is only valid for "today", read past days with `attendance_timeline.timeline_for_range(day bounds)` instead. Keep one code path in `run_for_day`, with a `spans` override parameter.
- **Output:** one line per fix (date, person, before → after) and one line per skip (date, person, reason, before). At the end, totals by reason and a reminder: "Dry run — nothing changed. Re-run with --yes to apply."
- `--yes` refuses, with exit code 2 and a clear message, unless `quick_punch_fix_settings.current().mode == "live"`.
- Exit code 0 otherwise.

**Tests:**
- The dry run prints fixes and skips and never calls job creation.
- `--yes` in Preview or Off exits 2.
- `--yes` in Live creates jobs via `run_for_day(apply=True)`.
- `--start` before the pay period is clamped.

- [ ] Steps: red, implement, green, lint, then commit `feat: add dry-run-first quick-punch pay-period cleanup`.

---

### Task 8: Patch notes, full validation, push in Preview, live check

- [ ] **Step 1:** Add a `CHANGELOG.md` entry above the newest date, written for a 10-year-old:

```markdown
## 2026-09-18

### The app can now fix quick sign-in mix-ups in the time clock records

#### Features

- **When someone taps the wrong station, or signs out and back in by mistake, the app can now fix the time clock record too, not just the dashboards.** It joins the pieces into one steady record, so every screen and report matches.
- **It starts in "Preview".** For now it only lists what it would fix in the Exception Inbox history. A manager can switch it to "Live" in Settings when the list looks right.
- **It never changes days payroll has already finished.**

#### Fixes

- **When a manager moves someone's time to a different station, the fix now goes through.** Before, some of these fixes got stuck or failed and had to be redone, like a fix that ended while the person was still clocked in.
```

If a `## 2026-09-18` section already exists, add this as a new `###` deploy entry under it, above the earlier one. The Fixes bullet covers the correction-engine order fixes that ship in the same push (`44841bd4` and the commit "fix: free a still-open row's time before correcting the range before it").

- [ ] **Step 2:** Run the full suite and ruff: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q -p no:cacheprovider` and `.venv/bin/ruff check src tests scripts`. Expect 0 failures.
- [ ] **Step 3:** Read-only production check. Use the prod public DSN with `default_transaction_read_only=on`, the way the smoothing plan's Task 4 did; never pytest. Build today's scan with `quick_punch_fixes.find_fixes` from the mirror spans, no meter data, and today's breaks. Print the fixes and skips. Expect Christian's 2026-09-18 detour, blocked only by `meter_not_current` since no meters are loaded, and nothing unexpected.
- [ ] **Step 4:** Before pushing, run a read-only production `SELECT status, count(*) FROM attendance_correction_jobs WHERE status IN ('planned','applying','verifying','recalculating') GROUP BY status`. It must return no rows, because the new operation order would fail an in-flight legacy job. Then commit the changelog, `git fetch`, and `git push origin main`.
- [ ] **Step 5:** Confirm the Railway deploy is SUCCESS and `/healthz` returns 200. Confirm the new tables exist through a read-only SELECT of `quick_punch_fix_settings` (mode `preview`). A few minutes later, confirm the fixer tick is running without errors: `railway logs` and grep for `quick-punch`.
- [ ] **Step 6:** Hand off to Dale. Tell him where the Preview lines appear and how to switch to Live. After he switches to Live, run `python -m scripts.quick_punch_backfill` (dry run) against production through `railway run`, show him the list, and apply with `--yes` only after his OK.
