# Live Schedule Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Staffing warning panel show only warnings that apply to the schedule currently displayed, updating after every edit without saving the in-progress view.

**Architecture:** Add a pure current-view validator that evaluates a proposed assignment map against server-supplied roster, work-center, time-off, training, and default data. A read-only rotation endpoint builds that authoritative context and returns structured issues. The Staffing page uses the same service on its initial render; the browser debounces snapshot validation after grid changes and replaces—not appends to—the warning panel with the latest response.

**Tech Stack:** Python 3.12, FastAPI, pytest, vanilla JavaScript, existing Staffing template and `schedule_solver.PlacementIssue` payloads.

## Global Constraints

- The server is the single authority for skills, availability, time off, defaults, capacity, and safe crew rules.
- Live validation must not write schedules, change assignment sources, invalidate caches, or reconcile/start/finish training blocks.
- The initial warning panel must validate the displayed schedule; it must not report issues from a hypothetical Auto rebuild.
- The client sends only the visible assignment snapshot and enabled Auto center names; it must never decide a warning is valid or invalid locally.
- A newer snapshot response always wins over an older response, even if the older request finishes last.
- A validation transport failure removes prior staffing warnings and displays a distinct validation-unavailable message.
- Keep existing accessible warning-panel markup and structured `PlacementIssue.to_dict()` rendering.
- Add a short child-friendly `CHANGELOG.md` entry in the implementation commit pushed to `origin/main`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/zira_dashboard/current_schedule_validation.py` | Pure, I/O-free validation of the assignments currently being viewed. |
| `src/zira_dashboard/routes/staffing.py` | Assemble the current day’s authoritative validation inputs and use the result for initial page warnings. |
| `src/zira_dashboard/routes/rotations.py` | Expose `POST /api/rotations/validate-current` and strictly parse its read-only snapshot. |
| `src/zira_dashboard/static/staffing.js` | Debounce current-grid snapshots, ignore obsolete responses, and replace the warning list. |
| `tests/test_current_schedule_validation.py` | Unit tests for the pure warning rules. |
| `tests/test_staffing_rotations.py` | Route/page-orchestration tests proving the endpoint is read-only and initial render validates displayed assignments. |
| `tests/test_staffing_static.py` | Browser-harness tests for latest-response-wins and stale-warning removal. |
| `CHANGELOG.md` | Short user-facing patch note for the feature push. |

## Validation Contract

`current_schedule_validation.validate_current_assignments` receives only
already-loaded facts and returns a tuple of `schedule_solver.PlacementIssue`.
It has no database or HTTP imports.

```python
def validate_current_assignments(
    *,
    roster: Sequence[staffing.Person],
    assignments: Mapping[str, Sequence[str]],
    enabled_centers: Collection[str],
    locations: Sequence[staffing.Location],
    minimums: Mapping[str, int],
    capacities: Mapping[str, int | None],
    required_skills: Mapping[str, Sequence[str]],
    full_day_off_names: Collection[str],
    trim_saw_centers: Collection[str],
    training_trainees_by_center: Mapping[str, Collection[str]],
    exact_defaults: Mapping[str, Sequence[str]],
    group_defaults: Mapping[str, Sequence[str]],
    user_group_centers: Mapping[str, Sequence[str]],
) -> tuple[schedule_solver.PlacementIssue, ...]:
```

The function emits deterministic, name- and center-sorted issues for visible
assignments only. It reports duplicate people, over-capacity centers, missing
or insufficient skills, inactive/reserve/full-day-off assignees, unsafe Trim
Saw pairs, missing level-3 training partners, unassigned active people,
below-minimum or invalidly configured centers, and unhonored exact/group
defaults. Messages describe the current view and never say “Previous schedule
kept.”

`staffing_route.current_view_validation_for_day` is the I/O boundary. It
accepts `day`, `assignments`, and `enabled_work_centers`, loads the relevant
authoritative data without mutating it, converts active training blocks to
per-center trainee requirements without reconciliation, and calls the pure
validator. It returns only serializable issue dictionaries.

## Task 1: Add the pure current-view validator

**Files:**

- Create: `src/zira_dashboard/current_schedule_validation.py`
- Create: `tests/test_current_schedule_validation.py`

**Interfaces:**

- Consumes: `staffing.Person`, `staffing.Location`, and `schedule_solver.PlacementIssue`.
- Produces: `validate_current_assignments` returning a tuple of `PlacementIssue` values for routes and tests.

- [ ] **Step 1: Write failing unit tests for safe crews and current center coverage**

```python
from zira_dashboard import current_schedule_validation, staffing


def _person(name: str, **skills: int) -> staffing.Person:
    return staffing.Person(name=name, skills=skills)


def test_trim_saw_warning_tracks_the_current_pair():
    locations = (staffing.Location("Trim Saw 1", "Trim Saw", "Bay 4", "Recycled", None, 2, 2),)
    common = dict(
        roster=[_person("Level Two", **{"Trim Saw": 2}), _person("Level One", **{"Trim Saw": 1}), _person("Green", **{"Trim Saw": 3})],
        enabled_centers={"Trim Saw 1"}, locations=locations,
        minimums={"Trim Saw 1": 2}, capacities={"Trim Saw 1": 2},
        required_skills={"Trim Saw 1": ("Trim Saw",)}, full_day_off_names=set(),
        trim_saw_centers={"Trim Saw 1"}, training_trainees_by_center={},
        exact_defaults={}, group_defaults={}, user_group_centers={},
    )

    unsafe = current_schedule_validation.validate_current_assignments(
        assignments={"Trim Saw 1": ["Level Two", "Level One"]}, **common,
    )
    safe = current_schedule_validation.validate_current_assignments(
        assignments={"Trim Saw 1": ["Green", "Level One"]}, **common,
    )

    assert "no_safe_complete_crew" in {issue.code for issue in unsafe}
    assert "no_safe_complete_crew" not in {issue.code for issue in safe}


def test_current_validation_reports_duplicate_capacity_and_unqualified_people():
    locations = (
        staffing.Location("Repair 1", "Repair", "Bay 1", "Recycled", None, 1, 1),
        staffing.Location("Repair 2", "Repair", "Bay 1", "Recycled", None, 1, 1),
    )
    issues = current_schedule_validation.validate_current_assignments(
        roster=[_person("Alex", Repair=0), _person("Bea", Repair=1)],
        assignments={"Repair 1": ["Alex", "Bea"], "Repair 2": ["Bea"]},
        enabled_centers={"Repair 1"}, locations=locations,
        minimums={"Repair 1": 1}, capacities={"Repair 1": 1},
        required_skills={"Repair 1": ("Repair",)}, full_day_off_names=set(),
        trim_saw_centers=set(), training_trainees_by_center={},
        exact_defaults={}, group_defaults={}, user_group_centers={},
    )

    assert {issue.code for issue in issues} == {
        "person_assigned_multiple_centers",
        "center_capacity_exceeded",
        "assignment_unqualified",
    }
```

- [ ] **Step 2: Add failing unit tests for availability, training, defaults, and resolved minimum coverage**

```python
def test_current_validation_removes_minimum_warning_when_the_visible_crew_is_safe():
    locations = (staffing.Location("Repair 1", "Repair", "Bay 1", "Recycled", None, 2, 2),)
    common = dict(
        roster=[_person("Green", Repair=3), _person("Trainee", Repair=0)],
        enabled_centers={"Repair 1"}, locations=locations,
        minimums={"Repair 1": 2}, capacities={"Repair 1": 2},
        required_skills={"Repair 1": ("Repair",)}, full_day_off_names=set(),
        trim_saw_centers=set(), training_trainees_by_center={"Repair 1": {"Trainee"}},
        exact_defaults={"Repair 1": ["Green"]}, group_defaults={}, user_group_centers={},
    )

    short = current_schedule_validation.validate_current_assignments(
        assignments={"Repair 1": ["Trainee"]}, **common,
    )
    safe = current_schedule_validation.validate_current_assignments(
        assignments={"Repair 1": ["Green", "Trainee"]}, **common,
    )

    assert {issue.code for issue in short} >= {"training_partner_missing", "center_minimum_unmet"}
    assert safe == ()


def test_current_validation_reports_unavailable_and_unhonored_defaults():
    locations = (staffing.Location("Repair 1", "Repair", "Bay 1", "Recycled", None, 1, 1),)
    issues = current_schedule_validation.validate_current_assignments(
        roster=[_person("Off Today", Repair=3), _person("Defaulted", Repair=3)],
        assignments={"Repair 1": ["Off Today"]}, enabled_centers={"Repair 1"},
        locations=locations, minimums={"Repair 1": 1}, capacities={"Repair 1": 1},
        required_skills={"Repair 1": ("Repair",)}, full_day_off_names={"Off Today"},
        trim_saw_centers=set(), training_trainees_by_center={},
        exact_defaults={"Repair 1": ["Defaulted"]}, group_defaults={}, user_group_centers={},
    )

    assert {issue.code for issue in issues} >= {"assignment_unavailable", "center_minimum_unmet", "exact_default_violation"}
```

- [ ] **Step 3: Run the new tests to verify they fail because the module is absent**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_current_schedule_validation.py -q`

Expected: FAIL during collection with `ImportError: cannot import name 'current_schedule_validation'`.

- [ ] **Step 4: Implement the pure validator**

```python
# src/zira_dashboard/current_schedule_validation.py
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence

from . import schedule_solver, staffing


def _issue(code, message, *, person=None, centers=()):
    return schedule_solver.PlacementIssue(
        code=code, message=message, person=person, centers=tuple(centers),
    )


def _trim_saw_pair_is_safe(levels: Sequence[int]) -> bool:
    if len(levels) != 2:
        return False
    low, high = sorted(levels)
    return high >= 3 if low <= 1 else low >= 2


def validate_current_assignments(
    *,
    roster: Sequence[staffing.Person],
    assignments: Mapping[str, Sequence[str]],
    enabled_centers: Collection[str],
    locations: Sequence[staffing.Location],
    minimums: Mapping[str, int],
    capacities: Mapping[str, int | None],
    required_skills: Mapping[str, Sequence[str]],
    full_day_off_names: Collection[str],
    trim_saw_centers: Collection[str],
    training_trainees_by_center: Mapping[str, Collection[str]],
    exact_defaults: Mapping[str, Sequence[str]],
    group_defaults: Mapping[str, Sequence[str]],
    user_group_centers: Mapping[str, Sequence[str]],
) -> tuple[schedule_solver.PlacementIssue, ...]:
    locations_by_name = {location.name: location for location in locations}
    enabled = tuple(sorted(
        set(enabled_centers) & set(locations_by_name), key=str.lower,
    ))
    by_name = {person.name: person for person in roster}
    off = set(full_day_off_names)
    visible = {
        center: tuple(str(name) for name in names or ())
        for center, names in assignments.items()
        if center in locations_by_name
    }
    assigned_centers = defaultdict(set)
    for center, names in visible.items():
        for name in names:
            assigned_centers[name].add(center)

    issues = []
    for name, centers in sorted(assigned_centers.items(), key=lambda item: item[0].lower()):
        if len(centers) > 1:
            ordered = tuple(sorted(centers, key=str.lower))
            issues.append(_issue(
                "person_assigned_multiple_centers",
                f"{name} is assigned to multiple work centers ({', '.join(ordered)}).",
                person=name, centers=ordered,
            ))

    for center in enabled:
        names = visible.get(center, ())
        required = tuple(required_skills.get(center, (locations_by_name[center].skill,)))
        capacity = capacities.get(center)
        minimum = max(0, int(minimums.get(center, 0)))
        if capacity is not None and minimum > capacity:
            issues.append(_issue(
                "invalid_center_configuration",
                f"{center} has a minimum of {minimum} but a maximum of {capacity}.",
                centers=(center,),
            ))
        if capacity is not None and len(names) > capacity:
            issues.append(_issue(
                "center_capacity_exceeded",
                f"{center} exceeds its maximum capacity of {capacity}.", centers=(center,),
            ))

        trainees = set(training_trainees_by_center.get(center, ()))
        green_present = any(
            (person := by_name.get(name)) is not None
            and person.active and not person.reserve and name not in off
            and all(person.level(skill) >= 3 for skill in required)
            for name in names
        )
        safe_names = set()
        for name in names:
            person = by_name.get(name)
            if person is None or not person.active or person.reserve or name in off:
                issues.append(_issue(
                    "assignment_unavailable",
                    f"{name} is unavailable for {center}.", person=name, centers=(center,),
                ))
                continue
            if name in trainees and green_present:
                safe_names.add(name)
                continue
            if not all(person.level(skill) >= 1 for skill in required):
                issues.append(_issue(
                    "assignment_unqualified",
                    f"{name} is not qualified for {center}.", person=name, centers=(center,),
                ))
                continue
            safe_names.add(name)

        missing_trainees = trainees - set(names)
        if missing_trainees or (trainees & set(names) and not green_present):
            details = sorted(missing_trainees or trainees & set(names), key=str.lower)
            issues.append(_issue(
                "training_partner_missing",
                f"{center} needs its training crew with a level 3 partner ({', '.join(details)}).",
                centers=(center,),
            ))
        if center in trim_saw_centers:
            levels = [by_name[name].level(required[0]) if name in by_name else 0 for name in names]
            if not _trim_saw_pair_is_safe(levels):
                issues.append(_issue(
                    "no_safe_complete_crew",
                    f"{center} cannot form a safe complete crew.", centers=(center,),
                ))
        if len(safe_names) < minimum:
            issues.append(_issue(
                "center_minimum_unmet",
                f"{center} is below its safe minimum: {len(safe_names)} qualified and present, minimum {minimum}.",
                centers=(center,),
            ))

    assigned_names = set(assigned_centers)
    for person in sorted(roster, key=lambda item: item.name.lower()):
        if person.active and not person.reserve and person.name not in off and person.name not in assigned_names:
            issues.append(_issue(
                "person_unplaced",
                f"{person.name} is not assigned to a work center.", person=person.name,
            ))

    defaults_by_person = defaultdict(list)
    for center, names in exact_defaults.items():
        for name in names:
            defaults_by_person[str(name)].append(("exact", center))
    for group, names in group_defaults.items():
        for name in names:
            defaults_by_person[str(name)].append(("group", group))
    for name, targets in sorted(defaults_by_person.items(), key=lambda item: item[0].lower()):
        if name not in by_name or not by_name[name].active or by_name[name].reserve or name in off:
            continue
        unique = tuple(sorted(set(targets), key=lambda item: (item[0], item[1].lower())))
        if len(unique) > 1:
            issues.append(_issue(
                "default_target_conflict",
                f"{name} has conflicting default targets.", person=name,
                centers=tuple(target for _kind, target in unique),
            ))
            continue
        kind, target = unique[0]
        actual = assigned_centers.get(name, set())
        if kind == "exact":
            if target not in enabled or target not in locations_by_name:
                issues.append(_issue(
                    "exact_default_center_disabled",
                    f"{name}'s default work center {target} is not enabled.",
                    person=name, centers=(target,),
                ))
                continue
            target_required = tuple(required_skills.get(target, (locations_by_name[target].skill,)))
            if not all(by_name[name].level(skill) >= 1 for skill in target_required):
                issues.append(_issue(
                    "exact_default_unqualified",
                    f"{name} is not qualified for default work center {target}.",
                    person=name, centers=(target,),
                ))
                continue
            if actual != {target}:
                issues.append(_issue(
                    "exact_default_violation",
                    f"{name} is not at default center {target}.", person=name, centers=(target,),
                ))
        if kind == "group":
            group_centers = set(user_group_centers.get(target, ())) & set(enabled)
            if not group_centers:
                issues.append(_issue(
                    "group_default_no_enabled_member",
                    f"{name}'s default group {target} has no enabled work center.", person=name,
                ))
                continue
            qualified_centers = {
                center for center in group_centers
                if all(by_name[name].level(skill) >= 1 for skill in required_skills.get(center, (locations_by_name[center].skill,)))
            }
            if not qualified_centers:
                issues.append(_issue(
                    "group_default_no_qualified_member",
                    f"{name} is not qualified for any enabled work center in default group {target}.",
                    person=name, centers=tuple(sorted(group_centers, key=str.lower)),
                ))
                continue
            if not actual & qualified_centers:
                issues.append(_issue(
                    "group_default_violation",
                    f"{name} is outside default group {target}.", person=name,
                    centers=tuple(sorted(qualified_centers, key=str.lower)),
                ))

    return tuple(sorted(
        issues,
        key=lambda issue: (issue.code, tuple(name.lower() for name in issue.centers), (issue.person or "").lower()),
    ))
```

Use the exact function signature in the Validation Contract. Ignore unknown
centers and unknown names after the route has rejected malformed snapshots;
this keeps the pure helper safe for direct callers. Count a level-0 training
trainee toward a center minimum only when a non-trainee level-3 partner is in
that same visible center. Emit one `no_safe_complete_crew` issue for each Trim
Saw center whose current selected pair is not safe, even if the center also
has a normal minimum issue.

- [ ] **Step 5: Run the focused validator tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_current_schedule_validation.py -q`

Expected: PASS with all current-view validator tests green.

- [ ] **Step 6: Commit and push the pure validator**

```bash
git add src/zira_dashboard/current_schedule_validation.py tests/test_current_schedule_validation.py
git commit -m "feat: validate current staffing assignments"
git push origin main
```

## Task 2: Build a read-only authoritative validation API and use it on page load

**Files:**

- Modify: `src/zira_dashboard/routes/staffing.py:157-235, 802-905, 1567-1590`
- Modify: `src/zira_dashboard/routes/rotations.py:1-75, 383-535`
- Modify: `tests/test_staffing_rotations.py`

**Interfaces:**

- Consumes: `validate_current_assignments` from Task 1.
- Produces: `current_view_validation_for_day(day, assignments, enabled_work_centers) -> list[dict[str, object]]` and `POST /api/rotations/validate-current` returning an `ok` flag plus an issue list.

- [ ] **Step 1: Write failing route and page-orchestration tests**

```python
def test_live_validation_endpoint_uses_the_submitted_current_view_without_saving(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    monkeypatch.setattr(rotations.staffing_route, "current_view_validation_for_day", lambda **kwargs: [
        {"code": "no_safe_complete_crew", "message": "Trim Saw 1 cannot form a safe complete crew.", "person": None, "centers": ["Trim Saw 1"], "rejections": []},
    ])
    monkeypatch.setattr(rotations.staffing, "save_schedule", lambda *_args, **_kwargs: pytest.fail("validation must not save"))

    response = client.post("/api/rotations/validate-current", json={
        "day": TARGET_DAY.isoformat(), "enabled_work_centers": ["Trim Saw 1"],
        "assignments": {"Trim Saw 1": ["Level Two", "Level One"]},
    })

    assert response.status_code == 200
    assert response.json() == {"ok": True, "issues": [
        {"code": "no_safe_complete_crew", "message": "Trim Saw 1 cannot form a safe complete crew.", "person": None, "centers": ["Trim Saw 1"], "rejections": []},
    ]}


def test_live_validation_endpoint_rejects_unknown_center_and_duplicate_names(monkeypatch):
    client, _rotations = _rotations_client(monkeypatch)
    response = client.post("/api/rotations/validate-current", json={
        "day": TARGET_DAY.isoformat(), "enabled_work_centers": ["Missing Center"],
        "assignments": {"Repair 1": ["Alex", "Alex"]},
    })

    assert response.status_code == 422
    assert response.json()["ok"] is False
```

Add a direct `staffing_route.current_view_validation_for_day` test that
monkeypatches the roster, time off, defaults, and training-read helpers. Assert
that a visible safe `Green + Level One` Trim Saw crew has no
`no_safe_complete_crew` issue even when the old Auto recommendation would have
failed.

- [ ] **Step 2: Run the route tests to verify they fail because the endpoint and helper are absent**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py -q -k 'live_validation or current_view_validation'`

Expected: FAIL because `current_view_validation_for_day` and
`/api/rotations/validate-current` do not exist.

- [ ] **Step 3: Implement `current_view_validation_for_day` in the Staffing route**

```python
def current_view_validation_for_day(*, day, assignments, enabled_work_centers):
    enabled = _ordered_work_center_names(enabled_work_centers)
    roster = staffing.load_roster()
    time_off_entries = scheduler_time_off.time_off_entries_for_day(day)
    exact_defaults, group_defaults, user_group_centers = _default_inputs(strict=True)
    group_locations, group_required_skills = _auto_group_maps(set(enabled))
    required_skills = {
        center: group_required_skills[group]
        for group, centers in group_locations.items() for center in centers
    }
    trainee_requirements = _current_training_trainees_by_center(day, assignments)
    issues = current_schedule_validation.validate_current_assignments(
        roster=roster, assignments=assignments, enabled_centers=enabled,
        locations=staffing.LOCATIONS,
        minimums={loc.name: _effective_minimum(loc) for loc in staffing.LOCATIONS if loc.name in enabled},
        capacities=_configured_center_capacities(enabled), required_skills=required_skills,
        full_day_off_names=rotation_suggestions._full_day_time_off_names(time_off_entries),
        trim_saw_centers=set(group_locations.get(rotation_suggestions.TRIM_SAW_SKILL, ())),
        training_trainees_by_center=trainee_requirements,
        exact_defaults=exact_defaults, group_defaults=group_defaults,
        user_group_centers=user_group_centers,
    )
    return [issue.to_dict() for issue in issues]
```

Implement `_current_training_trainees_by_center` as a read-only projection of
`rotation_store.active_blocks_for_day(day)` and `rotation_training.effect_for_day`.
Pass the current assignments as manual names to preserve the existing manual
conflict behavior. Do not call `rotation_training.reconcile_blocks` from this
path.

Replace `_recycled_context_for_day`’s current `suggestion.placement_issues`
and `suggestion.warnings` warning construction with the helper’s validation
payload. Keep rotation reasons and the active training-block display unchanged.

- [ ] **Step 4: Implement strict snapshot parsing and the read-only endpoint**

```python
@router.post("/api/rotations/validate-current")
async def validate_current_rotation_view(request: Request):
    body = await _json_body(request)
    if body is None:
        return _error("Invalid JSON body.", 400)
    day, assignments, enabled = _parse_current_validation_snapshot(body)

    def _work():
        return JSONResponse({
            "ok": True,
            "issues": staffing_route.current_view_validation_for_day(
                day=day, assignments=assignments, enabled_work_centers=enabled,
            ),
        })

    return await asyncio.to_thread(_work)
```

`_parse_current_validation_snapshot` must accept only an ISO date, a list of
known work-center names, and a mapping from known work-center names to lists of
unique non-empty names. It returns a 422 response for unknown centers,
duplicate names within one center, non-list assignees, or more assignees than
that center’s configured display maximum. It must not call
`save_schedule`, `update_auto_enabled_work_centers`, cache invalidation, or
training reconciliation.

- [ ] **Step 5: Run focused route and page tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py -q -k 'live_validation or current_view_validation or current_minimum_coverage'`

Expected: PASS, including the no-write endpoint assertion and current-screen
Trim Saw regression.

- [ ] **Step 6: Commit and push the read-only validation boundary**

```bash
git add src/zira_dashboard/routes/staffing.py src/zira_dashboard/routes/rotations.py tests/test_staffing_rotations.py
git commit -m "feat: expose live staffing validation"
git push origin main
```

## Task 3: Revalidate the visible grid in the browser

**Files:**

- Modify: `src/zira_dashboard/static/staffing.js:1510-1929`
- Modify: `tests/test_staffing_static.py`

**Interfaces:**

- Consumes: `POST /api/rotations/validate-current` from Task 2, response shape `{"ok": true, "issues": Issue[]}`.
- Produces: `scheduleCurrentViewValidation()` and `validateCurrentView()` functions that replace `window.ROTATION_ISSUES` with the newest server response.

- [ ] **Step 1: Write a failing browser-harness test for response ordering and stale-warning removal**

```python
def test_live_validation_uses_only_the_newest_response_and_replaces_warnings():
    js = _script()
    validation = js.split("function scheduleCurrentViewValidation() {", 1)[1].split(
        "function setWorkCenterOnState", 1,
    )[0]
    assert "AbortController" in validation
    assert "if (requestId !== validationRequestId) return;" in validation
    assert "renderCoverageIssues([], data.issues || []);" in validation


def test_live_validation_failure_does_not_keep_old_staffing_issues():
    js = _script()
    validation = js.split("async function validateCurrentView() {", 1)[1].split(
        "function scheduleCurrentViewValidation", 1,
    )[0]
    assert "renderCoverageIssues([], [validationUnavailableIssue()]);" in validation
```

Add a Node harness that resolves the second mocked `fetch` response before the
first. Assert the warning panel contains only the second response’s issue.

- [ ] **Step 2: Run the browser tests to verify they fail because live validation is missing**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_static.py -q -k 'live_validation'`

Expected: FAIL because `scheduleCurrentViewValidation` and
`validateCurrentView` are not present.

- [ ] **Step 3: Add snapshot collection and debounced latest-response validation**

```javascript
let validationTimer = null;
let validationRequestId = 0;
let validationController = null;

function currentViewSnapshot() {
  const assignments = {};
  document.querySelectorAll('details.sched-dd[data-loc]').forEach(dropdown => {
    assignments[dropdown.dataset.loc] = [...dropdown.querySelectorAll(
      'input[type="checkbox"]:checked',
    )].map(input => input.value);
  });
  return { day, enabled_work_centers: [...(window.AUTO_SCHEDULE_WC_NAMES || [])], assignments };
}

async function validateCurrentView() {
  const requestId = ++validationRequestId;
  validationController?.abort();
  validationController = new AbortController();
  try {
    const response = await fetch('/api/rotations/validate-current', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      signal: validationController.signal, body: JSON.stringify(currentViewSnapshot()),
    });
    const data = await response.json().catch(() => ({}));
    if (requestId !== validationRequestId) return;
    if (!response.ok || !data.ok) throw new Error(data.error || 'Validation failed.');
    renderCoverageIssues([], data.issues || []);
  } catch (error) {
    if (error.name === 'AbortError' || requestId !== validationRequestId) return;
    renderCoverageIssues([], [validationUnavailableIssue()]);
  }
}

function scheduleCurrentViewValidation() {
  clearTimeout(validationTimer);
  validationTimer = setTimeout(validateCurrentView, 150);
}
```

`validationUnavailableIssue()` returns one structured issue with code
`current_view_validation_unavailable` and message
`Couldn’t validate the schedule currently shown. Try again.` It is not added
to `window.ROTATION_WARNINGS` and it replaces all older staffing issues.

- [ ] **Step 4: Connect every grid-changing path to validation**

Call `scheduleCurrentViewValidation()` after the existing
`staffing:selection-changed` handling, after a picker clear, after a successful
Auto-center save and its `applyAutoCenterAssignments`, and after successful
`applyRebuild`. Call `validateCurrentView()` once during draft-page startup so
the first displayed warning list is refreshed from current authoritative data.
Do not call it in a posted view or after a failed Auto request; failed Auto
requests keep their explicit red failure message until the manager changes the
grid.

Remove `clearStaleAutoWarnings`; complete response replacement makes its
partial string/code filtering both unnecessary and incorrect.

- [ ] **Step 5: Run focused static tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_static.py -q -k 'live_validation or rotation_warning or auto_toggle_failures'`

Expected: PASS, including the Node out-of-order response harness.

- [ ] **Step 6: Commit and push the live warning client**

```bash
git add src/zira_dashboard/static/staffing.js tests/test_staffing_static.py
git commit -m "feat: refresh staffing warnings live"
git push origin main
```

## Task 4: Run regressions and publish the user-facing note

**Files:**

- Modify: `CHANGELOG.md`

**Interfaces:**

- Consumes: the completed validator, route, page, and browser client from Tasks 1–3.
- Produces: verified production-ready behavior and a plain-language patch note.

- [ ] **Step 1: Write the new child-friendly changelog entry**

Under the new push’s date/time heading, add:

```markdown
- **Live schedule warnings** — Schedule warnings now update as you move people around. A warning goes away when the schedule is safe again, so you only see problems that are happening now.
```

- [ ] **Step 2: Run the complete focused regression suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_current_schedule_validation.py tests/test_staffing_rotations.py tests/test_staffing_static.py -q`

Expected: PASS with no failures.

- [ ] **Step 3: Run static linting for the modified Python files**

Run: `.venv/bin/ruff check src/zira_dashboard/current_schedule_validation.py src/zira_dashboard/routes/staffing.py src/zira_dashboard/routes/rotations.py tests/test_current_schedule_validation.py tests/test_staffing_rotations.py tests/test_staffing_static.py`

Expected: `All checks passed!`

- [ ] **Step 4: Review the final diff and working tree**

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only `CHANGELOG.md` is an intentional tracked
change. Preserve unrelated user-owned untracked files.

- [ ] **Step 5: Commit and push the release note**

```bash
git add CHANGELOG.md
git commit -m "docs: note live staffing warnings"
git push origin main
```
