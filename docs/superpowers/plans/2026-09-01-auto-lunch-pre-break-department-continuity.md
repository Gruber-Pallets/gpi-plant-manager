# Auto-Lunch Pre-Break Department Continuity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the exact Odoo department from the attendance immediately before lunch on Auto-Lunch's post-lunch attendance, restore the existing local work-center fallback, and safely repair Lauro's September 1 row.

**Architecture:** Extend both open-attendance source paths with canonical department identity, persist that identity on the Auto-Lunch run and return punch, and carry it through immediate and retry Odoo synchronization. An explicit saved department overrides work-center-derived department resolution; a missing saved department retains today's fallback behavior.

**Tech Stack:** Python 3.12, FastAPI service modules, PostgreSQL idempotent schema bootstrap, Odoo XML-RPC facade, pytest, Ruff.

## Global Constraints

- The exact department on the Odoo attendance immediately before lunch is authoritative.
- The local app work center is preserved independently and falls back to the latest local clock-in or transfer-in when the mirror has no mapped work center.
- A missing department never blocks the post-lunch clock-in.
- Immediate sync, retry sync, and adoption of an existing Odoo attendance apply the same explicit department intent.
- Stale-source guards, one-lunch-per-day idempotency, cancellation, and salaried-worker exclusion stay unchanged.
- Do not infer departments from display names, employee home department, or fuzzy work-center matching.
- Do not map Tablets to an Odoo Manufacturing Work Center.
- Run tests with production database and Odoo credentials blanked except for the final guarded production read/repair.
- New What's New text uses short, common words and clearly says whether behavior changed.

---

## File Structure

- `src/zira_dashboard/_schema.py`: owns the three nullable durable columns.
- `src/zira_dashboard/_odoo_attendance.py`: normalizes legacy open-attendance department identity.
- `src/zira_dashboard/attendance_mirror.py`: retains department identity in mirror-owned open rows.
- `src/zira_dashboard/live_cache.py`: gives legacy and mirror consumers one payload contract.
- `src/zira_dashboard/attendance_state.py`: exposes the authoritative current department.
- `src/zira_dashboard/auto_lunch.py`: captures pre-break department, restores WC fallback, writes return intent.
- `src/zira_dashboard/odoo_client.py`: accepts an explicit department override.
- `src/zira_dashboard/timeclock_sync.py`: replays intent during immediate sync, retry, and adoption.
- `tests/test_auto_lunch_schema_static.py`, `tests/test_auto_lunch_schema.py`: protect DDL.
- `tests/test_odoo_open_attendance.py`, `tests/test_attendance_mirror_cutover.py`: protect source parity.
- `tests/test_odoo_client.py`, `tests/test_timeclock_sync_dedup.py`: protect writer/retry behavior.
- `tests/test_auto_lunch_worker.py`, `tests/test_timeclock_state_reconciliation.py`: protect end-to-end carry-through.
- `CHANGELOG.md`: explains the shipped behavior.

---

### Task 1: Add Durable Department Intent Columns

**Files:**
- Modify: `src/zira_dashboard/_schema.py:1241-1248,1562-1580`
- Modify: `tests/test_auto_lunch_schema_static.py`
- Modify: `tests/test_auto_lunch_schema.py:11-56`

**Interfaces:**
- Consumes: idempotent `SCHEMA_DDL` and `_columns(table)`.
- Produces: nullable `timeclock_punches_log.odoo_department_id BIGINT`, `auto_lunch_runs.odoo_department_id BIGINT`, and `auto_lunch_runs.odoo_department_name TEXT`.

- [ ] **Step 1: Write failing schema tests**

Add to `tests/test_auto_lunch_schema_static.py`:

```python
from zira_dashboard._schema import SCHEMA_DDL


def test_auto_lunch_department_continuity_columns_are_idempotent():
    assert (
        "ALTER TABLE timeclock_punches_log\n"
        "  ADD COLUMN IF NOT EXISTS odoo_department_id BIGINT;"
    ) in SCHEMA_DDL
    assert (
        "ALTER TABLE auto_lunch_runs\n"
        "  ADD COLUMN IF NOT EXISTS odoo_department_id BIGINT;"
    ) in SCHEMA_DDL
    assert (
        "ALTER TABLE auto_lunch_runs\n"
        "  ADD COLUMN IF NOT EXISTS odoo_department_name TEXT;"
    ) in SCHEMA_DDL
```

Extend `test_auto_lunch_runs_and_settings_exist`:

```python
assert _columns("timeclock_punches_log") >= {"odoo_department_id"}
assert _columns("auto_lunch_runs") >= {
    "person_odoo_id", "day", "kind", "state", "target_out_at",
    "target_in_at", "wc_name", "odoo_department_id",
    "odoo_department_name", "out_punch_id", "in_punch_id",
}
```

- [ ] **Step 2: Verify RED**

Run:

```bash
env DATABASE_URL= .venv/bin/pytest \
  tests/test_auto_lunch_schema_static.py tests/test_auto_lunch_schema.py -q
```

Expected: static failure for absent `ADD COLUMN`; PostgreSQL tests skip.

- [ ] **Step 3: Add idempotent columns**

Add to `_schema.py`:

```sql
ALTER TABLE timeclock_punches_log
  ADD COLUMN IF NOT EXISTS odoo_department_id BIGINT;

ALTER TABLE auto_lunch_runs
  ADD COLUMN IF NOT EXISTS odoo_department_id BIGINT;
ALTER TABLE auto_lunch_runs
  ADD COLUMN IF NOT EXISTS odoo_department_name TEXT;
```

- [ ] **Step 4: Verify GREEN and commit**

Run Step 2 again, then:

```bash
git add src/zira_dashboard/_schema.py \
  tests/test_auto_lunch_schema_static.py tests/test_auto_lunch_schema.py
git commit -m "feat: store auto-lunch department intent"
```

---

### Task 2: Preserve Department Identity in Both Open-Attendance Sources

**Files:**
- Modify: `src/zira_dashboard/_odoo_attendance.py:582-614`
- Modify: `src/zira_dashboard/attendance_mirror.py:790-801`
- Modify: `src/zira_dashboard/live_cache.py:208-293`
- Modify: `tests/test_odoo_open_attendance.py:16-102`
- Modify: `tests/test_attendance_mirror_cutover.py:55-125,1035-1065`

**Interfaces:**
- Consumes: configured `department_field` and mirror `odoo_department_id/name`.
- Produces: open entries with `odoo_department_id: int | None` and `odoo_department_name: str | None`.

- [ ] **Step 1: Write failing legacy and mirror tests**

In `test_fetch_open_attendances_maps_rows`, configure `x_kiosk_department_id`, include `[4, "Supervisor"]`, assert the field was requested, and expect:

```python
{
    "att_id": 88,
    "employee_odoo_id": 5,
    "check_in": "2026-06-01T11:02:00+00:00",
    "wc_name": "Repair 1",
    "odoo_department_id": 4,
    "odoo_department_name": "Supervisor",
}
```

The row with a false department and the no-field test must expect both department values as `None`.

In `test_complete_baseline_makes_shadow_and_live_read_only_the_mirror`, add the same department fields to the fake mirror row and expected snapshot. Extend the current-open SQL test to assert both values survive.

- [ ] **Step 2: Verify RED**

```bash
env DATABASE_URL= ODOO_URL= ODOO_DB= ODOO_LOGIN= ODOO_API_KEY= \
  .venv/bin/pytest tests/test_odoo_open_attendance.py \
  tests/test_attendance_mirror_cutover.py -q
```

Expected: legacy does not request/return department and mirror adapter drops it.

- [ ] **Step 3: Normalize the legacy Odoo department**

Implement in `_odoo_attendance.fetch_open_attendances`:

```python
fields = ["id", "employee_id", "check_in"]
if wc_field:
    fields.append(wc_field)
if department_field:
    fields.append(department_field)
rows = execute_fn(
    "hr.attendance", "search_read", [("check_out", "=", False)], fields=fields,
)
out: list[dict] = []
for row in rows:
    employee_id = _unwrap_m2o(row.get("employee_id"))
    if not employee_id:
        continue
    department = row.get(department_field) if department_field else None
    out.append({
        "att_id": row["id"],
        "employee_odoo_id": employee_id,
        "check_in": odoo_dt_to_iso(row.get("check_in")),
        "wc_name": (
            app_wc_name_for_odoo_id(_unwrap_m2o(row.get(wc_field)))
            if wc_field else None
        ),
        "odoo_department_id": _unwrap_m2o(department),
        "odoo_department_name": (
            department[1]
            if isinstance(department, (list, tuple)) and len(department) > 1
            else None
        ),
    })
return out
```

- [ ] **Step 4: Retain mirror/cache department fields**

Extend `attendance_mirror.current_open_attendance`'s select:

```python
"SELECT odoo_attendance_id, employee_odoo_id, check_in_utc, "
"odoo_work_center_id, odoo_work_center_name, "
"odoo_department_id, odoo_department_name "
```

Add to both `live_cache.py` candidate dictionaries:

```python
"odoo_department_id": row.get("odoo_department_id"),
"odoo_department_name": row.get("odoo_department_name"),
```

- [ ] **Step 5: Verify GREEN and commit**

Run Step 2 again, then:

```bash
git add src/zira_dashboard/_odoo_attendance.py \
  src/zira_dashboard/attendance_mirror.py src/zira_dashboard/live_cache.py \
  tests/test_odoo_open_attendance.py tests/test_attendance_mirror_cutover.py
git commit -m "feat: retain open attendance departments"
```

---

### Task 3: Carry Explicit Department Intent Through Sync and Retry

**Files:**
- Modify: `src/zira_dashboard/odoo_client.py:892-933`
- Modify: `src/zira_dashboard/timeclock_sync.py:34-163`
- Modify: `tests/test_odoo_client.py:245-290`
- Modify: `tests/test_timeclock_sync_dedup.py:20-83`

**Interfaces:**
- Consumes: punch key `odoo_department_id`.
- Produces: `clock_in(..., *, odoo_department_id: int | None = None) -> int`.

- [ ] **Step 1: Write failing payload and sync tests**

Add Odoo writer tests proving explicit ID 4 overrides work-center-derived ID 8 and a missing override still uses ID 8:

```python
result = odoo_client.clock_in(
    26, "Tablets", datetime(2026, 9, 1, 16, 30, tzinfo=timezone.utc),
    odoo_department_id=4,
)
assert result == 5257
assert execute.call_args.args[2]["x_kiosk_department_id"] == 4
```

Add `"odoo_department_id": 4` to `_row` in `test_timeclock_sync_dedup.py`. Assert create receives the keyword and adoption invokes:

```python
set_department.assert_called_once_with(99, 4)
```

Add a test that both `retry_unsynced_punches` and `sync_one_by_id` query SQL contains `odoo_department_id`.

- [ ] **Step 2: Verify RED**

```bash
env DATABASE_URL= ODOO_URL= ODOO_DB= ODOO_LOGIN= ODOO_API_KEY= \
  .venv/bin/pytest tests/test_odoo_client.py \
  tests/test_timeclock_sync_dedup.py -q
```

Expected: `clock_in` rejects the keyword and sync omits the explicit write.

- [ ] **Step 3: Add explicit payload precedence**

Change the writer to:

```python
def _attendance_create_payload(
    employee_odoo_id: int, wc_name: str | None, check_in: datetime, *,
    odoo_department_id: int | None = None,
) -> dict[str, Any]:
    payload = {
        "employee_id": employee_odoo_id,
        "check_in": _to_odoo_dt(check_in),
        "in_mode": "kiosk",
        "overtime_status": "approved",
    }
    wc_field = _kiosk_wc_field()
    if wc_field and (odoo_wc_id := _odoo_work_center_id_for_wc(wc_name)):
        payload[wc_field] = odoo_wc_id
    dept_field = _kiosk_department_field()
    if dept_field:
        dept_id = (
            int(odoo_department_id)
            if odoo_department_id is not None
            else _department_id_for_wc(wc_name)
        )
        if dept_id:
            payload[dept_field] = dept_id
    return payload
```

Add the keyword-only argument to `clock_in` and pass it through to this helper.

- [ ] **Step 4: Replay intent in every sync path**

Add `odoo_department_id` to both sync select lists. In `_retry_one`:

```python
odoo_department_id = r.get("odoo_department_id")
if action in ("clock_in", "transfer_in"):
    existing = odoo_client.get_current_attendance(person_odoo_id)
    if existing:
        if wc_name is not None:
            odoo_client.set_attendance_wc(existing["id"], wc_name)
        if odoo_department_id is not None:
            odoo_client.set_attendance_department_id(
                existing["id"], int(odoo_department_id)
            )
        _mark_synced(r["id"], existing["id"])
        return
    att_id = odoo_client.clock_in(
        person_odoo_id, wc_name, ts,
        odoo_department_id=(
            int(odoo_department_id) if odoo_department_id is not None else None
        ),
    )
    _mark_synced(r["id"], att_id)
    return
```

- [ ] **Step 5: Verify GREEN and commit**

Run Step 2 again, then:

```bash
git add src/zira_dashboard/odoo_client.py src/zira_dashboard/timeclock_sync.py \
  tests/test_odoo_client.py tests/test_timeclock_sync_dedup.py
git commit -m "feat: replay attendance department intent"
```

---

### Task 4: Capture Pre-Break Department and Restore Mirror WC Fallback

**Files:**
- Modify: `src/zira_dashboard/attendance_state.py:21-210`
- Modify: `src/zira_dashboard/auto_lunch.py:155-280,361-566`
- Modify: `tests/test_timeclock_state_reconciliation.py:55-120`
- Modify: `tests/test_attendance_mirror_cutover.py:850-1010`
- Modify: `tests/test_auto_lunch_worker.py:45-82,174-205`

**Interfaces:**
- Consumes: snapshot department keys and durable columns.
- Produces: current-state department keys, run/punch carry-through, mirror blank-WC fallback.

- [ ] **Step 1: Write failing state and Lauro-path tests**

Extend the open snapshot in `test_forgot_to_punch_in_added_in_odoo_shows_clock_out` with department 4 / Supervisor and assert:

```python
assert st["current_odoo_department_id"] == 4
assert st["current_odoo_department_name"] == "Supervisor"
```

Add equivalent `None` assertions for local-only state.

Add a mirror-owned Auto-Lunch test with person 26, blank `wc_name`, department 4 / Supervisor, and a local latest punch `Tablets`. Assert `_advance_person` receives `latest_in_wc="Tablets"` and the local batch query runs once for `{26}`.

- [ ] **Step 2: Write failing persistence tests**

Extend `test_scheduled_auto_out_then_auto_in` with department 4 / Supervisor. Assert lunch-out run fields and lunch-in punch intent:

```python
assert run["wc_name"] == "Bay 3"
assert run["odoo_department_id"] == 4
assert run["odoo_department_name"] == "Supervisor"
assert [(r["action"], r["odoo_department_id"]) for r in rows] == [
    ("clock_out", None), ("clock_in", 4),
]
```

- [ ] **Step 3: Verify RED**

```bash
env DATABASE_URL= .venv/bin/pytest \
  tests/test_timeclock_state_reconciliation.py \
  tests/test_attendance_mirror_cutover.py tests/test_auto_lunch_worker.py -q
```

Expected: DB-free failures show missing state/fallback; DB tests skip.

- [ ] **Step 4: Expose authoritative department in state**

Add:

```python
def _department_state(entry: dict | None) -> dict:
    return {
        "current_odoo_department_id": (
            entry.get("odoo_department_id") if entry else None
        ),
        "current_odoo_department_name": (
            entry.get("odoo_department_name") if entry else None
        ),
    }
```

Merge `_department_state(None)` into local/unavailable/clocked-out states and `_department_state(entry)` into authoritative clocked-in states. Never derive ID from name.

- [ ] **Step 5: Persist run and punch intent**

Add both department fields to `_get_run`, `_get_runs_bulk`, `_upsert_run`, SQL parameters, and `COALESCE` conflict updates. Change `_write_auto_punch`:

```python
def _write_auto_punch(
    person_odoo_id, action, wc_name, occurred_at, *,
    odoo_department_id=None, cur,
) -> int:
    cur.execute(
        "INSERT INTO timeclock_punches_log "
        "(person_odoo_id, action, wc_name, odoo_department_id, "
        "occurred_at, rounded_at, source) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'auto_lunch') RETURNING id",
        (person_odoo_id, action, wc_name, odoo_department_id,
         occurred_at, occurred_at),
    )
    return cur.fetchone()["id"]
```

At lunch-out save `state.get("current_odoo_department_id")` and name on the run. At lunch-in copy `run.get("odoo_department_id")` to the punch.

- [ ] **Step 6: Restore local WC fallback in mirror mode**

Make `_latest_in_wc` return a mirror value only when nonblank; otherwise continue to its local query. In `run_tick`:

```python
if source.mirror_owned:
    missing_wc_ids = set()
    for pid in candidates:
        entry = snapshot.get(str(pid)) or snapshot.get(pid) or {}
        latest_in_wcs[pid] = entry.get("wc_name")
        if not latest_in_wcs[pid]:
            missing_wc_ids.add(pid)
    if missing_wc_ids:
        _unused_first_ins, fallback_wcs = _legacy_attendance_inputs_bulk(
            missing_wc_ids, today
        )
        for pid in missing_wc_ids:
            latest_in_wcs[pid] = fallback_wcs.get(pid)
```

Never replace a nonblank mirror work center.

- [ ] **Step 7: Verify GREEN and commit**

Run Step 3 again. If an explicitly safe local `_test` DSN exists, also run schema/worker DB tests with it; never substitute `DATABASE_PUBLIC_URL`. Then:

```bash
git add src/zira_dashboard/attendance_state.py src/zira_dashboard/auto_lunch.py \
  tests/test_timeclock_state_reconciliation.py \
  tests/test_attendance_mirror_cutover.py tests/test_auto_lunch_worker.py
git commit -m "fix: preserve department through auto lunch"
```

---

### Task 5: Validate, Push, Deploy, and Repair Lauro

**Files:**
- Modify: `CHANGELOG.md`
- Verify: all Task 1-4 files
- Production target: Lauro's September 1 post-lunch attendance only

**Interfaces:**
- Consumes: `fetch_attendance_rows_by_ids(ids)` and `set_attendance_department_id(id, department_id)`.
- Produces: pushed code, verified deployment, Supervisor readback for Lauro.

- [ ] **Step 1: Add shipped What's New note**

```markdown
### Keep each worker's team after lunch

- **Auto-Lunch now puts a worker back in the same Odoo team they had just before lunch.** It also remembers the worker's last plant spot when Odoo has no spot saved, so the afternoon record stays useful.
```

- [ ] **Step 2: Run focused verification**

```bash
env DATABASE_URL= FEEDBACK_SYNC_TEST_DATABASE= \
  ODOO_URL= ODOO_DB= ODOO_LOGIN= ODOO_API_KEY= \
  ODOO_KIOSK_WC_FIELD= ODOO_KIOSK_DEPARTMENT_FIELD= \
  .venv/bin/pytest \
  tests/test_auto_lunch_schema_static.py tests/test_auto_lunch_schema.py \
  tests/test_odoo_open_attendance.py tests/test_attendance_mirror.py \
  tests/test_attendance_mirror_cutover.py \
  tests/test_timeclock_state_reconciliation.py \
  tests/test_timeclock_sync_dedup.py tests/test_odoo_client.py \
  tests/test_auto_lunch_decide.py tests/test_auto_lunch_worker.py \
  tests/test_attendance_timeline.py tests/test_attendance_location_policy.py \
  tests/test_attendance_exceptions.py \
  tests/test_exception_inbox_attendance.py -q
```

Expected: all runnable tests pass; PostgreSQL tests skip safely.

- [ ] **Step 3: Run complete verification**

```bash
env DATABASE_URL= FEEDBACK_SYNC_TEST_DATABASE= \
  ODOO_URL= ODOO_DB= ODOO_LOGIN= ODOO_API_KEY= \
  ODOO_KIOSK_WC_FIELD= ODOO_KIOSK_DEPARTMENT_FIELD= \
  .venv/bin/pytest -q
.venv/bin/ruff check .
git diff --check
```

Expected: full runnable suite passes, Ruff is clean, diff check is silent.

- [ ] **Step 4: Commit, merge safely, and push**

```bash
git add CHANGELOG.md
git commit -m "docs: explain auto-lunch department fix"
git push origin main
```

If origin advanced, merge without discarding unrelated work, rerun Steps 2-3, then push.

- [ ] **Step 5: Verify deployment read-only**

After main deploys, directly read attendance IDs 5212 and 5257 and information-schema. Assert both belong to employee 26, row 5212 proves department 4 / Supervisor, and the new run/punch columns exist. Do not print credentials.

- [ ] **Step 6: Repair only the proven row and read it back**

If row 5257 already has department 4, make no write. Otherwise require:

```python
from datetime import UTC, datetime

before, after = odoo_client.fetch_attendance_rows_by_ids([5212, 5257])
assert before["employee_odoo_id"] == after["employee_odoo_id"] == 26
assert before["odoo_department_id"] == 4
assert before["odoo_department_name"] == "Supervisor"
assert after["odoo_department_id"] is None
assert after["check_in_utc"] == datetime(2026, 9, 1, 16, 30, tzinfo=UTC)
odoo_client.set_attendance_department_id(5257, 4)
verified = odoo_client.fetch_attendance_rows_by_ids([5257])
assert verified[0]["employee_odoo_id"] == 26
assert verified[0]["check_in_utc"] == datetime(2026, 9, 1, 16, 30, tzinfo=UTC)
assert verified[0]["odoo_department_id"] == 4
assert verified[0]["odoo_department_name"] == "Supervisor"
```

Do not alter employee, times, work center, or any other field.

- [ ] **Step 7: Verify the Inbox result and hand off**

After normal mirror refresh, verify Lauro's affected `attendance_missing_location` item is absent because Supervisor is exempt. Report root cause, commits, test/Ruff results, schema/source readback, Lauro row readback, Inbox result, and preservation of unrelated working files.
