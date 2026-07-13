# Scheduling Preferences Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Let managers set Primary, Regular, Occasional, or Never for every work-center group or standalone center an operator is qualified to run, and make Auto scheduling honor those preferences.

**Architecture:** Add shared preference-target helpers to \`staffing.py\`. The Skills route renders a per-person eligible target list, while the Staffing route supplies the same target metadata to the generic scheduling engine. The existing preference table retains its current key/value form, but dynamic target validation replaces the former Recycled-only target list.

**Tech Stack:** Python 3, FastAPI, Jinja, vanilla JavaScript/CSS, pytest.

## Global Constraints

- A group is two or more locations that share exactly one required skill. Every other location is standalone.
- A person qualifies only with level 1+ in every required skill of the target.
- Missing choice means Regular; accepted values remain Primary, Regular, Occasional, and Never.
- Keep saved defaults, manual locks, capacity, sibling-center fairness, and Trim Saw pairing safety intact.
- Level-0 training remains Recycled-only and separate from routine scheduling choices.
- Do not modify the pre-existing untracked \`.claude/\` directory.

---

## File Structure

- \`src/zira_dashboard/staffing.py\` — preference target derivation and qualification.
- \`src/zira_dashboard/rotation_store.py\` — dynamic preference key validation.
- \`src/zira_dashboard/routes/staffing.py\` — target map for enabled Auto centers.
- \`src/zira_dashboard/rotation_suggestions.py\` — multi-skill target scoring.
- \`src/zira_dashboard/routes/skills.py\`, \`templates/skills.html\`, \`static/skills-page.js\`, and \`static/skills.css\` — qualified preference dialog and icon.
- \`src/zira_dashboard/routes/rotations.py\` — reject unqualified preference writes.
- \`tests/test_rotation_store.py\`, \`tests/test_rotation_suggestions.py\`, and \`tests/test_staffing_rotations.py\` — behavioral coverage.
- \`README.md\` — manager workflow copy.

### Task 1: Shared preference targets and persistence validation

**Files:**
- Modify: \`src/zira_dashboard/staffing.py\` after \`required_skills_for\`
- Modify: \`src/zira_dashboard/rotation_store.py:12-65\`
- Test: \`tests/test_rotation_store.py\`

**Interfaces:**
- Produces \`SchedulingPreferenceTarget(key, label, centers, required_skills)\`.
- Produces \`scheduling_preference_targets() -> tuple[SchedulingPreferenceTarget, ...]\`.
- Produces \`eligible_scheduling_preference_targets(person) -> tuple[SchedulingPreferenceTarget, ...]\`.

- [x] **Step 1: Write failing target derivation tests**

~~~python
def test_scheduling_preference_targets_group_sibling_centers():
    from zira_dashboard import staffing

    targets = {target.key: target for target in staffing.scheduling_preference_targets()}

    assert targets["Repair"].centers == (
        "Repair 1", "Repair 2", "Repair 3", "Repair 4", "Repair 5",
    )
    assert targets["Hand Build"].centers == (
        "Hand Build #2", "Hand Build #1", "Big Build #1",
    )
    assert targets["Woodpecker #1"].centers == ("Woodpecker #1",)
    assert targets["Woodpecker #1"].required_skills == ("Woodpecker",)


def test_eligible_targets_require_every_required_skill():
    from zira_dashboard import staffing

    person = staffing.Person(
        "Qualified", skills={"Repair": 1, "Loading": 1, "CPUs/VDOs": 1}
    )
    keys = {target.key for target in staffing.eligible_scheduling_preference_targets(person)}
    assert "Repair" in keys
    assert "Loading/Jockeying" not in keys

    person.skills["Trailer Jockeying"] = 1
    assert "Loading/Jockeying" in {
        target.key for target in staffing.eligible_scheduling_preference_targets(person)
    }
~~~

- [x] **Step 2: Verify red**

Run: \`ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_store.py -q\`

Expected: FAIL because \`scheduling_preference_targets\` does not exist.

- [x] **Step 3: Implement shared derivation**

~~~python
@dataclass(frozen=True)
class SchedulingPreferenceTarget:
    key: str
    label: str
    centers: tuple[str, ...]
    required_skills: tuple[str, ...]


def scheduling_preference_targets() -> tuple[SchedulingPreferenceTarget, ...]:
    single_skill_centers: dict[str, list[str]] = {}
    for loc in LOCATIONS:
        required = required_skills_for(loc)
        if len(required) == 1:
            single_skill_centers.setdefault(required[0], []).append(loc.name)
    grouped_skills = {
        skill for skill, centers in single_skill_centers.items() if len(centers) > 1
    }

    targets = []
    emitted_groups = set()
    for loc in LOCATIONS:
        required = required_skills_for(loc)
        if len(required) == 1 and required[0] in grouped_skills:
            skill = required[0]
            if skill not in emitted_groups:
                targets.append(SchedulingPreferenceTarget(
                    skill, skill, tuple(single_skill_centers[skill]), (skill,)
                ))
                emitted_groups.add(skill)
        else:
            targets.append(SchedulingPreferenceTarget(
                loc.name, loc.name, (loc.name,), required
            ))
    return tuple(targets)


def eligible_scheduling_preference_targets(person: Person) -> tuple[SchedulingPreferenceTarget, ...]:
    return tuple(
        target for target in scheduling_preference_targets()
        if all(person.level(skill) >= 1 for skill in target.required_skills)
    )
~~~

Import \`staffing\` in \`rotation_store.py\` and replace its fixed group validation with:

~~~python
if group not in {target.key for target in staffing.scheduling_preference_targets()}:
    raise InvalidRotationPreference(f"Unknown rotation group: {group!r}")
~~~

Keep \`ROTATION_GROUPS\` unchanged for Recycled-only training block validation.

- [x] **Step 4: Verify green**

Run: \`ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_store.py -q\`

Expected: PASS.

- [x] **Step 5: Commit**

~~~bash
git add src/zira_dashboard/staffing.py src/zira_dashboard/rotation_store.py tests/test_rotation_store.py
git commit -m "feat: derive scheduling preference targets"
~~~

### Task 2: Schedule target groups and standalone centers with preferences

**Files:**
- Modify: \`src/zira_dashboard/routes/staffing.py:172-182, 300-430\`
- Modify: \`src/zira_dashboard/rotation_suggestions.py:490-780\`
- Test: \`tests/test_rotation_suggestions.py\`
- Test: \`tests/test_staffing_rotations.py\`

**Interfaces:**
- Extends \`suggest_recycled_assignments(..., group_required_skills: dict[str, tuple[str, ...]] | None = None)\`.
- Produces \`_auto_group_maps(enabled_work_centers) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]\`.
- A target level is the lowest of the operator's required-skill levels.

- [x] **Step 1: Write failing generic-scheduling tests**

~~~python
def test_generic_engine_honors_standalone_preference():
    from zira_dashboard.rotation_suggestions import RecycledHistory, suggest_recycled_assignments

    roster = [
        staffing.Person("Primary", skills={"Woodpecker": 2}),
        staffing.Person("Regular", skills={"Woodpecker": 3}),
    ]
    out = suggest_recycled_assignments(
        day=TARGET_DAY, mode="normal", roster=roster,
        preferences={"Primary": {"Woodpecker #1": "primary"}},
        base_assignments={},
        group_locations={"Woodpecker #1": ("Woodpecker #1",)},
        group_required_skills={"Woodpecker #1": ("Woodpecker",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    assert out.assignments["Woodpecker #1"] == ["Primary"]


def test_auto_group_maps_keep_hand_build_centers_under_one_target():
    from zira_dashboard.routes import staffing as staffing_route

    locations, skills = staffing_route._auto_group_maps({"Hand Build #1", "Hand Build #2"})

    assert locations == {"Hand Build": ("Hand Build #2", "Hand Build #1")}
    assert skills == {"Hand Build": ("Hand Build",)}
~~~

- [x] **Step 2: Verify red**

Run: \`ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_suggestions.py tests/test_staffing_rotations.py -q\`

Expected: FAIL with an unexpected \`group_required_skills\` argument and missing \`_auto_group_maps\`.

- [x] **Step 3: Implement target-aware maps and levels**

Replace \`_auto_group_locations\` with:

~~~python
def _auto_group_maps(enabled_work_centers):
    enabled = set(_ordered_work_center_names(enabled_work_centers))
    locations = {}
    required_skills = {}
    for target in staffing.scheduling_preference_targets():
        centers = tuple(center for center in target.centers if center in enabled)
        if centers:
            locations[target.key] = centers
            required_skills[target.key] = target.required_skills
    return locations, required_skills
~~~

Pass both maps through \`_recycled_suggestion_for_day\`. In the engine, default omitted metadata to \`(group,)\` for current direct callers and use:

~~~python
def _group_level(person, group, group_required_skills):
    if person is None:
        return 0
    skills = group_required_skills.get(group, (group,))
    return min(
        (max(0, min(3, int(person.level(skill)))) for skill in skills),
        default=0,
    )
~~~

Replace every generic \`_recycled_level(person, group)\` call with \`_group_level(person, group, group_required_skills)\`. Preserve the existing Trim Saw safety branch and its pairing rules, but pass it the resolved group level.

- [x] **Step 4: Verify green**

Run: \`ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_suggestions.py tests/test_staffing_rotations.py -q\`

Expected: PASS.

- [x] **Step 5: Commit**

~~~bash
git add src/zira_dashboard/routes/staffing.py src/zira_dashboard/rotation_suggestions.py tests/test_rotation_suggestions.py tests/test_staffing_rotations.py
git commit -m "feat: honor preferences across auto work centers"
~~~

### Task 3: Render qualified Scheduling Preferences dialog

**Files:**
- Modify: \`src/zira_dashboard/routes/skills.py:64-125\`
- Modify: \`src/zira_dashboard/templates/skills.html:70-145, 190-200\`
- Modify: \`src/zira_dashboard/static/skills-page.js:1130-1420\`
- Modify: \`src/zira_dashboard/static/skills.css:328-360\`
- Test: \`tests/test_staffing_rotations.py\`

**Interfaces:**
- Adds route context \`rotation_preference_targets_by_person: dict[str, list[dict[str, str]]]\`.
- Browser reads \`window.ROTATION_PREFERENCE_TARGETS_BY_PERSON\`.
- The existing POST body remains \`{person, group, preference}\`, with \`group\` now a general target key.

- [x] **Step 1: Write failing route and static-contract tests**

~~~python
def test_skills_context_only_exposes_qualified_preference_targets(monkeypatch):
    # Reuse test_staffing_skills_context_includes_rotation_editor_data's
    # template capture, but patch load_roster with this exact roster.
    monkeypatch.setattr(
        skills_routes.staffing, "load_roster",
        lambda: [staffing.Person("Alex", skills={"Repair": 1, "Woodpecker": 0})],
    )
    skills_routes.staffing_skills(request=object())
    assert captured["context"]["rotation_preference_targets_by_person"]["Alex"] == [
        {"key": "Repair", "label": "Repair"}
    ]


def test_people_matrix_uses_dynamic_scheduling_preferences_picker():
    html = (ROOT / "src/zira_dashboard/templates/skills.html").read_text()
    js = (ROOT / "src/zira_dashboard/static/skills-page.js").read_text()

    assert 'aria-label="Scheduling preferences for {{ p.name }}"' in html
    assert "<svg" in html
    assert "ROTATION_PREFERENCE_TARGETS_BY_PERSON" in html
    assert "renderPreferences(person)" in js
    assert "data.rotationPreference" in js
~~~

- [x] **Step 2: Verify red**

Run: \`ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py -q\`

Expected: FAIL because the context and dynamic icon/rendering contracts do not exist.

- [x] **Step 3: Add eligible target context and modal markup**

Keep \`rotation_groups\` and \`rotation_levels\` for Recycled training. Add to the Skills route:

~~~python
rotation_preference_targets_by_person = {
    person.name: [
        {"key": target.key, "label": target.label}
        for target in staffing.eligible_scheduling_preference_targets(person)
    ]
    for person in roster
}
~~~

Pass it into the template. Replace the fixed Jinja select loop with:

~~~html
<div class="rotation-pref-grid" id="rotation-pref-grid"></div>
<script>
  window.ROTATION_PREFERENCE_TARGETS_BY_PERSON =
    {{ rotation_preference_targets_by_person | tojson }};
</script>
~~~

Rename the dialog to \`Scheduling Preferences\`. Keep training section copy explicitly Recycled. Replace the glyph with a compact inline two-arrow SVG inside the existing button and set its title and aria-label to \`Scheduling preferences for {{ p.name }}\`.

- [x] **Step 4: Render rows in JavaScript and retain save behavior**

Replace the static \`prefSelects\` setup with \`renderPreferences(person)\`, which clears the grid, reads the target list for that person, and creates the label and select. The implementation must attach the existing save/revert behavior to each dynamic select:

~~~javascript
function renderPreferences(person) {
  prefGrid.textContent = '';
  const saved = PREFS[person] || {};
  (PREFERENCE_TARGETS_BY_PERSON[person] || []).forEach(target => {
    const label = document.createElement('label');
    label.className = 'rotation-pref';
    const name = document.createElement('span');
    name.className = 'rotation-pref-group';
    name.textContent = target.label;
    const select = document.createElement('select');
    select.className = 'rotation-pref-select';
    select.dataset.rotationPreference = '';
    select.dataset.group = target.key;
    select.dataset.person = person;
    select.dataset.prev = saved[target.key] || 'regular';
    ["primary", "regular", "occasional", "never"].forEach(value => {
      select.add(new Option(value[0].toUpperCase() + value.slice(1), value));
    });
    select.value = select.dataset.prev;
    select.addEventListener('change', () => savePreference(select));
    label.append(name, select);
    prefGrid.appendChild(label);
  });
}
~~~

Extract the existing endpoint request, disabled state, \`PREFS\` update, toast, and failure revert into \`savePreference(select)\`. Call \`renderPreferences(person)\` from \`openModal\`. Make the button square enough for the SVG's pointer target while retaining the current hover and focus styles.

- [x] **Step 5: Verify green**

Run: \`ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py tests/test_staffing_static.py tests/test_skills_static.py tests/test_skills_template_render.py -q\`

Expected: PASS.

- [x] **Step 6: Commit**

~~~bash
git add src/zira_dashboard/routes/skills.py src/zira_dashboard/templates/skills.html src/zira_dashboard/static/skills-page.js src/zira_dashboard/static/skills.css tests/test_staffing_rotations.py
git commit -m "feat: show qualified scheduling preferences"
~~~

### Task 4: Reject unqualified writes, document, and verify

**Files:**
- Modify: \`src/zira_dashboard/routes/rotations.py:1-90\`
- Modify: \`tests/test_staffing_rotations.py:39-113\`
- Modify: \`README.md:56-78\`
- Modify: this plan's completion checkboxes as work proceeds.

**Interfaces:**
- \`POST /api/rotations/preferences\` returns 422 when the target is unavailable to the identified person.

- [x] **Step 1: Write the failing endpoint eligibility test**

~~~python
def test_preference_endpoint_rejects_unqualified_target(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    monkeypatch.setattr(rotations.db, "query", lambda sql, params=None: [{"id": 7}])
    monkeypatch.setattr(
        rotations.staffing, "load_roster",
        lambda: [staffing.Person("Alex", skills={"Repair": 0})],
    )

    resp = client.post(
        "/api/rotations/preferences",
        json={"person": "Alex", "group": "Repair", "preference": "primary"},
    )

    assert resp.status_code == 422
    assert "qualified" in resp.json()["error"]
~~~

- [x] **Step 2: Verify red**

Run: \`ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py::test_preference_endpoint_rejects_unqualified_target -q\`

Expected: FAIL with \`assert 200 == 422\`.

- [x] **Step 3: Validate before saving and update manager documentation**

After resolving \`person_id\`, load the matching roster person and derive its eligible target keys. Return \`_error(f"{person} is not qualified for {group}.")\` unless the posted key is eligible. Then call the existing store save path.

Change the rotations-route docstring from Recycled preferences to scheduling preferences. Update README step 1 to say: open the Scheduling Preferences icon on People Matrix; it lists only qualified grouped and standalone targets; those choices influence enabled Auto work centers. Preserve the existing Auto toggle, locks, training, and fair rotation guidance.

- [x] **Step 4: Verify green**

Run: \`ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_store.py tests/test_rotation_suggestions.py tests/test_staffing_rotations.py tests/test_staffing_trim_saw_defaults.py tests/test_staffing_static.py tests/test_skills_static.py tests/test_skills_template_render.py -q\`

Expected: PASS.

- [x] **Step 5: Run full verification**

Run: \`ZIRA_API_KEY=test .venv/bin/python -m pytest -q\`

Expected: all application tests PASS. If the known sandbox-only macOS Playwright bootstrap error \`MachPortRendezvous Permission denied (1100)\` recurs, report it as an environment limitation without changing product code.

Run: \`git diff --check && git status --short --branch\`

Expected: no whitespace errors and only intended feature files plus the pre-existing untracked \`.claude/\` directory.

- [x] **Step 6: Commit**

~~~bash
git add src/zira_dashboard/routes/rotations.py tests/test_staffing_rotations.py README.md docs/superpowers/plans/2026-07-13-scheduling-preferences.md
git commit -m "feat: validate scheduling preferences"
~~~
