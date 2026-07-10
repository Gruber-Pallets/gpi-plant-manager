# Codebase Architecture Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decompose the app's highest-friction Odoo and route internals into focused, testable modules without changing behavior, routes, integration calls, or rendered appearance.

**Architecture:** Keep `odoo_client.py` and the existing route modules as compatibility facades. Move domain logic into private modules that receive patchable dependencies at call time, then extract pure route computations behind stable HTTP wrappers. Finish with only high-signal hygiene changes and a full contract-focused verification pass.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, HTMX, psycopg2/Postgres, Odoo XML-RPC, pytest, Ruff.

## Global Constraints

- Preserve every HTTP route, method, parameter, redirect, status code, JSON shape, template name, and template context key.
- Preserve rendered HTML structure and all static CSS/JavaScript assets.
- Preserve `zira_dashboard.odoo_client` functions, exceptions, constants, caches, and established monkeypatch seams.
- Preserve database schema, SQL results, cache lifetimes, background cadence, external call ordering, and error behavior.
- Do not change cache TTLs, add speculative memoization, rewrite queries broadly, or mass-format the repository.
- Use characterization tests before each extraction and run focused tests immediately afterward.
- Stage only task-owned files; `.claude/` is pre-existing untracked user state and must remain untouched.

---

### Task 1: Lock the Odoo facade compatibility contract

**Files:**
- Create: `tests/test_odoo_facade_contract.py`
- Read: `src/zira_dashboard/odoo_client.py`

**Interfaces:**
- Consumes: current `zira_dashboard.odoo_client` functions and mutable module state.
- Produces: characterization tests proving facade dependency lookup happens at call time and private caches remain assignable.

- [ ] **Step 1: Write the facade characterization tests**

```python
from zira_dashboard import odoo_client


def test_facade_uses_execute_replaced_after_import(monkeypatch):
    calls = []

    def fake(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        if model == "hr.skill.type":
            return [{"id": 1, "name": "Production Skills"}]
        if model == "hr.skill":
            return [{"id": 11, "name": "Planer", "skill_type_id": [1, "Production Skills"]}]
        raise AssertionError(model)

    monkeypatch.setattr(odoo_client, "execute", fake)
    assert odoo_client.fetch_skill_columns_with_types() == [
        {"id": 11, "name": "Planer", "type": "Production Skills"}
    ]
    assert [call[0:2] for call in calls] == [
        ("hr.skill.type", "search_read"),
        ("hr.skill", "search_read"),
    ]


def test_facade_leave_cache_remains_assignable(monkeypatch):
    expected = [{"id": 7, "name": "Vacation", "request_unit": "day"}]
    monkeypatch.setattr(odoo_client, "_leave_types_cache", (expected, float("inf")))
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cache miss")),
    )
    assert odoo_client.fetch_leave_types() is expected


def test_facade_department_helper_is_resolved_at_call_time(monkeypatch):
    calls = []
    monkeypatch.setenv("ODOO_KIOSK_DEPARTMENT_FIELD", "x_kiosk_department_id")
    monkeypatch.setattr(odoo_client, "_department_id_for_wc", lambda wc: 44)
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda model, method, *args, **kwargs: calls.append(
            (model, method, args, kwargs)
        )
        or 91,
    )
    attendance_id = odoo_client.clock_in(3, "Repair 2", odoo_client.datetime.now(odoo_client.UTC))
    assert attendance_id == 91
    assert calls[-1][0:2] == ("hr.attendance", "create")
    assert calls[-1][2][0]["x_kiosk_department_id"] == 44
```

- [ ] **Step 2: Run the new tests against the pre-refactor implementation**

Run: `.venv/bin/pytest tests/test_odoo_facade_contract.py -q`

Expected: `3 passed`; these are characterization tests and must pass before extraction.

- [ ] **Step 3: Commit the contract tests**

```bash
git add tests/test_odoo_facade_contract.py
git commit -m "test: lock Odoo facade compatibility"
```

---

### Task 2: Extract Odoo skill operations

**Files:**
- Create: `src/zira_dashboard/_odoo_skills.py`
- Modify: `src/zira_dashboard/odoo_client.py:157-329,529-582`
- Test: `tests/test_odoo_client.py`
- Test: `tests/test_odoo_facade_contract.py`

**Interfaces:**
- Consumes: `execute_fn(model, method, *args, **kwargs)` and `unwrap_m2o(value)`.
- Produces: private skill functions with explicit executor arguments; unchanged public facade functions.

- [ ] **Step 1: Run the existing focused skill tests**

Run: `.venv/bin/pytest tests/test_odoo_client.py -q`

Expected: all tests pass before extraction.

- [ ] **Step 2: Create the private skills module and move the existing bodies**

Move these exact facade functions into `src/zira_dashboard/_odoo_skills.py`:
`fetch_skill_columns_with_types`, `fetch_skill_columns`,
`_bucket_for_level_count`, `fetch_skill_level_buckets`,
`_skill_type_id_for_skill`, `_skill_level_id_for_bucket`,
`_employee_skill_ids`, `_keep_one_employee_skill_row`,
`set_employee_skill_level`, `fetch_skills_for`, and
`fetch_spanish_speaker_ids`.

Apply this mechanical transformation to every moved I/O function:

```python
# Before, in odoo_client.py
def fetch_skill_columns_with_types() -> list[dict]:
    rows = execute("hr.skill", "search_read", [], fields=["id", "name", "skill_type_id"])
    # existing normalization body remains byte-for-byte equivalent

# After, in _odoo_skills.py
def fetch_skill_columns_with_types(execute_fn: Callable[..., Any]) -> list[dict]:
    rows = execute_fn(
        "hr.skill", "search_read", [], fields=["id", "name", "skill_type_id"]
    )
    # paste the remaining existing normalization body without semantic edits
```

Every private helper that performs I/O receives `execute_fn` as its first
argument. `fetch_skills_for` also receives `unwrap_m2o_fn`; replace only its
`unwrap_m2o(...)` lookup with `unwrap_m2o_fn(...)`. Pure
`_bucket_for_level_count` keeps its existing signature.

- [ ] **Step 3: Replace facade bodies with call-time wrappers**

```python
from . import _odoo_skills


def fetch_skill_columns_with_types() -> list[dict]:
    return _odoo_skills.fetch_skill_columns_with_types(execute)


def fetch_skill_columns() -> list[str]:
    return _odoo_skills.fetch_skill_columns(execute)


def fetch_skill_level_buckets() -> dict[int, int]:
    return _odoo_skills.fetch_skill_level_buckets(execute)


def set_employee_skill_level(employee_odoo_id: int, skill_odoo_id: int, bucket: int) -> None:
    _odoo_skills.set_employee_skill_level(
        execute, employee_odoo_id, skill_odoo_id, bucket
    )


def fetch_skills_for(employee_ids: list[int]) -> dict[int, list[dict]]:
    return _odoo_skills.fetch_skills_for(execute, employee_ids, unwrap_m2o)


def fetch_spanish_speaker_ids() -> set[int]:
    return _odoo_skills.fetch_spanish_speaker_ids(execute)
```

- [ ] **Step 4: Run the skill and facade tests**

Run: `.venv/bin/pytest tests/test_odoo_client.py tests/test_odoo_facade_contract.py -q`

Expected: all tests pass with the facade still honoring patched `execute`.

- [ ] **Step 5: Run Ruff and commit**

```bash
.venv/bin/ruff check src/zira_dashboard/_odoo_skills.py src/zira_dashboard/odoo_client.py tests/test_odoo_facade_contract.py
git add src/zira_dashboard/_odoo_skills.py src/zira_dashboard/odoo_client.py
git commit -m "refactor: extract Odoo skill operations"
```

---

### Task 3: Extract Odoo calendar operations

**Files:**
- Create: `src/zira_dashboard/_odoo_calendars.py`
- Modify: `src/zira_dashboard/odoo_client.py:331-514,1138-1201`
- Test: `tests/test_odoo_calendar_hours.py`
- Test: `tests/test_odoo_client_leaves.py`

**Interfaces:**
- Consumes: `execute_fn` and `unwrap_m2o_fn` supplied by the facade.
- Produces: pure calendar-row reducers and I/O helpers; facade-owned resource-calendar cache.

- [ ] **Step 1: Run the current calendar tests**

Run: `.venv/bin/pytest tests/test_odoo_calendar_hours.py tests/test_odoo_client_leaves.py -q`

Expected: all locally available tests pass.

- [ ] **Step 2: Move calendar reducers and queries into the private module**

Move `_float_to_hhmm`, `_calendar_hours_from_lines`,
`_calendar_lunch_windows_from_lines`, `_is_flexible`, `fetch_work_schedules`,
`fetch_calendar_hours`, `fetch_calendar_lunch_windows`, and the body of
`_fetch_resource_calendar_uncached` into `_odoo_calendars.py`.

Use the existing bodies with these exact dependency changes:

```python
def fetch_work_schedules(execute_fn: Callable[..., Any]) -> list[dict]:
    rows = execute_fn(
        "resource.calendar", "search_read", [], fields=["id", "name", "tz"]
    )
    # retain the current row normalization and sort code


def fetch_resource_calendar(
    execute_fn: Callable[..., Any],
    unwrap_m2o_fn: Callable[[Any], Any],
    employee_odoo_id: int,
) -> dict | None:
    # paste the existing _fetch_resource_calendar_uncached body
    # replace execute with execute_fn and unwrap_m2o with unwrap_m2o_fn
```

Keep `fetch_resource_calendar()` and `_resource_calendar_cache` in the facade.
Only `_fetch_resource_calendar_uncached()` delegates, so assigning
`_resource_calendar_cache = {}` retains identical behavior.

- [ ] **Step 3: Add facade wrappers and private aliases**

```python
_float_to_hhmm = _odoo_calendars.float_to_hhmm
_calendar_hours_from_lines = _odoo_calendars.calendar_hours_from_lines
_calendar_lunch_windows_from_lines = _odoo_calendars.calendar_lunch_windows_from_lines
_is_flexible = _odoo_calendars.is_flexible


def fetch_work_schedules() -> list[dict]:
    return _odoo_calendars.fetch_work_schedules(execute)


def fetch_calendar_hours(calendar_ids) -> dict:
    return _odoo_calendars.fetch_calendar_hours(execute, calendar_ids)


def fetch_calendar_lunch_windows(calendar_ids) -> dict:
    return _odoo_calendars.fetch_calendar_lunch_windows(execute, calendar_ids)


def _fetch_resource_calendar_uncached(employee_odoo_id: int) -> dict | None:
    return _odoo_calendars.fetch_resource_calendar(
        execute, unwrap_m2o, employee_odoo_id
    )
```

- [ ] **Step 4: Run focused tests, Ruff, and commit**

```bash
.venv/bin/pytest tests/test_odoo_calendar_hours.py tests/test_odoo_client_leaves.py tests/test_auto_lunch_flex_sync.py -q
.venv/bin/ruff check src/zira_dashboard/_odoo_calendars.py src/zira_dashboard/odoo_client.py
git add src/zira_dashboard/_odoo_calendars.py src/zira_dashboard/odoo_client.py
git commit -m "refactor: extract Odoo calendar operations"
```

Expected: all commands pass.

---

### Task 4: Extract Odoo attendance operations

**Files:**
- Create: `src/zira_dashboard/_odoo_attendance.py`
- Modify: `src/zira_dashboard/odoo_client.py:585-1051`
- Test: `tests/test_odoo_attendance_for_day.py`
- Test: `tests/test_odoo_open_attendance.py`
- Test: `tests/test_odoo_transfer_dept.py`
- Test: `tests/test_odoo_facade_contract.py`

**Interfaces:**
- Consumes: facade `execute`, field-name helpers, department resolver, and datetime converters.
- Produces: attendance query/reducer functions while retaining facade wrappers for monkeypatch-sensitive writes.

- [ ] **Step 1: Run attendance characterization tests**

Run: `.venv/bin/pytest tests/test_odoo_attendance_for_day.py tests/test_odoo_open_attendance.py tests/test_odoo_transfer_dept.py tests/test_odoo_facade_contract.py -q`

Expected: all tests pass.

- [ ] **Step 2: Move attendance reads and pure normalization**

Move `_to_odoo_dt`, `_odoo_dt_to_iso`, `_is_zero_duration_attendance`,
`get_current_attendance`, `fetch_attendances_missing_wc`,
`fetch_open_attendances`, `fetch_attendances_for_day`, and
`fetch_attendance_intervals_for_day` into `_odoo_attendance.py`.

```python
def get_current_attendance(
    execute_fn: Callable[..., Any],
    employee_odoo_id: int,
    wc_field: str | None,
    department_field: str | None,
) -> dict | None:
    # paste the existing body
    # delete calls to _kiosk_wc_field/_kiosk_department_field and use the
    # supplied field-name values; replace execute with execute_fn


def fetch_attendances_for_day(
    execute_fn: Callable[..., Any], day: date
) -> list[dict]:
    # paste the existing body and replace execute with execute_fn
```

The other moved read functions follow the same exact rule: keep domains, fields,
sort order, zero-duration filtering, and return shapes unchanged; replace only
global dependency lookups with their supplied arguments.

Leave `_kiosk_wc_field`, `_kiosk_department_field`, `_department_id_for_wc`,
`clock_in`, `clock_out`, `transfer`, and `undo_transfer` in the facade during this
task because tests and callers patch those names directly. Their query helpers may
delegate later only if the existing facade tests prove call-time resolution.

- [ ] **Step 3: Replace facade read bodies with wrappers**

```python
_to_odoo_dt = _odoo_attendance.to_odoo_dt
_odoo_dt_to_iso = _odoo_attendance.odoo_dt_to_iso
_is_zero_duration_attendance = _odoo_attendance.is_zero_duration_attendance


def get_current_attendance(employee_odoo_id: int) -> dict | None:
    return _odoo_attendance.get_current_attendance(
        execute,
        employee_odoo_id,
        _kiosk_wc_field(),
        _kiosk_department_field(),
    )


def fetch_open_attendances() -> list[dict]:
    return _odoo_attendance.fetch_open_attendances(
        execute, _kiosk_wc_field(), _kiosk_department_field()
    )
```

Add explicit wrappers for `fetch_attendances_missing_wc`,
`fetch_attendances_for_day`, and `fetch_attendance_intervals_for_day`, each passing
the facade's current `execute` object when called.

- [ ] **Step 4: Run focused tests, Ruff, and commit**

```bash
.venv/bin/pytest tests/test_odoo_attendance_for_day.py tests/test_odoo_open_attendance.py tests/test_odoo_transfer_dept.py tests/test_odoo_facade_contract.py -q
.venv/bin/ruff check src/zira_dashboard/_odoo_attendance.py src/zira_dashboard/odoo_client.py
git add src/zira_dashboard/_odoo_attendance.py src/zira_dashboard/odoo_client.py
git commit -m "refactor: extract Odoo attendance reads"
```

Expected: all commands pass.

---

### Task 5: Extract Odoo time-off operations

**Files:**
- Create: `src/zira_dashboard/_odoo_time_off.py`
- Modify: `src/zira_dashboard/odoo_client.py:1053-1501`
- Modify: `src/zira_dashboard/absence_sync.py:35`
- Modify: `src/zira_dashboard/routes/settings.py:1110,1162`
- Test: `tests/test_odoo_client_leaves.py`
- Test: `tests/test_odoo_client.py`
- Test: `tests/test_absence_sync.py`

**Interfaces:**
- Consumes: facade `execute`, `unwrap_m2o`, and facade-owned TTL caches.
- Produces: stateless leave query/write helpers and a named facade cache invalidator.

- [ ] **Step 1: Add a cache invalidation contract test**

```python
def test_invalidate_leave_types_cache_clears_facade_cache(monkeypatch):
    from zira_dashboard import odoo_client

    monkeypatch.setattr(odoo_client, "_leave_types_cache", ([{"id": 1}], float("inf")))
    odoo_client.invalidate_leave_types_cache()
    assert odoo_client._leave_types_cache is None
```

Run: `.venv/bin/pytest tests/test_odoo_facade_contract.py::test_invalidate_leave_types_cache_clears_facade_cache -q`

Expected: fail with `AttributeError` before implementation.

- [ ] **Step 2: Add the named facade invalidator and update direct assignments**

```python
def invalidate_leave_types_cache() -> None:
    global _leave_types_cache
    _leave_types_cache = None
```

Replace application assignments in `absence_sync.py` and
`routes/settings.py` with `odoo_client.invalidate_leave_types_cache()`. Keep the
raw variable available for existing tests and compatibility.

- [ ] **Step 3: Create the private time-off module**

Move `_norm_requires_allocation`, the cache-miss query body from
`fetch_leave_types`, `fetch_leaves_for_range`, `_aggregate_balances`, all leave
write/read helpers from `create_leave` through `post_leave_message`, the
cache-miss query body from `fetch_public_holidays`, and `find_duplicate_leave`
into `_odoo_time_off.py`.

```python
def fetch_leave_types(execute_fn: Callable[..., Any]) -> list[dict]:
    # paste the existing Odoo query and normalization after the facade cache check


def fetch_balances_for_many(
    execute_fn: Callable[..., Any],
    unwrap_m2o_fn: Callable[[Any], Any],
    types: list[dict],
    employee_odoo_ids: list[int],
) -> dict[int, list[dict]]:
    # paste the existing query/grouping body, using the supplied types and
    # replacing execute/unwrap_m2o with execute_fn/unwrap_m2o_fn


def approve_leave(execute_fn: Callable[..., Any], leave_id: int) -> str | None:
    # paste the existing three-iteration state transition loop and replace
    # execute with execute_fn; keep action ordering unchanged
```

Move `_ALLOCATION_STATE_VALIDATED`, `_LEAVE_STATES_OPEN`, and
`_LEAVE_STATE_TAKEN` with the implementation. Keep TTL lookup, expiry cleanup,
and mutation for `_leave_types_cache` and `_public_holidays_cache` in facade
wrappers; private functions perform cache-miss I/O only.

- [ ] **Step 4: Add call-time facade wrappers**

Facade wrappers call `_odoo_time_off` with the current `execute`. For
`fetch_balances_for_many`, call `types = fetch_leave_types()` in the facade and
pass those types into the private function so patched facade cache behavior is
preserved. For `approve_leave`, pass only `execute`; the private function performs
the same state loop without calling back through a stale imported facade symbol.

```python
def create_leave(employee_odoo_id, holiday_status_id, date_from, date_to,
                 hour_from=None, hour_to=None, note=None) -> int:
    return _odoo_time_off.create_leave(
        execute, employee_odoo_id, holiday_status_id, date_from, date_to,
        hour_from, hour_to, note,
    )


def invalidate_leave_types_cache() -> None:
    global _leave_types_cache
    _leave_types_cache = None
```

- [ ] **Step 5: Run focused tests, full Odoo tests, Ruff, and commit**

```bash
.venv/bin/pytest tests/test_odoo_client_leaves.py tests/test_odoo_client.py tests/test_absence_sync.py tests/test_odoo_facade_contract.py -q
.venv/bin/ruff check src/zira_dashboard/_odoo_time_off.py src/zira_dashboard/odoo_client.py src/zira_dashboard/absence_sync.py src/zira_dashboard/routes/settings.py
git add src/zira_dashboard/_odoo_time_off.py src/zira_dashboard/odoo_client.py src/zira_dashboard/absence_sync.py src/zira_dashboard/routes/settings.py tests/test_odoo_facade_contract.py
git commit -m "refactor: extract Odoo time-off operations"
```

Expected: all commands pass.

---

### Task 6: Extract Odoo feedback-task operations

**Files:**
- Create: `src/zira_dashboard/_odoo_feedback.py`
- Modify: `src/zira_dashboard/odoo_client.py:1503-1629`
- Test: `tests/test_feedback_odoo.py`
- Test: `tests/test_feedback_routes.py`

**Interfaces:**
- Consumes: facade `execute`, `xmlrpc.client.Fault`, and facade-owned `_feedback_project_id`.
- Produces: stateless task/tag/message/attachment helpers; facade keeps project-id cache.

- [ ] **Step 1: Run current feedback tests**

Run: `.venv/bin/pytest tests/test_feedback_odoo.py tests/test_feedback_routes.py -q`

Expected: all tests pass.

- [ ] **Step 2: Create the private feedback module**

Move `_ensure_feedback_stages`, the uncached find/create body from
`ensure_feedback_project`, `ensure_feedback_tag`, `create_feedback_task`,
`update_task`, `post_task_message`, `add_task_attachment`,
`fetch_task_stage_names`, and `feedback_status_bucket` into
`_odoo_feedback.py`.

```python
def find_or_create_feedback_project(execute_fn: Callable[..., Any]) -> int:
    found = execute_fn(
        "project.project",
        "search_read",
        [("name", "=", FEEDBACK_PROJECT_NAME)],
        fields=["id"],
        limit=1,
    )
    if found:
        return found[0]["id"]
    return execute_fn(
        "project.project", "create", {"name": FEEDBACK_PROJECT_NAME}
    )
```

All other moved I/O helpers preserve their existing bodies and replace only
`execute(...)` with `execute_fn(...)`. Move `FEEDBACK_PROJECT_NAME`,
`FEEDBACK_STAGES`, `FEEDBACK_DONE_STAGE`, and `FEEDBACK_REJECTED_STAGE` with the
implementation; re-export facade aliases if the reference search finds callers.

- [ ] **Step 3: Keep the project cache in the facade**

```python
def ensure_feedback_project() -> int:
    global _feedback_project_id
    if _feedback_project_id is None:
        _feedback_project_id = _odoo_feedback.find_or_create_feedback_project(execute)
        _odoo_feedback.ensure_feedback_stages(execute, _feedback_project_id)
    return _feedback_project_id


def ensure_feedback_tag(name: str) -> int:
    return _odoo_feedback.ensure_feedback_tag(execute, name)
```

Add direct call-time wrappers for `create_feedback_task`, `update_task`,
`post_task_message`, `add_task_attachment`, `fetch_task_stage_names`, and
`feedback_status_bucket`; each I/O wrapper passes the facade's current `execute`.

- [ ] **Step 4: Run focused tests, Ruff, and commit**

```bash
.venv/bin/pytest tests/test_feedback_odoo.py tests/test_feedback_routes.py tests/test_feedback_mine_route.py tests/test_calendar_conflict_monitor.py -q
.venv/bin/ruff check src/zira_dashboard/_odoo_feedback.py src/zira_dashboard/odoo_client.py
git add src/zira_dashboard/_odoo_feedback.py src/zira_dashboard/odoo_client.py
git commit -m "refactor: extract Odoo feedback operations"
```

Expected: all commands pass.

---

### Task 7: Extract recycling range aggregation

**Files:**
- Create: `src/zira_dashboard/recycling_range.py`
- Modify: `src/zira_dashboard/routes/departments.py:482-582`
- Create: `tests/test_recycling_range.py`
- Test: `tests/test_recycling_data.py`
- Test: `tests/test_dashboards_polish.py`

**Interfaces:**
- Consumes: ordered `days`, matching `per_day` dictionaries, and `is_range`.
- Produces: `RangeAggregate` with all values currently computed inline before template assembly.

- [ ] **Step 1: Write pure aggregation tests**

```python
from datetime import date

from zira_dashboard.recycling_range import aggregate_range


def _day(units, who):
    return {
        "total_units": units,
        "total_downtime": 60,
        "elapsed": 120,
        "available": 100,
        "uptime_minutes": 90,
        "total_man_hours": 2.0,
        "active_wc_names": {"Dismantler 1"},
        "per_wc_units": {"Dismantler 1": units},
        "per_wc_downtime": {"Dismantler 1": 60},
        "per_wc_expected": {"Dismantler 1": 80.0},
        "per_wc_who": {"Dismantler 1": who},
        "per_wc_category": {"Dismantler 1": "Dismantler"},
        "per_wc_station_obj": {"Dismantler 1": object()},
        "schedule_assignments": {"Dismantler 1": [who]},
    }


def test_single_day_keeps_who_and_assignments():
    result = aggregate_range([_day(100, "Ana")], [date(2026, 7, 9)], is_range=False)
    assert result.total_units == 100
    assert result.agg_who_today == {"Dismantler 1": "Ana"}
    assert result.schedule_today_assignments == {"Dismantler 1": ["Ana"]}


def test_multi_day_sums_work_center_metrics_without_single_day_labels():
    result = aggregate_range(
        [_day(100, "Ana"), _day(50, "Luis")],
        [date(2026, 7, 8), date(2026, 7, 9)],
        is_range=True,
    )
    assert result.total_units == 150
    assert result.agg_units == {"Dismantler 1": 150}
    assert result.agg_expected == {"Dismantler 1": 160.0}
    assert result.agg_who_today == {}
```

- [ ] **Step 2: Run the tests to verify the module is missing**

Run: `.venv/bin/pytest tests/test_recycling_range.py -q`

Expected: collection fails with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `RangeAggregate` and `aggregate_range`**

```python
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class RangeAggregate:
    total_units: int
    total_downtime: int
    total_elapsed: float
    total_available: float
    total_uptime_minutes: float
    total_man_hours: float
    agg_units: dict[str, int]
    agg_downtime: dict[str, int]
    agg_expected: dict[str, float]
    agg_who_today: dict[str, str | None]
    agg_category: dict[str, str]
    agg_station_obj: dict[str, Any]
    agg_active_names: set[str]
    schedule_today_assignments: dict[str, list[str]]


def aggregate_range(
    per_day: list[dict], days: list[date], *, is_range: bool
) -> RangeAggregate:
    total_units = sum(item["total_units"] for item in per_day)
    total_downtime = sum(item["total_downtime"] for item in per_day)
    total_elapsed = sum(item["elapsed"] for item in per_day)
    total_available = sum(item["available"] for item in per_day)
    total_uptime_minutes = sum(item["uptime_minutes"] for item in per_day)
    total_man_hours = sum(item["total_man_hours"] for item in per_day)

    agg_units: dict[str, int] = {}
    agg_downtime: dict[str, int] = {}
    agg_expected: dict[str, float] = {}
    agg_who_today: dict[str, str | None] = {}
    agg_category: dict[str, str] = {}
    agg_station_obj: dict[str, Any] = {}
    agg_active_names: set[str] = set()
    schedule_today_assignments: dict[str, list[str]] = {}

    for item, day in zip(per_day, days, strict=True):
        del day
        agg_active_names |= item["active_wc_names"]
        for name, units in item["per_wc_units"].items():
            agg_units[name] = agg_units.get(name, 0) + units
        for name, downtime in item["per_wc_downtime"].items():
            agg_downtime[name] = agg_downtime.get(name, 0) + downtime
        for name, expected in item["per_wc_expected"].items():
            agg_expected[name] = agg_expected.get(name, 0.0) + expected
        agg_category.update(item["per_wc_category"])
        agg_station_obj.update(item["per_wc_station_obj"])
        if not is_range:
            agg_who_today = item["per_wc_who"]
            schedule_today_assignments = item["schedule_assignments"]

    return RangeAggregate(
        total_units=total_units,
        total_downtime=total_downtime,
        total_elapsed=total_elapsed,
        total_available=total_available,
        total_uptime_minutes=total_uptime_minutes,
        total_man_hours=total_man_hours,
        agg_units=agg_units,
        agg_downtime=agg_downtime,
        agg_expected=agg_expected,
        agg_who_today=agg_who_today,
        agg_category=agg_category,
        agg_station_obj=agg_station_obj,
        agg_active_names=agg_active_names,
        schedule_today_assignments=schedule_today_assignments,
    )
```

Do not move ratio, bucket, group-goal, bar, or template code in this task.

- [ ] **Step 4: Replace the route's inline aggregation with one call**

```python
aggregate = recycling_range.aggregate_range(per_day, days, is_range=is_range)
total_units = aggregate.total_units
total_downtime = aggregate.total_downtime
total_elapsed = aggregate.total_elapsed
total_available = aggregate.total_available
total_uptime_minutes = aggregate.total_uptime_minutes
total_man_hours = aggregate.total_man_hours
agg_units = aggregate.agg_units
agg_downtime = aggregate.agg_downtime
agg_expected = aggregate.agg_expected
agg_who_today = aggregate.agg_who_today
agg_category = aggregate.agg_category
agg_station_obj = aggregate.agg_station_obj
agg_active_names = aggregate.agg_active_names
schedule_today_assignments = aggregate.schedule_today_assignments
```

- [ ] **Step 5: Run focused route tests, Ruff, and commit**

```bash
.venv/bin/pytest tests/test_recycling_range.py tests/test_recycling_data.py tests/test_dashboards_polish.py tests/test_recycling_scaling_static.py -q
.venv/bin/ruff check src/zira_dashboard/recycling_range.py src/zira_dashboard/routes/departments.py tests/test_recycling_range.py
git add src/zira_dashboard/recycling_range.py src/zira_dashboard/routes/departments.py tests/test_recycling_range.py
git commit -m "refactor: extract recycling range aggregation"
```

Expected: DB-free tests pass; DB-gated tests skip locally when `DATABASE_URL` is absent.

---

### Task 8: Extract machine-breakdown inbox commands

**Files:**
- Create: `src/zira_dashboard/breakdown_actions.py`
- Modify: `src/zira_dashboard/routes/exceptions.py:756-909`
- Modify: `tests/test_exceptions_breakdown_routes.py`

**Interfaces:**
- Consumes: command payloads, actor identity, and a supplied friendly-error callback.
- Produces: `transfer`, `snooze`, `dismiss`, and `report` functions returning the same `JSONResponse` objects.

- [ ] **Step 1: Add a route-delegation characterization test**

```python
def test_breakdown_transfer_sync_delegates_with_actor(monkeypatch):
    from zira_dashboard import breakdown_actions
    from zira_dashboard.routes import exceptions as exceptions_route

    seen = {}

    def fake(body, actor_upn=None, actor_name=None, friendly_error=None):
        seen.update(body=body, actor_upn=actor_upn, actor_name=actor_name)
        return exceptions_route.JSONResponse({"ok": True})

    monkeypatch.setattr(breakdown_actions, "transfer", fake)
    response = exceptions_route._breakdown_transfer_sync(
        {"incident_id": 1, "person_name": "Ana", "to_wc": "Repair 3"},
        "dale@example.com",
        "Dale",
    )
    assert response.status_code == 200
    assert seen == {
        "body": {"incident_id": 1, "person_name": "Ana", "to_wc": "Repair 3"},
        "actor_upn": "dale@example.com",
        "actor_name": "Dale",
    }
```

- [ ] **Step 2: Run the new test to verify the module is missing**

Run: `.venv/bin/pytest tests/test_exceptions_breakdown_routes.py::test_breakdown_transfer_sync_delegates_with_actor -q`

Expected: fail because `breakdown_actions` does not exist.

- [ ] **Step 3: Move command bodies into `breakdown_actions.py`**

Move the exact bodies of `_breakdown_transfer_sync`, `_breakdown_snooze_sync`,
`_breakdown_dismiss_sync`, and `_breakdown_report_sync` into functions named
`transfer`, `snooze`, `dismiss`, and `report`. Preserve their local imports and
validation/mutation order. Add only the injected friendly-error dependency shown
below; every other statement is copied unchanged.

```python
from __future__ import annotations

from collections.abc import Callable

from fastapi.responses import JSONResponse


def _json_error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


def transfer(
    body: dict,
    actor_upn=None,
    actor_name=None,
    friendly_error: Callable[[Exception], str] = str,
) -> JSONResponse:
    # Copy the existing transfer body and replace
    # _friendly_odoo_error(error) with friendly_error(error).
```

The `snooze`, `dismiss`, and `report` signatures remain byte-for-byte identical
to their route-private predecessors after removing the `_breakdown_` prefix and
`_sync` suffix.

- [ ] **Step 4: Replace route sync functions with wrappers**

```python
def _breakdown_transfer_sync(body: dict, actor_upn=None, actor_name=None) -> JSONResponse:
    return breakdown_actions.transfer(
        body,
        actor_upn,
        actor_name,
        friendly_error=_friendly_odoo_error,
    )


def _breakdown_snooze_sync(body: dict) -> JSONResponse:
    return breakdown_actions.snooze(body)


def _breakdown_dismiss_sync(body: dict, actor_upn=None, actor_name=None) -> JSONResponse:
    return breakdown_actions.dismiss(body, actor_upn, actor_name)


def _breakdown_report_sync(body: dict) -> JSONResponse:
    return breakdown_actions.report(body)
```

- [ ] **Step 5: Run all breakdown tests, Ruff, and commit**

```bash
.venv/bin/pytest tests/test_exceptions_breakdown_routes.py tests/test_exception_inbox_breakdown.py tests/test_inbox_undo_endpoint.py tests/test_machine_breakdown_store.py -q
.venv/bin/ruff check src/zira_dashboard/breakdown_actions.py src/zira_dashboard/routes/exceptions.py tests/test_exceptions_breakdown_routes.py
git add src/zira_dashboard/breakdown_actions.py src/zira_dashboard/routes/exceptions.py tests/test_exceptions_breakdown_routes.py
git commit -m "refactor: extract breakdown inbox commands"
```

Expected: all tests pass.

---

### Task 9: Extract pure settings context builders

**Files:**
- Create: `src/zira_dashboard/settings_context.py`
- Modify: `src/zira_dashboard/routes/settings.py:238-388`
- Create: `tests/test_settings_context.py`
- Test: `tests/test_settings_timeclock_layout.py`

**Interfaces:**
- Consumes: schedules, work-center rows, people, store callables, rounding systems, and settings objects already loaded by the route.
- Produces: pure dictionaries/lists for the existing `settings.html` context; the route retains all I/O and response behavior.

- [ ] **Step 1: Write DB-free tests for the pure builders**

```python
from datetime import time
from types import SimpleNamespace

from zira_dashboard import settings_context


def test_schedule_context_preserves_template_shape():
    schedule = SimpleNamespace(
        shift_start=time(6, 0),
        shift_end=time(14, 30),
        work_weekdays={0, 1, 2, 3, 4},
        breaks=[SimpleNamespace(start=time(9, 0), end=time(9, 15), name="Break")],
    )
    assert settings_context.schedule_context(schedule, ["Mon", "Tue"])["breaks"] == [
        {"start": "09:00", "end": "09:15", "name": "Break"}
    ]


def test_work_center_rows_keep_skill_and_default_people_rules():
    location = SimpleNamespace(meter_id="meter-1", name="Repair 1", bay="R1")
    people = [
        SimpleNamespace(name="Ana", reserve=False, level=lambda skill: 3),
        SimpleNamespace(name="Luis", reserve=True, level=lambda skill: 4),
    ]
    effective = {
        "required_skills": ["Repair"], "min_ops": 1, "max_ops": 2,
        "goal_per_day": 100, "note": "", "groups": ["Repair"],
        "department": "Recycled", "default_people": ["Ana"],
    }
    rows = settings_context.work_center_rows(
        [location], people, lambda candidate: effective
    )
    assert rows[0]["key"] == "meter-1"
    assert rows[0]["default_pool"] == [
        {"name": "Ana", "level": 3, "reserve": False},
        {"name": "Luis", "level": 4, "reserve": True},
    ]


def test_group_summary_preserves_override_display():
    rows = settings_context.group_summary(
        "group",
        all_names=lambda kind: ["Repair"],
        members=lambda kind, name: ["Repair 1", "Repair 2"],
        auto_goal=lambda kind, name: 200,
        override_goal=lambda kind, name: None,
        effective_goal=lambda kind, name: 200,
    )
    assert rows == [{
        "name": "Repair", "count": 2, "auto": 200,
        "override": "", "effective": 200,
    }]
```

- [ ] **Step 2: Run the tests to verify the module is missing**

Run: `.venv/bin/pytest tests/test_settings_context.py -q`

Expected: collection fails with `ImportError`.

- [ ] **Step 3: Implement the pure builders**

```python
def schedule_context(schedule, weekday_names: list[str]) -> dict:
    return {
        "shift_start": f"{schedule.shift_start.hour:02d}:{schedule.shift_start.minute:02d}",
        "shift_end": f"{schedule.shift_end.hour:02d}:{schedule.shift_end.minute:02d}",
        "work_weekdays": sorted(schedule.work_weekdays),
        "weekday_names": weekday_names,
        "breaks": [
            {
                "start": f"{item.start.hour:02d}:{item.start.minute:02d}",
                "end": f"{item.end.hour:02d}:{item.end.minute:02d}",
                "name": item.name,
            }
            for item in schedule.breaks
        ],
    }


def work_center_rows(locations, active_people, effective_for) -> list[dict]:
    rows = []
    for location in locations:
        effective = effective_for(location)
        required_skills = effective["required_skills"]
        pool = []
        for person in active_people:
            level = min((person.level(skill) for skill in required_skills), default=0) if required_skills else 2
            pool.append({"name": person.name, "level": level, "reserve": person.reserve})
        pool.sort(key=lambda row: (row["reserve"], -row["level"], row["name"].lower()))
        rows.append({
            "key": location.meter_id or f"name:{location.name}",
            "name": location.name,
            "bay": location.bay,
            "required_skills": required_skills,
            "min_ops": effective["min_ops"],
            "max_ops": effective["max_ops"] if effective["max_ops"] is not None else "",
            "goal": effective["goal_per_day"],
            "note": effective["note"],
            "groups": effective["groups"],
            "department": effective["department"],
            "default_people": effective["default_people"],
            "default_pool": pool,
        })
    return rows


def group_summary(kind, *, all_names, members, auto_goal,
                  override_goal, effective_goal) -> list[dict]:
    rows = []
    for name in all_names(kind):
        override = override_goal(kind, name)
        rows.append({
            "name": name,
            "count": len(members(kind, name)),
            "auto": auto_goal(kind, name),
            "override": "" if override is None else override,
            "effective": effective_goal(kind, name),
        })
    return rows
```

Also move the existing list/dict comprehensions for work-schedule overrides,
rounding systems, department rounding, Saturday schedule, and auto-lunch into
named pure functions. Their arguments are already-loaded objects and their return
expressions remain unchanged.

- [ ] **Step 4: Replace the route's pure construction blocks with calls**

```python
wc_rows = settings_context.work_center_rows(
    staffing.LOCATIONS, active_people_objs, work_centers_store.effective
)
group_rows = settings_context.group_summary(
    "group",
    all_names=work_centers_store.all_group_names,
    members=work_centers_store.members,
    auto_goal=work_centers_store.group_goal_auto,
    override_goal=work_centers_store.group_goal_override,
    effective_goal=work_centers_store.group_goal,
)
schedule_ctx = settings_context.schedule_context(
    schedule_store.current(), schedule_store.WEEKDAY_NAMES
)
```

Use the corresponding named builder for each remaining pure block. Keep Odoo
sync, SQL reads, exception handling, request/session access, and
`TemplateResponse` in `routes/settings.py`.

- [ ] **Step 5: Run settings tests, Ruff, and commit**

```bash
.venv/bin/pytest tests/test_settings_context.py tests/test_settings_api_keys.py tests/test_settings_timeclock_layout.py tests/test_settings_forklift.py tests/test_settings_auto_lunch.py -q
.venv/bin/ruff check src/zira_dashboard/settings_context.py src/zira_dashboard/routes/settings.py tests/test_settings_context.py
git add src/zira_dashboard/settings_context.py src/zira_dashboard/routes/settings.py tests/test_settings_context.py
git commit -m "refactor: extract settings context builders"
```

Expected: DB-free tests pass and database-gated cases skip locally.

---

### Task 10: Apply targeted hygiene and verify the whole refactor

**Files:**
- Verify: `src/zira_dashboard/recycling_range.py`
- Modify: `src/zira_dashboard/routes/staffing.py:587`
- Modify only proven-safe transformed loops reported by Ruff.
- Modify: `pyproject.toml` only if an expanded rule category is completely clean.
- Read/verify: `src/zira_dashboard/templates/**`
- Read/verify: `src/zira_dashboard/static/**`

**Interfaces:**
- Consumes: all extracted modules and facade contracts from Tasks 1-9.
- Produces: a green full suite, clean Ruff output, explicit zip intent, and evidence that UI assets/contracts did not drift.

- [ ] **Step 1: Make zip truncation intent explicit**

```python
# recycling_range.py (created in Task 7)
for item, day in zip(per_day, days, strict=True):

# routes/staffing.py
- for bs, be, bn in zip(starts, ends, names):
+ for bs, be, bn in zip(starts, ends, names, strict=False):
```

Use `strict=True` for recycling because both lists derive from the same range;
use `strict=False` for form lists because current behavior intentionally truncates
malformed unequal inputs rather than raising.

- [ ] **Step 2: Apply only behavior-identical PERF/SIM fixes**

Run: `.venv/bin/ruff check src --select PERF401,PERF403,SIM118,B905 --diff`

For each proposed change, keep it only when the loop body has no logging,
mutation outside the result container, early exit, or exception-boundary effect.
Do not enable these rules in `pyproject.toml` unless the selected categories are
clean across `src` after edits.

- [ ] **Step 3: Run focused expanded lint**

Run: `.venv/bin/ruff check src --select B023,B905,PERF401,PERF403,SIM118`

Expected: no new findings in files changed by this branch. Pre-existing findings
outside changed files may remain documented and do not justify broad churn.

- [ ] **Step 4: Run the complete configured checks**

```bash
.venv/bin/ruff check src tests scripts
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src
```

Expected: Ruff exits 0; pytest reports at least the 1,413-pass baseline with only
environment-gated skips; compileall exits 0.

- [ ] **Step 5: Verify public route inventory and UI assets did not change**

```bash
git diff 220ba70...HEAD -- src/zira_dashboard/templates src/zira_dashboard/static
git diff 220ba70...HEAD -- src/zira_dashboard/_schema.py pyproject.toml
```

Expected: no template/static diff and no schema/dependency diff. If
`pyproject.toml` changed only for an adopted Ruff category, review that hunk
separately and confirm runtime dependencies are untouched.

- [ ] **Step 6: Review the complete branch diff**

Run: `git diff --check 220ba70...HEAD`

Expected: no whitespace errors. Inspect `git diff --stat 220ba70...HEAD` and
confirm every file belongs to Tasks 1-10; `.claude/` remains untracked and absent
from commits.

- [ ] **Step 7: Commit final hygiene only if files changed**

```bash
git add src/zira_dashboard/routes/staffing.py
git commit -m "refactor: clarify safe iteration contracts"
```

- [ ] **Step 8: Record the database verification boundary**

The existing GitHub Actions workflow runs the Postgres-backed suite. Do not claim
those 294 locally skipped cases passed. Before merge, require the branch's Actions
test job to pass against Postgres 16.
