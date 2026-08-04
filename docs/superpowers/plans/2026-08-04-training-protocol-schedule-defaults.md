# Training Protocol Schedule Defaults Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a Recycled training protocol schedule its trainee automatically, show the trainee in the selected work-center picker, and keep day-one pairing safe at a one-person normal maximum.

**Architecture:** Keep `rotation_suggestions` authoritative for safe training reservations. Pass the engine's temporary trainer capacity exception to rebuild validation, reuse its applied training assignments when seeding a fresh future draft, and pass active trainee names to the pure Staffing view model for picker visibility.

**Tech Stack:** Python 3, FastAPI, pytest, Jinja templates, vanilla JavaScript, CSS.

## Global Constraints

- A training protocol is temporary; never write its people to permanent Default People settings.
- Only the day-one generated trainer may exceed a normal work-center maximum, and only as an engine-produced training exception.
- Preserve existing level-1 promotion, absence extension, manual-conflict, and generated-reservation attendance rules.
- Add a short, child-friendly CHANGELOG entry before pushing the implementation to `origin/main`.

---

### Task 1: Preserve the supervised day-one pair through the Recycled solver and validation

**Files:**

- Modify: `src/zira_dashboard/rotation_suggestions.py`
- Modify: `src/zira_dashboard/routes/rotations.py`
- Test: `tests/test_rotation_suggestions.py`
- Test: `tests/test_staffing_rotations.py`

**Interfaces:**

- Consumes: `BlockEffect.locked_work_centers` and `BlockEffect.temporary_extra_work_centers`.
- Produces: `RecycledSuggestion.temporary_training_extras: dict[str, tuple[str, ...]]`, containing only day-one trainer names that may temporarily exceed a normal center maximum.

- [ ] **Step 1: Write the failing solver and validator tests**

```python
def test_exact_training_pair_can_exceed_normal_center_maximum():
    out = suggest_recycled_assignments(
        ..., center_capacities={"Master Recycler": 1},
        block_effects=[BlockEffect(
            locked_work_centers={"Master Recycler": ["Adrian"]},
            temporary_extra_work_centers={"Master Recycler": ["Alejandro"]},
        )],
    )
    assert out.assignments["Master Recycler"] == ["Adrian", "Alejandro"]
    assert out.temporary_training_extras == {"Master Recycler": ("Alejandro",)}
```

```python
def test_rebuild_validation_accepts_only_the_explicit_training_extra():
    issues = rotations._validate_complete_rebuild(
        ..., proposed_assignments={"Master Recycler": ["Adrian", "Alejandro"]},
        proposed_sources={"Master Recycler": {"Adrian": "generated", "Alejandro": "generated"}},
        temporary_training_extras={"Master Recycler": ("Alejandro",)},
    )
    assert not [issue for issue in issues if issue.code == "center_capacity_exceeded"]
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_suggestions.py tests/test_staffing_rotations.py -q`

Expected: FAIL on the new supervised-pair assertions.

- [ ] **Step 3: Add the narrow engine and validator support**

```python
@dataclass(frozen=True)
class RecycledSuggestion:
    # Existing fields remain unchanged.
    temporary_training_extras: dict[str, tuple[str, ...]] = field(default_factory=dict)
```

Record exact day-one extra names while applying `temporary_extra_work_centers`. Require the normal maximum for locked trainee slots, then allow only those recorded extra trainer names to occupy one temporary slot. Pass the map to `_validate_complete_rebuild`, which raises the maximum only by the number of generated names in that map.

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_suggestions.py tests/test_staffing_rotations.py -q`

Expected: PASS.

### Task 2: Seed training reservations into fresh protocol-day drafts

**Files:**

- Modify: `src/zira_dashboard/routes/staffing.py`
- Test: `tests/test_staffing_rotations.py`

**Interfaces:**

- Consumes: `_recycled_suggestion_for_day(...)` and `RecycledSuggestion.reason_codes`.
- Produces: `_apply_training_reservations_to_defaults(day, roster, assignments, sources, time_off_entries) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]`.

- [ ] **Step 1: Write the failing overlay test**

```python
def test_training_reservations_replace_only_defaults_at_their_exact_center(monkeypatch):
    monkeypatch.setattr(staffing_route, "_recycled_suggestion_for_day", lambda *args, **kwargs: suggestion)
    assignments, sources = staffing_route._apply_training_reservations_to_defaults(
        TARGET_DAY, roster, {"Master Recycler": ["Default"]},
        {"Master Recycler": {"Default": "default"}}, [],
    )
    assert assignments["Master Recycler"] == ["Adrian", "Alejandro"]
    assert sources["Master Recycler"] == {"Adrian": "generated", "Alejandro": "generated"}
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py -q`

Expected: FAIL with an attribute error for `_apply_training_reservations_to_defaults`.

- [ ] **Step 3: Implement the default-draft overlay and call it from `_seed_new_future_draft`**

The helper retains only engine assignments whose reason code is `training_block`, removes only default-source occupants from those exact target centers, removes each training person from their prior default center, and writes `generated` sources. Call it after `defaults_only_schedule` for new non-optional working-day drafts. If the suggestion is unavailable or carries no applied reservation, return the original defaults unchanged.

- [ ] **Step 4: Run the focused test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py -q`

Expected: PASS.

### Task 3: Make active level-0 trainees visible only at their training center and rebuild on protocol creation

**Files:**

- Modify: `src/zira_dashboard/routes/staffing.py`
- Modify: `src/zira_dashboard/staffing_view.py`
- Modify: `src/zira_dashboard/templates/staffing.html`
- Modify: `src/zira_dashboard/static/staffing.css`
- Modify: `src/zira_dashboard/static/staffing.js`
- Test: `tests/test_staffing_view.py`
- Test: `tests/test_staffing_rotations.py`

**Interfaces:**

- Consumes: `_block_effects_for_day(day, time_off_entries)`.
- Produces: `training_reserved: bool` on a picker-pool row and `window.rebuildRotationForTraining(): Promise<boolean>` for protocol setup.

- [ ] **Step 1: Write failing unit and static-contract tests**

```python
def test_training_reservation_marks_only_the_target_level_zero_picker_row(patch_wcs):
    model = staffing_view.build_staffing_bays(
        ..., training_reservations_by_center={"Master Recycler": {"Adrian"}},
    )
    assert master_pool["Adrian"]["training_reserved"] is True
    assert repair_pool["Adrian"]["training_reserved"] is False
```

```python
assert "training-reserved" in html
assert ".dd-item.untrained.training-reserved { display: flex; }" in css
assert "window.rebuildRotationForTraining" in js
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_view.py tests/test_staffing_rotations.py -q`

Expected: FAIL on the new training-reservation assertions.

- [ ] **Step 3: Implement picker visibility and post-create rebuild**

Build the exact-center trainee-name map from active day effects in the Staffing route and pass it to `build_staffing_bays`. Mark matching rows in the template and override only their level-0 hidden CSS rule. Expose the existing rotation `rebuild(currentMode())` through `window.rebuildRotationForTraining`; after a successful protocol POST, await it, display its error in the protocol dialog if it fails, then close the dialog and reload the page on success.

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_view.py tests/test_staffing_rotations.py -q`

Expected: PASS.

### Task 4: Verify the complete training flow and publish it

**Files:**

- Modify: `CHANGELOG.md`
- Test: `tests/test_rotation_suggestions.py`
- Test: `tests/test_staffing_rotations.py`
- Test: `tests/test_staffing_view.py`

- [ ] **Step 1: Add the user-facing patch note**

Add a new `2026-08-04` entry explaining in simple words that starting a training plan now puts the learner in the right work center, keeps the trainer with them on day one, and makes the learner selectable during training.

- [ ] **Step 2: Run focused regression coverage**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_suggestions.py tests/test_staffing_rotations.py tests/test_staffing_view.py -q`

Expected: PASS.

- [ ] **Step 3: Run the full test suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`

Expected: PASS.

- [ ] **Step 4: Commit and push the implementation**

Run: `git add CHANGELOG.md src/zira_dashboard/rotation_suggestions.py src/zira_dashboard/routes/rotations.py src/zira_dashboard/routes/staffing.py src/zira_dashboard/staffing_view.py src/zira_dashboard/templates/staffing.html src/zira_dashboard/static/staffing.css src/zira_dashboard/static/staffing.js tests/test_rotation_suggestions.py tests/test_staffing_rotations.py tests/test_staffing_view.py && git commit -m "fix: schedule active training protocols" && git push origin main`
