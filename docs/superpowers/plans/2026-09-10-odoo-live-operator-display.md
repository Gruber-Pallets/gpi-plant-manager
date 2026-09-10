# Odoo Live Operator Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every live current-operator label show planned people in gray and people physically signed into that exact Odoo work center at full strength, without changing the Plant Scheduler or the existing production bars.

**Architecture:** Extract the already-atomic canonical attendance-location read from the Staffing route into a domain module, then build one pure read-only current-operator join over the saved plan and canonical physical presences. Dashboard routes attach those display rows only after production scores and bar geometry are complete. The Scheduler loses its Odoo work-center preview and attendance-driven write path, while its existing attendance/absence status reads remain intact.

**Tech Stack:** Python 3.13, FastAPI, Jinja2, PostgreSQL-backed attendance mirror, pytest.

## Global Constraints

- Odoo work-center attendance is the authority for physical presence.
- The saved schedule remains the authority for the staffing plan.
- Planned-only names are gray; valid current Odoo names at that exact mapped work center are full-strength.
- Match by Odoo employee identity whenever available; do not use display-name equality as the primary identity.
- Read physical presence only from the canonical local Odoo attendance mirror. Never use request-time XML-RPC or the legacy open-attendance cache for dashboard presence.
- Freeze response time, mirror health, rows, and freshness once per rendered response.
- Stale, unavailable, unmapped, conflicting, closed, or future attendance never creates a full-strength name.
- Full-day absences filter the planned layer but do not suppress valid physical presence.
- The Plant Scheduler stays plan-only. Attendance may not add, move, or remove saved assignments.
- Do not infer provenance from `assignment_sources == "generated"` and do not bulk-delete persisted seats.
- Do not change `assignment_windows`, production attribution, `coalesce_display_scores`, `worker_coverage_is_split`, production scoring, goals, units, finish lines, segment geometry, or multi-day aggregation.
- Historical worker segments remain visible after attendance closes; only the separate current-operator label changes.
- Do not add a legend, an “unplanned” badge, or a routine Plant Manager work-center transfer.
- Preserve untracked `.cursorignore`, `.python-version`, and `uv.lock`.

## File Structure

- Create `src/zira_dashboard/attendance_location_snapshot.py`: own the atomic mirror policy/row/timeline snapshot that is currently embedded in `routes/staffing.py`.
- Create `src/zira_dashboard/current_operators.py`: own canonical physical-presence extraction and the pure planned-versus-present display join.
- Create `tests/test_attendance_location_snapshot.py`: pin one-read freshness, half-open interval, identity, stale, and unavailable behavior.
- Create `tests/test_current_operators.py`: pin the shared display model and Christian/Dismantler 3 example.
- Modify `src/zira_dashboard/routes/staffing.py`: consume the extracted snapshot for existing attendance status, but stop building work-center preview context.
- Modify `src/zira_dashboard/machine_breakdown.py`: consume shared canonical presence types/extraction while preserving breakdown decisions.
- Modify `src/zira_dashboard/routes/departments.py`: carry the frozen snapshot into a separate current-label model and attach it after bars are built.
- Modify `src/zira_dashboard/recycling_range.py`: carry single-day display rows through aggregation without using them in any calculations.
- Modify `src/zira_dashboard/templates/_department_dashboard_widgets.html`: render structured current names in both horizontal and vertical labels.
- Modify `src/zira_dashboard/static/recycling.css`: style planned-only current names gray without changing bar styles.
- Modify `src/zira_dashboard/goat_watch.py` and `src/zira_dashboard/templates/_goat_watch_banner.html`: make the live contender’s current-operator names use the same display rows; historical GOAT alerts remain unchanged.
- Modify `src/zira_dashboard/wc_dashboard_data.py`: return plan-only assignments and expose the full filtered plan for the shared join.
- Modify `src/zira_dashboard/routes/wc_dashboard.py`: freeze one canonical snapshot and provide structured current names to screen and TV views.
- Modify `src/zira_dashboard/templates/wc_dashboard.html` and `src/zira_dashboard/templates/_tv_header.html`: render the same gray/full-strength current names.
- Modify `src/zira_dashboard/static/wc_dashboard.css` and `src/zira_dashboard/static/tv-mode.css`: retain readable current-name styling in work-center and TV chrome.
- Modify `src/zira_dashboard/attendance_sync.py`: remove attendance-to-schedule writes.
- Modify `src/zira_dashboard/staffing_view.py`, `src/zira_dashboard/templates/staffing.html`, and `src/zira_dashboard/static/staffing.css`: remove Scheduler work-center preview data and markup.
- Modify `src/zira_dashboard/static/staffing-print.css`: remove preview-only print hide rules after their markup is gone.
- Delete `src/zira_dashboard/staffing_live_assign.py`: remove the prohibited schedule-mutation path and retired legacy live readers.
- Modify Scheduler, dashboard, breakdown, and segment tests listed in the tasks below.
- Modify `CHANGELOG.md`: describe the shipped behavior in plain language only after all implementation checks pass.

---

### Task 1: Extract the canonical attendance-location snapshot

**Files:**
- Create: `src/zira_dashboard/attendance_location_snapshot.py`
- Create: `tests/test_attendance_location_snapshot.py`
- Modify: `src/zira_dashboard/routes/staffing.py:75-279`
- Modify: `src/zira_dashboard/routes/departments.py:52-131`
- Modify: `src/zira_dashboard/machine_breakdown.py:634-712`
- Modify: `tests/test_staffing_live_locations.py:146-480`
- Modify: `tests/test_department_operator_labels.py:315-386`
- Modify: `tests/test_machine_breakdown_rows.py:1520-1580`

**Interfaces:**
- Produces: `LocationSnapshot(policy, attendance_source, spans, verified_cap_utc, current_attendance_ids)`.
- Produces: `read_location_snapshot(day: date, *, as_of_utc: datetime) -> LocationSnapshot`.
- Produces: `project_location_spans(day: date, *, as_of_utc: datetime, policy: AttendanceReadPolicy, rows: Sequence[Mapping] | None = None) -> tuple[LocationSpan, ...]`.
- Produces: `current_attendance_ids_at(rows: Sequence[Mapping], verified_cap_utc: datetime) -> frozenset[int]`.
- Preserves: the existing Staffing attendance package, late-name logic, strict historical segment path, and breakdown rollback path.

- [ ] **Step 1: Move the atomic-snapshot characterization tests to the domain boundary**

Create tests that call the new module rather than a private Staffing route:

```python
def test_read_location_snapshot_uses_one_atomic_generation(monkeypatch):
    calls = []
    monkeypatch.setattr(
        attendance_mirror,
        "snapshot_overlapping",
        lambda *_args: calls.append(True) or atomic_snapshot,
    )

    snapshot = attendance_location_snapshot.read_location_snapshot(
        DAY, as_of_utc=NOW
    )

    assert calls == [True]
    assert snapshot.verified_cap_utc == VERIFIED_AT
    assert snapshot.current_attendance_ids == frozenset({91})
    assert snapshot.spans[0].employee_odoo_id == 101
```

Also port the existing cases for a row closing exactly at the cap, an open row capped to mirror freshness, canonical roster name by employee ID, stale health, unavailable reads, and one failed projection making the whole snapshot unavailable.

- [ ] **Step 2: Run the extracted tests and verify the missing module fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_attendance_location_snapshot.py -q`

Expected: FAIL during collection because `zira_dashboard.attendance_location_snapshot` does not exist.

- [ ] **Step 3: Create the domain snapshot module by moving the existing behavior unchanged**

Use the same logic currently in `_StaffingMirrorSnapshot`, `_project_staffing_location_spans`, `_current_attendance_ids_at`, and `_read_staffing_response_snapshot`:

```python
@dataclass(frozen=True)
class LocationSnapshot:
    policy: live_cache.AttendanceReadPolicy
    attendance_source: live_cache.AttendanceSourceSnapshot | None
    spans: tuple[attendance_timeline.LocationSpan, ...]
    verified_cap_utc: datetime
    current_attendance_ids: frozenset[int]


def current_attendance_ids_at(
    rows: Sequence[Mapping[str, object]], verified_cap_utc: datetime
) -> frozenset[int]:
    if verified_cap_utc.utcoffset() is None:
        raise ValueError("verified_cap_utc must be timezone-aware")
    return frozenset(
        int(row["odoo_attendance_id"])
        for row in rows
        if row["check_in_utc"] <= verified_cap_utc
        and (
            row.get("check_out_utc") is None
            or verified_cap_utc < row["check_out_utc"]
        )
    )
```

Keep the current fail-closed policy construction, `min(as_of_utc, policy.refreshed_at)` cap, atomic `snapshot_overlapping` call, `day_presence_from_rows`, timeline projection, work-center mapping, department validation, and canonical roster-name replacement exactly as they work now.

- [ ] **Step 4: Replace private route imports without changing callers’ decisions**

In Staffing:

```python
staffing_mirror_snapshot = attendance_location_snapshot.read_location_snapshot(
    d, as_of_utc=staffing_live_as_of
)
```

In departments and machine breakdown, import `attendance_location_snapshot` and call the same public function. Do not alter strict-day fallback, stale handling, cap math, segment construction, or breakdown legacy rollback in this task.

- [ ] **Step 5: Run snapshot, department, breakdown, and Staffing attendance tests**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_attendance_location_snapshot.py tests/test_department_operator_labels.py tests/test_machine_breakdown_rows.py tests/test_staffing_attendance_source.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the extraction**

```bash
git add src/zira_dashboard/attendance_location_snapshot.py src/zira_dashboard/routes/staffing.py src/zira_dashboard/routes/departments.py src/zira_dashboard/machine_breakdown.py tests/test_attendance_location_snapshot.py tests/test_staffing_live_locations.py tests/test_department_operator_labels.py tests/test_machine_breakdown_rows.py
git commit -m "refactor: share canonical attendance location snapshots"
```

### Task 2: Build the read-only current-operator model

**Files:**
- Create: `src/zira_dashboard/current_operators.py`
- Create: `tests/test_current_operators.py`
- Modify: `src/zira_dashboard/machine_breakdown.py:31-60,634-708`
- Modify: `tests/test_machine_breakdown_rows.py:1520-1580`

**Interfaces:**
- Consumes: `attendance_location_snapshot.LocationSnapshot`.
- Produces: `OperatorPresence(person_name, wc_name, arrival_utc, employee_odoo_id)`.
- Produces: `OperatorDeparture(person_name, wc_name, arrival_utc, departure_utc, employee_odoo_id)`.
- Produces: `OperatorSourceSnapshot(presences, departures, available, mirror_owned, complete)`.
- Produces: `OperatorDisplayRow(person_name, employee_odoo_id, planned, physically_present)`.
- Produces: `source_from_location_snapshot(snapshot: LocationSnapshot) -> OperatorSourceSnapshot`.
- Produces: `build_display_by_work_center(planned_by_wc, *, planned_employee_ids, absent_names, source, is_today) -> dict[str, tuple[OperatorDisplayRow, ...]]`.

- [ ] **Step 1: Write pure join tests for every approved identity and freshness case**

Use fixed IDs so display-name equality cannot accidentally pass:

```python
def test_person_planned_here_but_present_elsewhere_is_gray_and_full_strength():
    source = OperatorSourceSnapshot(
        presences=(
            OperatorPresence("Christian C.", "Dismantler 3", NOW, 8),
        ),
        departures=(),
        available=True,
        mirror_owned=True,
        complete=True,
    )

    display = build_display_by_work_center(
        {"Dismantler 1": ["Christian C."]},
        planned_employee_ids={"Christian C.": 8},
        absent_names=set(),
        source=source,
        is_today=True,
    )

    assert display["Dismantler 1"] == (
        OperatorDisplayRow("Christian C.", 8, True, False),
    )
    assert display["Dismantler 3"] == (
        OperatorDisplayRow("Christian C.", 8, False, True),
    )
```

Add focused cases for planned-only, planned-and-present deduplication, Christian unplanned on Dismantler 3, two present people at one center, same display name with two Odoo IDs, full-day absence, closed interval, unmapped, conflicting, stale, unavailable, mirror-off, non-today returning no live display rows, and deterministic planned-first ordering.

- [ ] **Step 2: Run the model tests and verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_current_operators.py -q`

Expected: FAIL during collection because `zira_dashboard.current_operators` does not exist.

- [ ] **Step 3: Implement canonical presence extraction**

Move the canonical conversion currently in `machine_breakdown._operator_source_from_staffing_snapshot` into the new module:

```python
def source_from_location_snapshot(
    snapshot: attendance_location_snapshot.LocationSnapshot,
) -> OperatorSourceSnapshot:
    policy = snapshot.policy
    if not policy.mirror_owned:
        return OperatorSourceSnapshot((), (), False, False, False)
    if not policy.available or policy.stale:
        return OperatorSourceSnapshot((), (), False, True, False)

    presences = []
    departures = []
    complete = True
    for span in snapshot.spans:
        is_current = (
            span.start_utc <= snapshot.verified_cap_utc < span.end_utc
            or (
                span.start_utc <= snapshot.verified_cap_utc == span.end_utc
                and bool(
                    snapshot.current_attendance_ids.intersection(
                        span.attendance_ids
                    )
                )
            )
        )
        if span.status == "exempt_no_location":
            continue
        if span.status != "valid" or not span.app_work_center_name:
            complete = complete and not is_current
            continue
        if is_current:
            presences.append(
                OperatorPresence(
                    span.employee_name,
                    span.app_work_center_name,
                    span.start_utc,
                    span.employee_odoo_id,
                )
            )
        elif span.start_utc < span.end_utc <= snapshot.verified_cap_utc:
            departures.append(
                OperatorDeparture(
                    span.employee_name,
                    span.app_work_center_name,
                    span.start_utc,
                    span.end_utc,
                    span.employee_odoo_id,
                )
            )
    return OperatorSourceSnapshot(
        tuple(sorted(presences, key=_identity_order)),
        tuple(sorted(departures, key=_identity_order)),
        True,
        True,
        complete,
    )
```

- [ ] **Step 4: Implement the planned/present join without mutation**

```python
def build_display_by_work_center(
    planned_by_wc,
    *,
    planned_employee_ids,
    absent_names,
    source,
    is_today,
):
    if not is_today:
        return {}
    filtered_plan = {
        wc_name: tuple(name for name in names if name not in absent_names)
        for wc_name, names in planned_by_wc.items()
        if wc_name != staffing.TIME_OFF_KEY
    }
    present = source.presences if is_today and source.available and source.mirror_owned else ()
    present_ids_by_wc = {
        wc_name: {item.employee_odoo_id for item in present if item.wc_name == wc_name}
        for wc_name in {item.wc_name for item in present}
    }
    result = {}
    for wc_name in dict.fromkeys([*filtered_plan, *(item.wc_name for item in present)]):
        rows = []
        seen_ids = set()
        for name in filtered_plan.get(wc_name, ()):
            employee_id = _employee_id(planned_employee_ids.get(name))
            physically_present = (
                employee_id is not None
                and employee_id in present_ids_by_wc.get(wc_name, set())
            )
            rows.append(OperatorDisplayRow(name, employee_id, True, physically_present))
            if employee_id is not None:
                seen_ids.add(employee_id)
        for item in present:
            if item.wc_name == wc_name and item.employee_odoo_id not in seen_ids:
                rows.append(
                    OperatorDisplayRow(
                        item.person_name,
                        item.employee_odoo_id,
                        False,
                        True,
                    )
                )
                if item.employee_odoo_id is not None:
                    seen_ids.add(item.employee_odoo_id)
        result[wc_name] = tuple(rows)
    return result
```

The helper must perform no database calls and must not import any schedule-save, attendance-write, transfer, scoring, or production-history API.

- [ ] **Step 5: Make breakdown use the shared canonical source**

Import the source dataclasses into `machine_breakdown.py` so its existing public module attributes and tests remain compatible. Replace only `_operator_source_from_staffing_snapshot` with a call to `current_operators.source_from_location_snapshot`. Preserve the legacy fallback used by breakdown rollback and preserve every detection, snooze, departure, exclusion, and transfer decision.

- [ ] **Step 6: Run model and breakdown tests**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_current_operators.py tests/test_machine_breakdown_math.py tests/test_machine_breakdown_rows.py tests/test_exceptions_breakdown_routes.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the shared model**

```bash
git add src/zira_dashboard/current_operators.py src/zira_dashboard/machine_breakdown.py tests/test_current_operators.py tests/test_machine_breakdown_rows.py
git commit -m "feat: model planned and present operators separately"
```

### Task 3: Use the shared model on Recycling and New dashboards

**Files:**
- Modify: `src/zira_dashboard/routes/departments.py:52-131,330-650,780-880,1045-1125`
- Modify: `src/zira_dashboard/recycling_range.py:6-89`
- Modify: `src/zira_dashboard/goat_watch.py:64-72,135-143,165-210`
- Modify: `src/zira_dashboard/templates/_department_dashboard_widgets.html:1-149,269-290`
- Modify: `src/zira_dashboard/templates/_goat_watch_banner.html:41-55`
- Modify: `src/zira_dashboard/static/recycling.css:171-177,384-389,475-478`
- Modify: `src/zira_dashboard/static/goat_watch.css`
- Create: `tests/test_current_operator_surfaces.py`
- Modify: `tests/test_goat_watch.py`

**Interfaces:**
- Consumes: `current_operators.build_display_by_work_center`.
- Adds to each single-day data payload: `current_operator_rows: dict[str, tuple[OperatorDisplayRow, ...]]`.
- Adds to `RangeAggregate`: `single_day_current_operator_rows`.
- Adds to rendered bar and downtime row dictionaries only after `build_bars` / `build_downtime_rows` return: `current_operators`.
- Adds to live `Contender`: `current_operators: tuple[OperatorDisplayRow, ...]`; keeps persisted GOAT alerts unchanged.
- Leaves all inputs and outputs of `production_segments` and all geometry fields from `build_bars` unchanged.

- [ ] **Step 1: Write a route-data regression for Christian on Dismantler 3**

In `tests/test_current_operator_surfaces.py`, build a department fixture with schedule assignments on other dismantlers and canonical attendance employee ID 8 at Dismantler 3:

```python
assert live["current_operator_rows"]["Dismantler 3"] == (
    current_operators.OperatorDisplayRow(
        "Christian C.", 8, planned=False, physically_present=True
    ),
)
assert live["schedule_assignments"]["Dismantler 3"] == []
```

In the same test, close Christian’s span at 11:00 and assert the current row disappears while his 7:00–11:00 segment, units, goal, result, and time label are unchanged.

- [ ] **Step 2: Write model-attachment tests that snapshot bar geometry**

Build a transfer bar once, copy these fields, attach current names, and assert exact equality afterward:

```python
geometry_before = [
    {
        key: segment[key]
        for key in (
            "start_pct",
            "actual_pct",
            "shortfall_start_pct",
            "shortfall_pct",
            "finish_pct",
            "runway_pct",
        )
    }
    for segment in bar["segments"]
]

departments._attach_current_operator_rows(
    [bar], {"Repair 4": operator_rows}
)

assert geometry_after == geometry_before
assert bar["units"] == 548
assert bar["expected"] == 725
assert bar["current_operators"] == operator_rows
```

Also assert a range row receives no current-presence treatment.

- [ ] **Step 3: Write horizontal, vertical, screen, and TV template tests**

Use one planned-only and one physically-present row:

```python
operator_rows = (
    OperatorDisplayRow("Planned Person", 11, True, False),
    OperatorDisplayRow("Christian C.", 8, False, True),
)
```

Assert every orientation renders `current-operator planned-only` for the planned person, `current-operator physically-present` for Christian, and the work-center name as secondary text. Assert no “from Odoo” or “unplanned” badge appears. Assert a live station with no rows and worker history still renders `No one here now`.

- [ ] **Step 4: Write a live GOAT Watch current-operator test**

Build a contender whose leading work center has one planned-only person and Christian physically present:

```python
contenders = goat_watch.contenders_for_now(
    DAY,
    NOW,
    current_operator_rows_by_wc={
        "Dismantler 3": (
            OperatorDisplayRow("Planned Person", 11, True, False),
            OperatorDisplayRow("Christian C.", 8, False, True),
        )
    },
)

assert contenders[0].current_operators[0].person_name == "Planned Person"
assert contenders[0].current_operators[1].person_name == "Christian C."
```

Render `_goat_watch_banner.html` and assert the same gray/full-strength classes. Persisted `NEW GOAT` alert rows must continue to render their historical winner normally, because they are records rather than current-presence claims.

- [ ] **Step 5: Carry the frozen location snapshot through department data**

Extend `_CanonicalDepartmentProjection` with `location_snapshot: LocationSnapshot | None`. When the mirror owns the read, return the exact snapshot already used to build historical canonical segments. When permanent strict history or legacy rollback is used, set it to `None`.

Inside `_department_day_data`, build current display only when `is_live`:

```python
operator_source = (
    current_operators.source_from_location_snapshot(
        canonical_projection.location_snapshot
    )
    if canonical_projection and canonical_projection.location_snapshot
    else current_operators.OperatorSourceSnapshot((), (), False, False, False)
)
current_operator_rows = current_operators.build_display_by_work_center(
    sched.assignments,
    planned_employee_ids=attendance.name_to_person_id(),
    absent_names=absent_names,
    source=operator_source,
    is_today=is_live,
)
```

This reuses the same atomic snapshot and cap as the segment read. Do not make a second attendance read.

- [ ] **Step 6: Attach labels only after production bars are complete**

Carry `current_operator_rows` through `RangeAggregate` only for a single-day response. After `build_bars` and `build_downtime_rows` have returned, attach:

```python
def _attach_current_operator_rows(rows, by_work_center):
    for row in rows:
        row["current_operators"] = tuple(by_work_center.get(row["name"], ()))
    return rows
```

Do not add the new model as a parameter to `build_bars`, `build_downtime_rows`, scoring, coalescing, split selection, or range arithmetic.

- [ ] **Step 7: Render structured current labels**

Extend `operator_name` with an optional state class and use `b.current_operators` / `d.current_operators` before the old current-label branches:

```jinja2
{% for operator in b.current_operators %}
  {{ operator_name(
      operator.person_name,
      operator_links_by_wc.get(b.name) if operator_links_by_wc else none,
      true,
      "physically-present" if operator.physically_present else "planned-only"
  ) }}
{% endfor %}
```

Use the same rendering in horizontal bars, vertical bars, and downtime labels. Keep segment hit areas, tooltips, worker history, goal markers, and assignment buttons unchanged.

- [ ] **Step 8: Render the live GOAT contender from the same rows**

Change `_goat_watch_contenders` to accept the already-frozen `current_operator_rows_by_wc` and pass those rows to `goat_watch.contenders_for_now`. Do not let `goat_watch` read the schedule or attendance again.

Replace live contender `c.person` markup with the structured rows:

```jinja2
{% for operator in c.current_operators %}
  <span class="goat-watch-person current-operator {{ 'physically-present' if operator.physically_present else 'planned-only' }}">
    {{ operator.person_name }}
  </span>{% if not loop.last %}<span aria-hidden="true"> + </span>{% endif %}
{% endfor %}
```

If there are no current rows, render “No one here now.” Do not change contender threshold, work-center selection, unit projection, record comparison, record holder, persisted alerts, or dismissal.

- [ ] **Step 9: Add display-only CSS**

```css
.current-operator.physically-present {
  color: var(--fg);
}

.current-operator.planned-only {
  color: var(--muted);
  font-weight: 500;
}
```

Do not edit any bar fill, segment, target-line, finish-line, scale, width, height, or color rule.

- [ ] **Step 10: Run surface tests plus untouched department and midday characterization suites**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_current_operator_surfaces.py tests/test_goat_watch.py tests/test_department_operator_labels.py tests/test_recycling_data.py tests/test_recycling_range.py tests/test_new_dashboard_template.py tests/test_production_segments.py tests/test_assignment_windows.py tests/test_recycling_goal_math.py tests/test_dashboards_polish.py tests/test_tv_dashboards_vs.py -q`

Expected: PASS, including lunch joining, lunch transfer splitting, overlapping workers, independent goals, “No one here now,” horizontal/vertical/TV rendering, and range suppression.

- [ ] **Step 11: Commit department surfaces**

```bash
git add src/zira_dashboard/routes/departments.py src/zira_dashboard/recycling_range.py src/zira_dashboard/goat_watch.py src/zira_dashboard/templates/_department_dashboard_widgets.html src/zira_dashboard/templates/_goat_watch_banner.html src/zira_dashboard/static/recycling.css src/zira_dashboard/static/goat_watch.css tests/test_current_operator_surfaces.py tests/test_goat_watch.py
git commit -m "feat: show verified operators on live dashboards"
```

### Task 4: Use the shared model on work-center screens and TV views

**Files:**
- Modify: `src/zira_dashboard/wc_dashboard_data.py:77-110`
- Modify: `src/zira_dashboard/routes/wc_dashboard.py:56-166`
- Modify: `src/zira_dashboard/templates/wc_dashboard.html:45-82`
- Modify: `src/zira_dashboard/templates/_tv_header.html:21-61`
- Modify: `src/zira_dashboard/static/wc_dashboard.css:377-448`
- Modify: `src/zira_dashboard/static/tv-mode.css`
- Modify: `tests/test_wc_dashboard_data.py:88-120`
- Modify: `tests/test_wc_dashboard.py:21-208`
- Modify: `tests/test_tv_displays_routes.py`
- Modify: `tests/test_operator_dashboard_day_links.py`

**Interfaces:**
- Produces: `wc_dashboard_data.planned_operators_by_work_center(day: date) -> dict[str, list[str]]`, already filtered for full-day absences.
- Keeps: `assigned_operators_for_wc(wc_name, day)` as a plan-only compatibility wrapper.
- Consumes: one `LocationSnapshot`, one `OperatorSourceSnapshot`, and `build_display_by_work_center`.
- Adds template context: `current_operator_rows` and `is_live_operator_display`, never a joined schedule assignment list.

- [ ] **Step 1: Write plan-only data tests**

Replace the test that expects `assigned_operators_for_wc` to append live Odoo people:

```python
def test_assigned_operators_for_wc_remains_plan_only(monkeypatch):
    monkeypatch.setattr(
        staffing,
        "load_schedule",
        lambda _day: staffing.Schedule(
            day=DAY,
            published=True,
            assignments={"Repair 1": ["Jose O."]},
        ),
    )
    monkeypatch.setattr(attendance, "full_day_absent_names", lambda _day: set())

    assert wc_dashboard_data.assigned_operators_for_wc("Repair 1", DAY) == [
        "Jose O."
    ]
```

- [ ] **Step 2: Write screen and TV integration tests**

Freeze a healthy canonical snapshot with Jose planned on Repair 1 and Christian physically present there. Assert both routes render Jose gray and Christian full-strength. Repeat with Jose’s current attendance at Repair 1 and assert Jose is deduplicated and full-strength. Repeat with stale source and assert no full-strength name remains.

For a past or future `day` query, assert plan names render with the old normal plan-only treatment, without gray/full-strength current-presence classes.

- [ ] **Step 3: Expose the complete filtered plan**

```python
def planned_operators_by_work_center(day: date) -> dict[str, list[str]]:
    try:
        schedule = staffing.load_schedule(day)
    except Exception:
        return {}
    try:
        absent = set(attendance.full_day_absent_names(day))
    except Exception:
        absent = set()
    return {
        wc_name: [name for name in names if name not in absent]
        for wc_name, names in schedule.assignments.items()
        if wc_name != staffing.TIME_OFF_KEY
    }
```

Remove the legacy-live append from `assigned_operators_for_wc`.

- [ ] **Step 4: Freeze and join once in the work-center route**

At the start of an uncached render, freeze `as_of_utc = datetime.now(UTC)`, read one location snapshot for today, build the shared source, and join against the complete plan. Set `is_live_operator_display = day == today`. For non-today views, skip the location snapshot, keep `current_operator_rows` empty, and render the existing plan-only `operators_display` without presence classes.

Use the selected work center’s rows for display and use `bool(current_operator_rows)` for the live “staffed versus calm empty station” presentation. Do not alter pallets, progress, KPI, downtime, GOAT, ribbons, layout, or cache durations.

- [ ] **Step 5: Render identical classes in screen and TV chrome**

For `is_live_operator_display`, render each operator separately rather than joining names with ` · `:

```jinja2
{% for operator in current_operator_rows %}
  <span class="current-operator {{ 'physically-present' if operator.physically_present else 'planned-only' }}">
    {{ operator.person_name }}
  </span>{% if not loop.last %}<span aria-hidden="true"> · </span>{% endif %}
{% else %}
  <span class="current-operator-empty">No one here now</span>
{% endfor %}
```

For non-live dates, retain the current `operators_display or "(unassigned)"` branch. Add a `right_operators` argument to `_tv_header.html` that renders the same classes only for today. Existing `right`, `right_items`, GOAT chips, and other TV headers must retain their markup.

- [ ] **Step 6: Run work-center and TV tests**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_wc_dashboard_data.py tests/test_wc_dashboard.py tests/test_tv_displays_routes.py tests/test_operator_dashboard_day_links.py -q`

Expected: PASS. Database-backed route tests may skip only when `DATABASE_URL` is not configured.

- [ ] **Step 7: Commit work-center surfaces**

```bash
git add src/zira_dashboard/wc_dashboard_data.py src/zira_dashboard/routes/wc_dashboard.py src/zira_dashboard/templates/wc_dashboard.html src/zira_dashboard/templates/_tv_header.html src/zira_dashboard/static/wc_dashboard.css src/zira_dashboard/static/tv-mode.css tests/test_wc_dashboard_data.py tests/test_wc_dashboard.py tests/test_tv_displays_routes.py tests/test_operator_dashboard_day_links.py
git commit -m "feat: show physical presence on work-center screens"
```

### Task 5: Restore the Plant Scheduler and remove attendance-driven seating

**Files:**
- Modify: `src/zira_dashboard/attendance_sync.py:287-299`
- Modify: `src/zira_dashboard/routes/staffing.py:282-349,2193-2211,2511-2550`
- Modify: `src/zira_dashboard/staffing_view.py:1-180`
- Modify: `src/zira_dashboard/templates/staffing.html:50-96,342-408`
- Modify: `src/zira_dashboard/static/staffing.css:678-723`
- Modify: `src/zira_dashboard/static/staffing-print.css:31-33`
- Modify: `src/zira_dashboard/wc_attributions.py:557-564,614-619`
- Delete: `src/zira_dashboard/staffing_live_assign.py`
- Delete: `tests/test_staffing_live_assign.py`
- Delete: `tests/test_staffing_live_locations.py`
- Modify: `tests/test_attendance_sync.py`
- Modify: `tests/test_wc_attributions.py`
- Modify: `tests/test_staffing_static.py`
- Modify: `tests/test_staffing_rotations.py`
- Modify: `tests/test_staffing_schedule_metadata.py`

**Interfaces:**
- Removes: `apply_after_attendance_sync`, `apply_live_odoo_assignments_for_day`, live-seat planning, retired legacy live-seat readers, `_staffing_live_context`, and Scheduler preview context.
- Preserves: `attendance_location_snapshot.read_location_snapshot` because Staffing still uses canonical attendance for arrived/late/absence status.
- Preserves: `_live_location_active` guards on manual attribution endpoints, floor-app `staffing_transfer`, and `staffing.js` schedule-revision polling; none of these is the removed preview or sync-to-schedule writer.
- Preserves: Scheduler schedule-save APIs, Auto, defaults, manual locks, posting, reset, history, time-off, late markers, and assignment attribution.

- [ ] **Step 1: Write failing Scheduler-boundary regressions**

Assert the template contains none of the prohibited presentation:

```python
def test_scheduler_has_no_odoo_work_center_preview():
    template = Path("src/zira_dashboard/templates/staffing.html").read_text()
    for forbidden in (
        "Odoo preview",
        "staffing-live-banner",
        "planned-live-locations",
        "live-unscheduled",
        "live-location-badge",
        "live-inbound",
        "Working elsewhere",
    ):
        assert forbidden not in template
```

Add an incremental-sync test that patches `staffing.save_schedule` to fail if called, returns a successful sync affecting today, and asserts sync succeeds without any schedule write.

Add an unattributed-work test proving a work center with unassigned production remains in the Scheduler’s old assignment list even when current Odoo presence exists; current physical presence belongs on dashboards, not this planning workflow.

- [ ] **Step 2: Run the boundary tests and verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_static.py tests/test_attendance_sync.py tests/test_wc_attributions.py -q`

Expected: FAIL because preview markup, occupancy suppression, and the post-sync schedule write still exist.

- [ ] **Step 3: Remove the attendance-sync mutation**

Delete the `staffing_live_assign.apply_after_attendance_sync(result.affected_days)` block from `run_incremental_sync`. Delete `staffing_live_assign.py` and its write-path tests. Do not replace it with another schedule mutation or a cache-triggered mutation.

- [ ] **Step 4: Remove all Scheduler work-center preview context**

Delete `_staffing_live_context` and its route invocation/context merge. Keep the one atomic snapshot read and policy passed to `_safe_attendance`, because those power existing arrived/late/absence behavior rather than current-work-center preview.

Remove `StaffingPersonLocation`, `build_live_locations`, and `inbound_live_by_work_center` from `staffing_view.py` once no production caller remains.

- [ ] **Step 5: Restore the Scheduler template and CSS**

Remove the top preview banner, not-scheduled Odoo list, inbound names in work-center summaries, per-planned-person location badges, and all dedicated preview CSS. Remove the now-dead preview selectors from `staffing-print.css`. Leave scheduled names, attendance late flags, certifications, GOAT badges, training labels, partial-hours controls, attributions, picker behavior, and all remaining print behavior unchanged.

- [ ] **Step 6: Restore old attribution-task behavior**

Remove `wc_attributions.live_occupied_work_centers` and the live-occupancy skip in `unattributed_for_day`. Existing schedule and durable manual-attribution checks remain.

- [ ] **Step 7: Do not perform unsafe persisted-seat cleanup**

Add no migration and no delete query. A `generated` source is not proof that attendance created the seat. If a specific persisted seat later has auditable provenance, handle that exact row in a separate reviewed repair.

- [ ] **Step 8: Run Scheduler behavior regressions**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_static.py tests/test_staffing_rotations.py tests/test_staffing_schedule_metadata.py tests/test_staffing_trim_saw_defaults.py tests/test_staffing_delivery.py tests/test_staffing_attendance_source.py tests/test_attendance_sync.py tests/test_attendance_mirror_cutover.py tests/test_wc_attributions.py -q`

Expected: PASS, including Auto, reset-to-defaults, manual lock preservation, publish/draft history, posted snapshot isolation, and no attendance-driven schedule save.

- [ ] **Step 9: Commit the Scheduler restoration**

```bash
git add src/zira_dashboard/attendance_sync.py src/zira_dashboard/routes/staffing.py src/zira_dashboard/staffing_view.py src/zira_dashboard/templates/staffing.html src/zira_dashboard/static/staffing.css src/zira_dashboard/static/staffing-print.css src/zira_dashboard/wc_attributions.py tests/test_attendance_sync.py tests/test_wc_attributions.py tests/test_staffing_static.py tests/test_staffing_rotations.py tests/test_staffing_schedule_metadata.py
git rm src/zira_dashboard/staffing_live_assign.py tests/test_staffing_live_assign.py tests/test_staffing_live_locations.py
git commit -m "fix: keep Odoo presence out of the Scheduler"
```

### Task 6: Lock the midday boundary, document, verify, and deploy

**Files:**
- Modify: `tests/test_current_operator_surfaces.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- No new runtime interfaces.
- Validates the approved Christian example and unchanged Scheduler/bar behavior as one release.

- [ ] **Step 1: Add one end-to-end presentation-only regression**

In `tests/test_current_operator_surfaces.py`, create a test that renders the same midday transfer data before and after substituting different planned/current label rows. Compare every segment’s person, start/end label, actual units, goal units, result, runway, start percentage, fill percentage, shortfall percentage, and finish percentage. Assert only the current-name HTML class/text differs. Keep the existing segment, geometry, goal, and range tests unchanged as characterization tests.

- [ ] **Step 2: Run the complete focused safety suite**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest \
  tests/test_attendance_location_snapshot.py \
  tests/test_current_operators.py \
  tests/test_current_operator_surfaces.py \
  tests/test_goat_watch.py \
  tests/test_department_operator_labels.py \
  tests/test_recycling_data.py \
  tests/test_recycling_range.py \
  tests/test_new_dashboard_template.py \
  tests/test_production_segments.py \
  tests/test_assignment_windows.py \
  tests/test_recycling_goal_math.py \
  tests/test_dashboards_polish.py \
  tests/test_tv_dashboards_vs.py \
  tests/test_wc_dashboard_data.py \
  tests/test_wc_dashboard.py \
  tests/test_tv_displays_routes.py \
  tests/test_machine_breakdown_math.py \
  tests/test_machine_breakdown_rows.py \
  tests/test_exceptions_breakdown_routes.py \
  tests/test_staffing_static.py \
  tests/test_staffing_rotations.py \
  tests/test_staffing_schedule_metadata.py \
  tests/test_staffing_trim_saw_defaults.py \
  tests/test_staffing_delivery.py \
  tests/test_staffing_attendance_source.py \
  tests/test_attendance_sync.py \
  tests/test_attendance_mirror_cutover.py \
  tests/test_wc_attributions.py -q
```

Expected: PASS, with only the repository’s documented environment-based skips.

- [ ] **Step 3: Run the full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`

Expected: PASS, with only documented skips.

- [ ] **Step 4: Perform authenticated live smoke checks**

On today’s live Recycling/New dashboard and one individual work-center screen/TV:

1. Confirm a planned-but-not-signed-in name is gray.
2. Confirm a planned-and-signed-in name is full-strength.
3. Confirm an unplanned signed-in person appears full-strength at the mapped Odoo center.
4. Confirm a transfer removes full-strength at the old center and adds it at the new center after mirror refresh.
5. Confirm closing attendance restores the gray plan or “No one here now.”
6. Confirm a known morning/afternoon handoff retains the same bars, goals, units, finish lines, and worker-history details.
7. Confirm the Plant Scheduler has no Odoo work-center preview and its saved assignments do not change.

- [ ] **Step 5: Write child-readable release notes**

Add a new top entry to `CHANGELOG.md`:

```markdown
### Live boards now show who is really at each work area

#### Fixes

- **Planned names stay gray until the person signs into that same work area.** A dark name means Odoo shows the person is really there now.
- **People who move during the day now show at the work area they actually signed into.** The midday bars and old work history still stay the same.
- **The Plant Scheduler is only a plan again.** Odoo attendance no longer moves names on the planning sheet.
```

- [ ] **Step 6: Request code review and fix only verified findings**

Use the requesting-code-review workflow against the approved design and this plan. Re-run the affected focused tests after each accepted fix, then re-run the complete focused safety suite.

- [ ] **Step 7: Commit release regressions and notes**

```bash
git add tests/test_current_operator_surfaces.py CHANGELOG.md
git commit -m "test: protect live operator display boundaries"
```

- [ ] **Step 8: Push implementation to `origin/main`**

Run: `git push origin main`

Expected: the push succeeds without force and Railway starts the normal deployment.

- [ ] **Step 9: Verify deployment before claiming completion**

Confirm the deployed web service is online, `/healthz` returns 200, and repeat the live smoke checks after deployment. If authenticated UI access is unavailable, report the feature as implemented and test-verified but leave the live UI check explicitly unverified.

## Plan Self-Review

- Spec coverage: every approved requirement maps to Tasks 1–6; Scheduler restoration is isolated in Task 5; midday protection is enforced in Tasks 3 and 6.
- Placeholder scan: no deferred implementation steps or unspecified error handling remain.
- Type consistency: `LocationSnapshot` feeds `source_from_location_snapshot`; `OperatorSourceSnapshot` feeds `build_display_by_work_center`; every surface receives `tuple[OperatorDisplayRow, ...]`.
- Boundary check: current labels are attached only after bar and downtime math returns; no production or scheduling write interface consumes the new display model.
- Safety check: no broad persisted-seat cleanup is planned because existing assignment metadata cannot safely prove attendance provenance.
