# Recycled Smart Rotations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build safe, explainable Recycled-area scheduling for Dismantler, Repair, and Trim Saw, including mode-based recommendations, per-person preferences, center fairness, and level-0 training blocks.

**Architecture:** Keep database reads/writes in focused rotation stores, pure scoring and center-selection logic in `rotation_suggestions.py`, and HTTP/template concerns in small rotation routes plus the existing Staffing page. Extend `staffing.Schedule` with rotation metadata so generated and manually locked assignments round-trip through drafts and posted snapshots without changing non-Recycled scheduling.

**Tech Stack:** FastAPI, Jinja, vanilla JavaScript, PostgreSQL, pytest, existing Odoo skill-update client.

## Global Constraints

- Scope automated rotation to Recycled locations whose required skill is `Dismantler`, `Repair`, or `Trim Saw`.
- `normal` is the default Recycled schedule mode; valid modes are exactly `optimized`, `normal`, and `training`.
- Preference values are exactly `primary`, `regular`, `occasional`, and `never`; a missing preference is `regular`.
- Level 0 may be assigned only by an active level-0 training block. The daily Training mode develops only levels 1 and 2, paired with level 3.
- A training block pairs the trainee and selected level-3 trainer on the first attended day; subsequent attended days reserve only the trainee; full-day absences extend the block.
- Completing all attended block days automatically raises the target skill from 0 to 1.
- Rebuilds must preserve manually locked assignments and never affect non-Recycled locations.
- Preserve current Trim Saw pair guarantees, next-day seeding, and fallback behavior.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/zira_dashboard/_schema.py` | Tables and additive schedule columns for rotation preferences, training blocks, and schedule metadata. |
| `src/zira_dashboard/rotation_store.py` | PostgreSQL persistence for preferences, blocks, and attended training days. |
| `src/zira_dashboard/rotation_suggestions.py` | Pure Recycled eligibility, scoring, safety, and fair-center recommendation logic; retain Trim Saw wrappers. |
| `src/zira_dashboard/rotation_training.py` | Translate active blocks into per-day locks and reconcile completed attended days. |
| `src/zira_dashboard/skill_levels.py` | Shared Odoo-plus-local skill-level writer used by the matrix and block completion. |
| `src/zira_dashboard/staffing.py` | `Schedule` rotation metadata and DB hydration/persistence. |
| `src/zira_dashboard/routes/rotations.py` | JSON endpoints for preferences, schedule recomputation, and training-block lifecycle. |
| `src/zira_dashboard/routes/staffing.py` | Supply recommendation data while seeding/rebuilding a schedule. |
| `src/zira_dashboard/app.py` | Register the new rotations router. |
| `src/zira_dashboard/templates/staffing.html` | Mode picker, reasons, warnings, and reset controls. |
| `src/zira_dashboard/static/staffing.js` | Submit mode/reset actions and preserve manual locks during autosave. |
| `src/zira_dashboard/templates/skills.html` | Per-person Recycled preference editor. |
| `src/zira_dashboard/static/skills-page.js` | Save the preference editor through JSON. |
| `src/zira_dashboard/static/skills.css` | Compact preference controls and training/recommendation status styling. |
| `tests/test_rotation_store.py` | Preference/block persistence and schema-facing behavior. |
| `tests/test_rotation_suggestions.py` | Mode scoring, safety, and group/center history rules. |
| `tests/test_rotation_training.py` | Day-one pairing, absence extension, conflicts, and promotion. |
| `tests/test_staffing_rotations.py` | Staffing schedule metadata, JSON APIs, and template/static contracts. |

### Task 1: Add additive rotation persistence and schedule metadata

**Files:**
- Modify: `src/zira_dashboard/_schema.py:122-150`
- Modify: `src/zira_dashboard/staffing.py:300-520`
- Create: `src/zira_dashboard/rotation_store.py`
- Create: `tests/test_rotation_store.py`

**Interfaces:**
- Produces `RotationPreference`, `TrainingBlock`, `load_preferences()`, `save_preference()`, `create_block()`, `active_blocks_for_day()`, and `record_attended_day()` for later tasks.
- Produces `Schedule.rotation_mode: str` and `Schedule.assignment_sources: dict[str, dict[str, str]]` where each source is `generated` or `manual`.

- [ ] **Step 1: Write persistence characterization tests**

```python
def test_missing_rotation_preference_is_regular(monkeypatch):
    monkeypatch.setattr(rotation_store.db, "query", lambda *_args, **_kwargs: [])
    assert rotation_store.preference_for({}, 17, "Repair") == "regular"


def test_training_block_rejects_non_green_trainer():
    with pytest.raises(rotation_store.InvalidTrainingBlock, match="level 3"):
        rotation_store.validate_block(level=0, trainer_level=2, workdays=5)


def test_schedule_metadata_round_trips(monkeypatch):
    schedule = staffing.Schedule(
        day=date(2026, 7, 14),
        assignments={"Repair 1": ["Jordan"]},
        rotation_mode="training",
        assignment_sources={"Repair 1": {"Jordan": "manual"}},
    )
    # Assert the save parameters include mode and JSON source data, then hydrate
    # the identical values from a fake schedules row.
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_store.py -v`

Expected: FAIL because `rotation_store` and the new `Schedule` fields do not exist.

- [ ] **Step 3: Add the schema and focused store**

Add these additive schema elements after the current schedule tables:

```sql
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS recycled_rotation_mode TEXT NOT NULL DEFAULT 'normal';
ALTER TABLE schedules ADD COLUMN IF NOT EXISTS assignment_sources JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS person_rotation_preferences (
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  rotation_group TEXT NOT NULL CHECK (rotation_group IN ('Dismantler', 'Repair', 'Trim Saw')),
  preference TEXT NOT NULL CHECK (preference IN ('primary', 'regular', 'occasional', 'never')),
  PRIMARY KEY (person_id, rotation_group)
);

CREATE TABLE IF NOT EXISTS rotation_training_blocks (
  id BIGSERIAL PRIMARY KEY,
  trainee_id INTEGER NOT NULL REFERENCES people(id),
  trainer_id INTEGER NOT NULL REFERENCES people(id),
  skill_id INTEGER NOT NULL REFERENCES skills(id),
  start_day DATE NOT NULL,
  planned_attended_days SMALLINT NOT NULL CHECK (planned_attended_days > 0),
  status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'completed', 'ended')),
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rotation_training_block_days (
  block_id BIGINT NOT NULL REFERENCES rotation_training_blocks(id) ON DELETE CASCADE,
  day DATE NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('attended', 'absent', 'conflict')),
  PRIMARY KEY (block_id, day)
);
```

Create `rotation_store.py` with immutable records and input validation:

```python
ROTATION_GROUPS = ("Dismantler", "Repair", "Trim Saw")
PREFERENCES = ("primary", "regular", "occasional", "never")

@dataclass(frozen=True)
class TrainingBlock:
    id: int
    trainee_name: str
    trainer_name: str
    skill: str
    start_day: date
    planned_attended_days: int
    status: str

def preference_for(preferences: dict[tuple[int, str], str], person_id: int, group: str) -> str:
    return preferences.get((person_id, group), "regular")

def validate_block(*, level: int, trainer_level: int, workdays: int) -> None:
    if level != 0:
        raise InvalidTrainingBlock("Trainee must be level 0 for the target skill.")
    if trainer_level != 3:
        raise InvalidTrainingBlock("Day-one trainer must be level 3 for the target skill.")
    if workdays < 1:
        raise InvalidTrainingBlock("Training block must contain at least one attended workday.")
```

Add the `rotation_mode` and `assignment_sources` fields to `staffing.Schedule`, include them in `snapshot_of`, both schedule load paths, and the existing `INSERT INTO schedules (day, published, testing_day, notes, custom_hours, published_snapshot, updated_at) ON CONFLICT (day)` statement in `save_schedule`. Default missing/old rows to `normal` and `{}`.

- [ ] **Step 4: Run persistence tests**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_store.py tests/test_staffing_trim_saw_defaults.py -v`

Expected: PASS, including old Trim Saw scheduling tests.

- [ ] **Step 5: Commit the persistence layer**

```bash
git add src/zira_dashboard/_schema.py src/zira_dashboard/staffing.py src/zira_dashboard/rotation_store.py tests/test_rotation_store.py
git commit -m "feat: persist recycled rotation settings"
```

### Task 2: Build pure group scoring and fair work-center selection

**Files:**
- Modify: `src/zira_dashboard/rotation_suggestions.py`
- Modify: `tests/test_rotation_suggestions.py`

**Interfaces:**
- Consumes `staffing.Person`, a group-to-location map, preferences, saved assignment history, manual locks, and active training effects.
- Produces `RecycledSuggestion(assignments, sources, reasons, warnings)` through `suggest_recycled_assignments(day, mode, roster, preferences, base_assignments, group_locations, history, locked_assignments, block_effects, training_cap)`.

- [ ] **Step 1: Add failing mode, safety, and center-fairness tests**

```python
def test_normal_mode_uses_primary_preference_before_regular():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal", roster=[_person("Primary", 3), _person("Regular", 3)],
        preferences={"Primary": {"Repair": "primary"}, "Regular": {"Repair": "regular"}},
        base_assignments={}, history=RecycledHistory(), locked_assignments={}, block_effects=[],
    )
    assert out.assignments["Repair 1"] == ["Primary"]


def test_training_mode_pairs_level_one_with_level_three():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="training", roster=[_person("Green", 3), _person("Learner", 1)],
        preferences={"Green": {"Repair": "regular"}, "Learner": {"Repair": "regular"}},
        base_assignments={}, group_locations={"Repair": ("Repair 1",)}, history=RecycledHistory(),
        locked_assignments={}, block_effects=(), training_cap=2,
    )
    assert {"Green", "Learner"} <= set(out.people_for_group("Repair"))


def test_level_zero_is_ignored_without_training_block():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal", roster=[_person("Zero", 0), _person("Green", 3)],
        preferences={"Zero": {"Repair": "primary"}, "Green": {"Repair": "regular"}},
        base_assignments={}, group_locations={"Repair": ("Repair 1",)}, history=RecycledHistory(),
        locked_assignments={}, block_effects=(), training_cap=2,
    )
    assert "Zero" not in out.assigned_people


def test_repair_center_rotation_uses_least_recent_then_least_frequent():
    history = RecycledHistory(center_counts={("Jordan", "Repair 1"): 1, ("Jordan", "Repair 2"): 1, ("Jordan", "Repair 3"): 1}, last_center_by_person_group={("Jordan", "Repair"): "Repair 3"})
    assert choose_center("Jordan", "Repair", ("Repair 1", "Repair 2", "Repair 3"), history) == "Repair 1"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_suggestions.py -v`

Expected: FAIL because `RecycledHistory`, `suggest_recycled_assignments`, and `choose_center` do not exist.

- [ ] **Step 3: Add generic recommendation types and deterministic scores**

Keep the existing Trim Saw constants and public wrapper. Add these types and group map near the top of `rotation_suggestions.py`:

```python
RECYCLED_GROUPS = ("Dismantler", "Repair", "Trim Saw")
PREFERENCE_POINTS = {"primary": 30, "regular": 15, "occasional": 3, "never": -10_000}
MODE_SKILL_POINTS = {
    "optimized": {3: 100, 2: 55, 1: 5, 0: -10_000},
    "normal": {3: 55, 2: 40, 1: 25, 0: -10_000},
    "training": {3: 45, 2: 70, 1: 80, 0: -10_000},
}

@dataclass(frozen=True)
class RecycledSuggestion:
    assignments: dict[str, list[str]]
    sources: dict[str, dict[str, str]]
    reasons: dict[str, dict[str, str]]
    warnings: Sequence[str]

def choose_center(name: str, group: str, centers: Sequence[str], history: RecycledHistory) -> str:
    return min(centers, key=lambda center: (
        history.center_counts.get((name, center), 0),
        center == history.last_center_by_person_group.get((name, group)),
        center.lower(),
    ))
```

Implement `suggest_recycled_assignments` to (a) start from non-Recycled and manual assignments unchanged, (b) reserve validated block effects, (c) ensure a level-3 person exists before adding a level-1/2 training candidate, (d) prevent duplicate people across centers, and (e) attach a human-readable reason for every generated assignment. Apply the daily training cap after block effects; default it to `2`.

- [ ] **Step 4: Run focused unit tests and the current Trim Saw regression tests**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_suggestions.py tests/test_staffing_trim_saw_defaults.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the scoring engine**

```bash
git add src/zira_dashboard/rotation_suggestions.py tests/test_rotation_suggestions.py
git commit -m "feat: add recycled rotation recommendations"
```

### Task 3: Add training-block day effects and automatic level promotion

**Files:**
- Create: `src/zira_dashboard/rotation_training.py`
- Create: `src/zira_dashboard/skill_levels.py`
- Modify: `src/zira_dashboard/routes/skills.py`
- Create: `tests/test_rotation_training.py`
- Modify: `tests/test_skills_routes.py`

**Interfaces:**
- Consumes `TrainingBlock`, working-day configuration, full-day absence names, and roster skill levels.
- Produces `BlockEffect(locked_people, temporary_extra_people, warnings)` and `reconcile_blocks(as_of: date) -> list[int]`.
- Produces `set_person_skill_level(person_id: int, skill_id: int, level: int) -> None` for both matrix edits and block completion.

- [ ] **Step 1: Write lifecycle tests**

```python
def test_first_attended_day_pairs_trainee_and_green_trainer():
    effect = effect_for_day(_block(start_day=date(2026, 7, 14)), date(2026, 7, 14), absent_names=set())
    assert effect.locked_people == {"Repair": ["Trainee"]}
    assert effect.temporary_extra_people == {"Repair": ["Trainer"]}


def test_absence_does_not_consume_training_day():
    days = planned_block_days(_block(start_day=date(2026, 7, 14), planned_attended_days=3), {date(2026, 7, 15): {"Trainee"}})
    assert days == [date(2026, 7, 14), date(2026, 7, 16), date(2026, 7, 17)]


def test_reconcile_promotes_once_after_last_attended_day(monkeypatch):
    promoted = []
    monkeypatch.setattr(skill_levels, "set_person_skill_level", lambda *args: promoted.append(args))
    reconcile_blocks(date(2026, 7, 21))
    assert len(promoted) == 1
```

- [ ] **Step 2: Run lifecycle tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_training.py tests/test_skills_routes.py -v`

Expected: FAIL because the lifecycle and shared writer do not exist.

- [ ] **Step 3: Implement training effects, reconciliation, and the shared writer**

Implement the core block-day calculation without database access:

```python
def planned_block_days(block: TrainingBlock, absence_by_day: Mapping[date, set[str]]) -> list[date]:
    out: list[date] = []
    cursor = block.start_day
    while len(out) < block.planned_attended_days:
        if cursor.weekday() in schedule_store.current().work_weekdays and block.trainee_name not in absence_by_day.get(cursor, set()):
            out.append(cursor)
        cursor += timedelta(days=1)
    return out
```

`effect_for_day` must return trainee-plus-trainer only for the first planned day and trainee-only for later planned days. If the trainee or trainer has a manual conflicting assignment, return a warning and do not replace that manual assignment.

Move the Odoo update and local mirror behavior from `routes/skills.py::_mirror_skill_level` into `skill_levels.set_person_skill_level`. It must call `odoo_client.set_employee_skill_level`, write the local `person_skills` mirror, invalidate roster and HTTP caches, and raise a typed failure when Odoo rejects the change. Make the existing matrix endpoint call this shared function so there is one promotion path.

`reconcile_blocks` processes only workdays before the current plant day. It records every resolved day once in `rotation_training_block_days`; absent days are recorded as `absent` and do not increment progress. When attended count equals the requested count, call the shared skill writer for level 1, set the block `completed`, and do not call the writer again on future reconciliation.

- [ ] **Step 4: Run lifecycle and matrix regressions**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_training.py tests/test_skills_routes.py tests/test_rotation_store.py -v`

Expected: PASS.

- [ ] **Step 5: Commit training lifecycle work**

```bash
git add src/zira_dashboard/rotation_training.py src/zira_dashboard/skill_levels.py src/zira_dashboard/routes/skills.py tests/test_rotation_training.py tests/test_skills_routes.py
git commit -m "feat: add recycled training blocks"
```

### Task 4: Wire recommendations and rotation APIs into Staffing

**Files:**
- Create: `src/zira_dashboard/routes/rotations.py`
- Modify: `src/zira_dashboard/routes/staffing.py:75-95, 150-180, 390-550`
- Modify: `src/zira_dashboard/app.py`
- Create: `tests/test_staffing_rotations.py`

**Interfaces:**
- Consumes `rotation_store`, `rotation_training`, `rotation_suggestions`, and `staffing.Schedule` metadata.
- Produces `POST /api/rotations/preferences`, `POST /api/rotations/training-blocks`, `POST /api/rotations/rebuild`, and schedule-template context keys `recycled_rotation_mode`, `rotation_reasons`, `rotation_warnings`, and `active_training_blocks`.

- [ ] **Step 1: Write route and seeding tests**

```python
def test_blank_staffing_day_uses_normal_recycled_suggestions(client, monkeypatch):
    monkeypatch.setattr(staffing_route.rotation_suggestions, "suggest_recycled_assignments", _suggestion)
    response = client.get("/staffing?day=2026-07-14")
    assert response.status_code == 200
    assert "Normal" in response.text


def test_rebuild_preserves_manual_assignment(client, monkeypatch):
    response = client.post("/api/rotations/rebuild", json={"day": "2026-07-14", "mode": "optimized"})
    assert response.status_code == 200
    assert response.json()["assignments"]["Repair 1"] == ["Manual Person"]


def test_training_block_endpoint_rejects_invalid_trainer(client):
    response = client.post("/api/rotations/training-blocks", json={"trainee": "Alex", "trainer": "Not Green", "group": "Repair", "start_day": "2026-07-14", "workdays": 5})
    assert response.status_code == 422
```

- [ ] **Step 2: Run route tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py -v`

Expected: FAIL because the API router and template context do not exist.

- [ ] **Step 3: Implement router and Staffing orchestration**

In `routes/rotations.py`, validate JSON bodies with these exact fields:

```python
{"person": str, "group": str, "preference": str}
{"trainee": str, "trainer": str, "group": str, "start_day": "YYYY-MM-DD", "workdays": int}
{"day": "YYYY-MM-DD", "mode": "optimized|normal|training"}
```

Return `422` with `{"ok": false, "error": "Day-one trainer must be level 3 for the target skill."}` for validation failures and `200` with the saved/rebuilt model for valid requests.

Update `_smart_defaults_for_day` in `routes/staffing.py` to load preferences, active block effects, and bounded Recycled history, call `suggest_recycled_assignments`, and merge its Recycled output into the existing default map. Retain the current `try/except` fallback that returns stored defaults when recommendation data cannot be read. Call `rotation_training.reconcile_blocks(plant_today())` before calculating active effects. On a blank day, use `normal`; on a saved day, hydrate its stored mode and sources.

Register `rotations.router` in `app.py` beside the existing Staffing and Skills routers. Invalidate the Staffing response cache after every preference, block, or rebuild write.

- [ ] **Step 4: Run route, staffing, and Trim Saw tests**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py tests/test_staffing_trim_saw_defaults.py tests/test_rotation_suggestions.py -v`

Expected: PASS.

- [ ] **Step 5: Commit API and orchestration work**

```bash
git add src/zira_dashboard/routes/rotations.py src/zira_dashboard/routes/staffing.py src/zira_dashboard/app.py tests/test_staffing_rotations.py
git commit -m "feat: schedule recycled rotations"
```

### Task 5: Add Staffing controls, reasons, and safe reset behavior

**Files:**
- Modify: `src/zira_dashboard/templates/staffing.html:1-180, 360-370`
- Modify: `src/zira_dashboard/static/staffing.js:1-110`
- Modify: `src/zira_dashboard/static/staffing.css`
- Modify: `tests/test_staffing_rotations.py`

**Interfaces:**
- Consumes template context from Task 4 and `/api/rotations/rebuild`.
- Produces `recycled_rotation_mode` form state, an accessible three-button mode control, assignment-reason badges, warnings, and a reset action that preserves manual sources.

- [ ] **Step 1: Add static/template contract tests**

```python
def test_staffing_has_rotation_mode_controls_and_reason_data():
    html = (ROOT / "src/zira_dashboard/templates/staffing.html").read_text()
    js = (ROOT / "src/zira_dashboard/static/staffing.js").read_text()
    assert 'data-rotation-mode="optimized"' in html
    assert 'data-rotation-mode="normal"' in html
    assert 'data-rotation-mode="training"' in html
    assert "rotation_reasons" in html
    assert "/api/rotations/rebuild" in js
```

- [ ] **Step 2: Run the contract test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py::test_staffing_has_rotation_mode_controls_and_reason_data -v`

Expected: FAIL because the controls and endpoint reference do not exist.

- [ ] **Step 3: Implement the Staffing controls**

Insert a Recycled-only control near the existing schedule actions:

```html
<fieldset class="rotation-mode" aria-label="Recycled schedule goal">
  <legend>Recycled schedule goal</legend>
  {% for mode, label in [('optimized', 'Optimized'), ('normal', 'Normal'), ('training', 'Training')] %}
  <button type="button" class="rotation-mode-btn {% if recycled_rotation_mode == mode %}active{% endif %}" data-rotation-mode="{{ mode }}" aria-pressed="{{ (recycled_rotation_mode == mode)|tojson }}">{{ label }}</button>
  {% endfor %}
  <p class="hint" id="rotation-mode-help">{{ rotation_mode_help }}</p>
</fieldset>
```

Render `rotation_reasons[wc_name][person_name]` beside generated Recycled pills and render `rotation_warnings` in a `role="alert"` block. Add a `Reset non-manual assignments` button. In JavaScript, post `{day, mode}` to `/api/rotations/rebuild`, replace only unlocked Recycled checkbox selections with returned generated selections, preserve manually locked pills, refresh summaries, and invoke `kickAutosave()` exactly once.

Use `.rotation-mode-btn.active`, `.rotation-reason`, and `.rotation-warning` styles that reuse current panel, accent, and level-color variables; do not introduce a new color system.

- [ ] **Step 4: Run UI contract and Staffing tests**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py tests/test_staffing_view.py -v`

Expected: PASS.

- [ ] **Step 5: Commit Staffing UI work**

```bash
git add src/zira_dashboard/templates/staffing.html src/zira_dashboard/static/staffing.js src/zira_dashboard/static/staffing.css tests/test_staffing_rotations.py
git commit -m "feat: add recycled staffing controls"
```

### Task 6: Add People Matrix preference editing and training-block management

**Files:**
- Modify: `src/zira_dashboard/templates/skills.html`
- Modify: `src/zira_dashboard/static/skills-page.js`
- Modify: `src/zira_dashboard/static/skills.css`
- Modify: `src/zira_dashboard/routes/skills.py`
- Modify: `tests/test_staffing_rotations.py`

**Interfaces:**
- Consumes preference and block routes from Task 4.
- Produces a per-person Recycled Rotation editor and a training-block modal/form available only for level-0 target skills.

- [ ] **Step 1: Write matrix markup and behavior tests**

```python
def test_skills_matrix_exposes_recycled_preference_editor():
    html = (ROOT / "src/zira_dashboard/templates/skills.html").read_text()
    js = (ROOT / "src/zira_dashboard/static/skills-page.js").read_text()
    assert "Recycled Rotation" in html
    assert 'data-rotation-preference' in html
    assert "/api/rotations/preferences" in js
    assert "/api/rotations/training-blocks" in js
```

- [ ] **Step 2: Run the matrix test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py::test_skills_matrix_exposes_recycled_preference_editor -v`

Expected: FAIL because the editor and routes are not referenced.

- [ ] **Step 3: Implement preference and block UI**

Pass `rotation_preferences` and `active_training_blocks` from `staffing_skills` into `skills.html`. Add a compact person-row drawer or modal containing three selects with values `primary`, `regular`, `occasional`, `never`; use `data-person` and `data-group` attributes. Save one changed select with:

```javascript
fetch('/api/rotations/preferences', {
  method: 'POST', headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
  body: JSON.stringify({person, group, preference})
});
```

Add a `Start level-0 training block` form that gathers trainee, group, start date, positive workday count, and green trainer. On a non-200 response, place the returned `error` in a visible live region and retain all form values. On success, close the form and refresh the active-block list. Render pause/end controls only for active blocks and route them through the corresponding lifecycle endpoint.

- [ ] **Step 4: Run matrix and API tests**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py tests/test_skills_routes.py -v`

Expected: PASS.

- [ ] **Step 5: Commit Matrix UI work**

```bash
git add src/zira_dashboard/templates/skills.html src/zira_dashboard/static/skills-page.js src/zira_dashboard/static/skills.css src/zira_dashboard/routes/skills.py tests/test_staffing_rotations.py
git commit -m "feat: manage recycled rotation preferences"
```

### Task 7: Run regression coverage and document the behavior

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-10-recycled-rotations-design.md`
- Modify: `tests/test_rotation_suggestions.py`
- Modify: `tests/test_rotation_training.py`
- Modify: `tests/test_staffing_rotations.py`

**Interfaces:**
- Consumes the complete implementation from Tasks 1–6.
- Produces a documented manager workflow and end-to-end regression evidence.

- [ ] **Step 1: Add end-to-end behavior tests**

```python
def test_manual_repair_assignment_survives_optimized_rebuild(client):
    response = client.post("/api/rotations/rebuild", json={"day": "2026-07-14", "mode": "optimized"})
    assert response.status_code == 200
    assert response.json()["sources"]["Repair 1"]["Manual Person"] == "manual"


def test_completed_training_block_promotes_to_one_and_never_repeats(monkeypatch):
    calls = []
    monkeypatch.setattr(rotation_training.skill_levels, "set_person_skill_level", lambda *args: calls.append(args))
    monkeypatch.setattr(rotation_training.rotation_store, "active_blocks", lambda: [_completed_candidate_block()])
    monkeypatch.setattr(rotation_training.rotation_store, "resolved_days", lambda _block_id: [_attended_day()] * 5)
    monkeypatch.setattr(rotation_training.rotation_store, "mark_completed", lambda _block_id: None)
    assert rotation_training.reconcile_blocks(date(2026, 7, 21)) == [42]
    assert rotation_training.reconcile_blocks(date(2026, 7, 21)) == []
    assert calls == [(17, 9, 1)]
```

- [ ] **Step 2: Run the new tests to verify current behavior**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_suggestions.py tests/test_rotation_training.py tests/test_staffing_rotations.py -v`

Expected: PASS.

- [ ] **Step 3: Document the manager workflow**

Add a concise README section titled `Recycled rotations` with these operational steps:

1. Set each person’s Dismantler, Repair, and Trim Saw preference.
2. Choose Optimized, Normal, or Training before rebuilding a Recycled schedule.
3. Review the reason badges and warnings, then make manual changes when needed.
4. Start a level-0 block only with a green day-one trainer; absences extend it.
5. Verify the automatic level-1 promotion after the final attended day.

Update the design spec only if implementation reveals a deliberately changed behavior; do not edit it for incidental file paths.

- [ ] **Step 4: Run the full relevant regression suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_store.py tests/test_rotation_suggestions.py tests/test_rotation_training.py tests/test_staffing_rotations.py tests/test_staffing_trim_saw_defaults.py tests/test_staffing_view.py tests/test_skills_routes.py -v`

Expected: PASS with no skipped newly added tests.

- [ ] **Step 5: Commit verification and docs**

```bash
git add README.md docs/superpowers/specs/2026-07-10-recycled-rotations-design.md tests/test_rotation_suggestions.py tests/test_rotation_training.py tests/test_staffing_rotations.py
git commit -m "docs: explain recycled rotations"
```

## Plan Self-Review

- **Spec coverage:** Task 1 covers additive storage and manual metadata; Task 2 covers the three modes, level gating, reasons, and fair individual-center rotation; Task 3 covers day-one pairing, absence extension, and automatic promotion; Tasks 4–6 expose these capabilities through Staffing and the People Matrix; Task 7 covers documentation and full regression verification.
- **Placeholder scan:** The plan contains no unresolved markers and every task has concrete files, interfaces, tests, commands, and commit scope.
- **Type consistency:** `rotation_store.TrainingBlock` is consumed by `rotation_training`; `RecycledSuggestion` is consumed by Staffing routes and templates; `Schedule.rotation_mode` and `Schedule.assignment_sources` persist the mode/reset contract used by the browser.
