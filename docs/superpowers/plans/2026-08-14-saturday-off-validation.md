# Saturday Off Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Stop the Staffing page from warning that people who declined an optional Saturday or holiday shift are unassigned, while preserving real coverage and safety warnings.

**Architecture:** The optional-workday render model already exposes the committed volunteer names. Thread that set through the staffing route's current-view validation wrappers into the pure validator. The validator uses the optional set only to decide who requires placement and default-placement checks; it leaves work-center validation unchanged.

**Tech Stack:** Python 3.11+, FastAPI, pytest, Ruff.

## Global Constraints

- Optional-workday declines are not Time Off records and must not be treated as them.
- Preserve coverage, capacity, qualification, and training warnings for the visible schedule.
- Preserve ordinary-day validation when no expected-working-names value is supplied.
- Do not stage the pre-existing untracked \`.cursorignore\`, \`.python-version\`, or \`uv.lock\` files.

---

## File Structure

- Modify \`src/zira_dashboard/current_schedule_validation.py\` to accept and apply the optional expected-working-names boundary.
- Modify \`src/zira_dashboard/routes/staffing.py\` to pass committed volunteer names from the optional-workday view into current validation.
- Modify \`tests/test_current_schedule_validation.py\` to lock down the pure validation behavior.
- Modify \`tests/test_staffing_holiday_work.py\` to verify the Staffing page passes only committed volunteer names into validation.
- Modify \`CHANGELOG.md\` with a short, child-friendly entry before the implementation push.

### Task 1: Limit pure unassigned and default validation to expected workers

**Files:**

- Modify: \`src/zira_dashboard/current_schedule_validation.py:20-27, 143-166\`
- Modify: \`tests/test_current_schedule_validation.py:1-180\`

**Interfaces:**

- Consumes: \`expected_working_names: Collection[str] | None\`, where \`None\` means the whole active, non-reserve roster is expected to work.
- Produces: \`validate_current_assignments(..., expected_working_names=...)\`, which omits \`person_unplaced\` and default-placement issues for names outside the supplied collection.

- [ ] **Step 1: Write the failing pure-validator test**

Append this test to \`tests/test_current_schedule_validation.py\`:

\`\`\`python
def test_current_validation_requires_only_expected_optional_workers_to_be_placed():
    locations = (
        staffing.Location("Repair 1", "Repair", "Bay 1", "Recycled", None, 1, 1),
    )

    issues = current_schedule_validation.validate_current_assignments(
        roster=[_person("Volunteer", Repair=3), _person("Saturday Off", Repair=3)],
        assignments={},
        enabled_centers={"Repair 1"},
        locations=locations,
        minimums={"Repair 1": 1},
        capacities={"Repair 1": 1},
        required_skills={"Repair 1": ("Repair",)},
        full_day_off_names=set(),
        trim_saw_centers=set(),
        training_trainees_by_center={},
        exact_defaults={"Repair 1": ["Saturday Off"]},
        group_defaults={},
        user_group_centers={},
        expected_working_names={"Volunteer"},
    )

    codes_by_person = {(issue.code, issue.person) for issue in issues}
    assert ("person_unplaced", "Volunteer") in codes_by_person
    assert ("person_unplaced", "Saturday Off") not in codes_by_person
    assert not any(issue.person == "Saturday Off" for issue in issues)
    assert "center_minimum_unmet" in {issue.code for issue in issues}
\`\`\`

- [ ] **Step 2: Run the new test and verify it fails for the missing interface**

Run:

\`\`\`bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q tests/test_current_schedule_validation.py::test_current_validation_requires_only_expected_optional_workers_to_be_placed
\`\`\`

Expected: FAIL with \`TypeError\` because \`validate_current_assignments\` does not yet accept \`expected_working_names\`.

- [ ] **Step 3: Add the expected-worker boundary to the pure validator**

In \`src/zira_dashboard/current_schedule_validation.py\`, add the optional keyword parameter after \`user_group_centers\`:

\`\`\`python
    expected_working_names: Collection[str] | None = None,
\`\`\`

After \`off = set(full_day_off_names)\`, add:

\`\`\`python
    expected_workers = (
        None if expected_working_names is None else {str(name) for name in expected_working_names}
    )
\`\`\`

Add this condition to the \`person_unplaced\` \`if\` expression:

\`\`\`python
            and (expected_workers is None or person.name in expected_workers)
\`\`\`

At the start of the \`for name, targets in ... defaults_by_person\` loop, extend the existing skip condition to include:

\`\`\`python
        if expected_workers is not None and name not in expected_workers:
            continue
\`\`\`

Keep the existing full-day-time-off logic and every station-level issue branch unchanged.

- [ ] **Step 4: Run the focused validator tests and verify they pass**

Run:

\`\`\`bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q tests/test_current_schedule_validation.py
\`\`\`

Expected: PASS, including the new optional-worker case and all ordinary-day validation cases.

### Task 2: Pass committed volunteers from the Staffing page to validation

**Files:**

- Modify: \`src/zira_dashboard/routes/staffing.py:737-774, 984-1111, 2110-2128\`
- Modify: \`tests/test_staffing_holiday_work.py:374-504, 716-767\`

**Interfaces:**

- Consumes: \`bay_model["saturday_committed_names"]\`, the authoritative names willing to work on an optional workday.
- Produces: \`current_view_validation_for_day(..., expected_working_names=...)\` and \`_recycled_context_for_day(..., expected_working_names=...)\`; ordinary page renders pass \`None\`.

- [ ] **Step 1: Write the failing Staffing-page wiring test**

First add \`recycled_context=None\` to \`_render_staffing\`'s keyword-only parameters in \`tests/test_staffing_holiday_work.py\`. Replace its fixed \`_recycled_context_for_day\` monkeypatch with:

\`\`\`python
    def default_recycled_context(*_args, **_kwargs):
        return {
            "recycled_rotation_mode": "normal",
            "rotation_reasons": {},
            "rotation_reason_codes": {},
            "rotation_warnings": [],
            "rotation_issues": [],
            "active_training_blocks": [],
        }

    monkeypatch.setattr(
        staffing_routes,
        "_recycled_context_for_day",
        recycled_context or default_recycled_context,
    )
\`\`\`

Then append this test:

\`\`\`python
def test_optional_workday_passes_only_committed_volunteers_to_validation(monkeypatch):
    captured = {}

    def capture_recycled_context(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "recycled_rotation_mode": "normal",
            "rotation_reasons": {},
            "rotation_reason_codes": {},
            "rotation_warnings": [],
            "rotation_issues": [],
            "active_training_blocks": [],
        }

    _render_staffing(
        monkeypatch,
        optional_day=_holiday(),
        bundle=_bundle(),
        roster=[_person("Volunteer"), _person("Saturday Off")],
        real_bay_model=True,
        recycled_context=capture_recycled_context,
    )

    assert captured["expected_working_names"] == {"Volunteer"}
\`\`\`

- [ ] **Step 2: Run the new page-wiring test and verify it fails for the missing argument**

Run:

\`\`\`bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q tests/test_staffing_holiday_work.py::test_optional_workday_passes_only_committed_volunteers_to_validation
\`\`\`

Expected: FAIL with \`KeyError: 'expected_working_names'\` because the page has not passed the volunteer set yet.

- [ ] **Step 3: Thread the expected-worker value through the Staffing route**

In \`src/zira_dashboard/routes/staffing.py\`, change the current-validation wrapper signature and call:

\`\`\`python
def current_view_validation_for_day(
    *, day, assignments, enabled_work_centers, expected_working_names=None
) -> list[dict[str, object]]:
    enabled = _ordered_work_center_names(enabled_work_centers)
    roster = staffing.load_roster()
    time_off_entries = scheduler_time_off.time_off_entries_for_day(day)
    exact_defaults, group_defaults, user_group_centers = _default_inputs(strict=True)
    group_locations, group_required_skills = _auto_group_maps(set(enabled))
    required_skills = {
        center: group_required_skills[group]
        for group, centers in group_locations.items()
        for center in centers
    }
    issues = current_schedule_validation.validate_current_assignments(
        roster=roster,
        assignments=assignments,
        enabled_centers=enabled,
        locations=staffing.LOCATIONS,
        minimums={
            loc.name: _effective_minimum(loc) for loc in staffing.LOCATIONS if loc.name in enabled
        },
        capacities=_configured_center_capacities(enabled, strict=True),
        required_skills=required_skills,
        full_day_off_names=rotation_suggestions._full_day_time_off_names(time_off_entries),
        trim_saw_centers=set(group_locations.get(rotation_suggestions.TRIM_SAW_SKILL, ())),
        training_trainees_by_center=_current_training_trainees_by_center(day, assignments),
        exact_defaults=exact_defaults,
        group_defaults=group_defaults,
        user_group_centers=user_group_centers,
        expected_working_names=expected_working_names,
    )
\`\`\`

Add \`expected_working_names=None\` as a keyword-only argument to \`_recycled_context_for_day\`, then pass it into the \`current_view_validation_for_day\` call:

\`\`\`python
            visible_issues = current_view_validation_for_day(
                day=d,
                assignments=current_assignments if current_assignments is not None else {},
                enabled_work_centers=enabled,
                expected_working_names=expected_working_names,
            )
\`\`\`

Finally, in \`staffing_page\`, pass the optional-workday committed names only after the optional \`bay_model\` rebuild:

\`\`\`python
        expected_working_names=(
            set(bay_model.get("saturday_committed_names") or ())
            if render_as_optional_day
            else None
        ),
\`\`\`

Do not alter the existing \`current_assignments\`, published-view, or optional-workday save/publish enforcement paths.

- [ ] **Step 4: Run the focused regression suite and verify it passes**

Run:

\`\`\`bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q \
  tests/test_current_schedule_validation.py \
  tests/test_staffing_holiday_work.py \
  tests/test_staffing_saturday_recruiting.py \
  tests/test_staffing_rotations.py
\`\`\`

Expected: PASS. The optional-worker test suppresses only the off person's personal warnings, while existing Saturday and holiday workflows retain their current checks.

### Task 3: Publish the user-facing fix note

**Files:**

- Modify: \`CHANGELOG.md:1\`

**Interfaces:**

- Consumes: the verified Saturday-off behavior from Tasks 1 and 2.
- Produces: a short What's New note that explains the visible scheduler correction in plain language.

- [ ] **Step 1: Add the new changelog entry**

Insert this new bullet at the top of the current release section, leaving every historical entry unchanged:

\`\`\`markdown
- **Saturday schedules no longer blame people who said they were off.** On an optional Saturday or holiday shift, people who did not volunteer no longer show up as yellow “not assigned” errors. Real staffing and safety warnings still show for the people who said yes.
\`\`\`

- [ ] **Step 2: Run lint and the complete test suite**

Run:

\`\`\`bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/ruff check src/zira_dashboard/current_schedule_validation.py src/zira_dashboard/routes/staffing.py tests/test_current_schedule_validation.py tests/test_staffing_holiday_work.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q
\`\`\`

Expected: Ruff exits \`0\`; pytest exits \`0\` with the full suite passing.

- [ ] **Step 3: Review the final diff**

Run:

\`\`\`bash
git diff --check
git diff -- src/zira_dashboard/current_schedule_validation.py src/zira_dashboard/routes/staffing.py tests/test_current_schedule_validation.py tests/test_staffing_holiday_work.py CHANGELOG.md
\`\`\`

Expected: no whitespace errors; the diff contains only the availability boundary, its regression tests, and the new What's New note.

- [ ] **Step 4: Commit and push the implementation**

Run:

\`\`\`bash
git add CHANGELOG.md src/zira_dashboard/current_schedule_validation.py src/zira_dashboard/routes/staffing.py tests/test_current_schedule_validation.py tests/test_staffing_holiday_work.py
git commit -m "fix: exclude Saturday declines from assignment warnings"
git push origin main
\`\`\`

Expected: one implementation commit reaches \`origin/main\`; the unrelated untracked files remain unstaged.
