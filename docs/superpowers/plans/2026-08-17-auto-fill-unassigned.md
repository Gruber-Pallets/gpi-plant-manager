# Auto Fill Unassigned Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Staffing schedule-goal click keep everyone already on a work center and only seat unassigned people into remaining Auto-on capacity.

**Architecture:** Stop stripping Auto-on assignments before the solver. Keep every seated name from the current board, then run the existing best-effort fill on leftover people using remaining maximum seats. Preserve prior assignment sources for people who did not move; mark only new Auto seats `generated`. Goal-button rebuilds pass `minimum_only=False` so leftovers can fill open max seats, not just remaining minimums.

**Tech Stack:** Python 3.12, FastAPI, pytest, Jinja2, browser JavaScript.

## Global Constraints

- Follow `docs/superpowers/specs/2026-08-17-auto-fill-unassigned-design.md`.
- Everyone already on the day's assignment map stays on that same work center.
- Auto may add people only from the unassigned pool into remaining Auto-on maximum capacity.
- Auto never turns a work center on or off.
- Leftover people with no safe opening stay unassigned; that partial fill is saved.
- Do not stamp seated people `manual`. Keep their existing `default` / `generated` / `manual` source.
- **Reset to defaults** and **Clear schedule** stay unchanged.
- Do not add, commit, or revert unrelated local changes in Settings files, `.cursorignore`, `.python-version`, or `uv.lock`.
- Work directly on `main`.

---

## File Structure

- `src/zira_dashboard/rotation_suggestions.py` — keep every seated Auto-on name from `base_assignments`; leftover fill still uses remaining capacity.
- `src/zira_dashboard/routes/staffing.py` — pass the current board into the solver; weekday goal rebuilds fill remaining max seats; update help copy.
- `src/zira_dashboard/routes/rotations.py` — preserve existing sources, fill-only validation, Saturday uses the same keep-then-fill board.
- `src/zira_dashboard/static/staffing.js` — match the new help lines.
- `CLAUDE.md` — replace the complete-reshuffle invariants.
- `CHANGELOG.md` — kid-friendly What's New note.
- `tests/test_rotation_suggestions.py` — pure keep-then-fill cases.
- `tests/test_staffing_rotations.py` — rebuild route cases.
- `tests/test_staffing_static.py` — help-copy contract.

---

### Task 1: Keep seated people in the pure engine

**Files:**
- Modify: `src/zira_dashboard/rotation_suggestions.py` (`suggest_recycled_assignments` base-assignment copy loop)
- Test: `tests/test_rotation_suggestions.py`

**Interfaces:**
- Consumes: existing `suggest_recycled_assignments(..., base_assignments=..., exact_defaults=..., block_effects=..., minimum_only=False)`.
- Produces: seated names from `base_assignments` remain in `result.assignments` at the same centers; leftover people may be added into remaining maximum capacity; a seated trainer/trainee is not relocated by a training-block effect.

- [ ] **Step 1: Write the failing keep-then-fill tests**

Add these tests to `tests/test_rotation_suggestions.py` near `test_manual_lock_survives_rebuild_and_engine_fills_around_it`. Reuse `_person` and `_BlockEffect` already in that file.

```python
def test_existing_assignments_stay_and_leftover_fills_remaining_capacity():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14),
        mode="normal",
        roster=[
            _person("Seated A", 3),
            _person("Seated B", 3),
            _person("Leftover", 3),
        ],
        base_assignments={"Repair 1": ["Seated A", "Seated B"]},
        group_locations={"Repair": ("Repair 1",)},
        group_required_skills={"Repair": ("Repair",)},
        center_minimums={"Repair 1": 2},
        center_capacities={"Repair 1": 3},
        runnable_centers={"Repair 1"},
        minimum_only=False,
    )

    assert out.assignments["Repair 1"] == ["Seated A", "Seated B", "Leftover"]


def test_existing_assignment_is_not_pulled_to_exact_default():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14),
        mode="normal",
        roster=[_person("Default Person", 3)],
        base_assignments={"Repair 2": ["Default Person"]},
        group_locations={"Repair": ("Repair 1", "Repair 2")},
        group_required_skills={"Repair": ("Repair",)},
        exact_defaults={"Repair 1": ("Default Person",)},
        center_minimums={"Repair 1": 1, "Repair 2": 1},
        center_capacities={"Repair 1": 1, "Repair 2": 1},
        runnable_centers={"Repair 1", "Repair 2"},
        minimum_only=False,
    )

    assert out.assignments["Repair 2"] == ["Default Person"]
    assert "Default Person" not in out.assignments.get("Repair 1", [])


def test_leftover_uses_other_center_when_default_is_full():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14),
        mode="normal",
        roster=[_person("Seated", 3), _person("Default Leftover", 3)],
        base_assignments={"Repair 1": ["Seated"]},
        group_locations={"Repair": ("Repair 1", "Repair 2")},
        group_required_skills={"Repair": ("Repair",)},
        exact_defaults={"Repair 1": ("Default Leftover",)},
        center_minimums={"Repair 1": 1, "Repair 2": 1},
        center_capacities={"Repair 1": 1, "Repair 2": 1},
        runnable_centers={"Repair 1", "Repair 2"},
        minimum_only=False,
    )

    assert out.assignments["Repair 1"] == ["Seated"]
    assert out.assignments["Repair 2"] == ["Default Leftover"]


def test_unqualified_existing_assignment_stays():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14),
        mode="normal",
        roster=[
            staffing.Person(name="Unskilled", skills={"Repair": 0}),
            _person("Leftover", 3),
        ],
        base_assignments={"Repair 1": ["Unskilled"]},
        group_locations={"Repair": ("Repair 1",)},
        group_required_skills={"Repair": ("Repair",)},
        center_minimums={"Repair 1": 1},
        center_capacities={"Repair 1": 2},
        runnable_centers={"Repair 1"},
        minimum_only=False,
    )

    assert out.assignments["Repair 1"][0] == "Unskilled"
    assert "Leftover" in out.assignments["Repair 1"]


def test_training_block_does_not_relocate_seated_trainer():
    effect = _BlockEffect(
        locked_work_centers={"Repair 2": ["Trainee"]},
        temporary_extra_work_centers={"Repair 2": ["Trainer"]},
    )
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14),
        mode="normal",
        roster=[
            staffing.Person(name="Trainee", skills={"Repair": 0}),
            _person("Trainer", 3),
        ],
        base_assignments={"Repair 1": ["Trainer"]},
        group_locations={"Repair": ("Repair 1", "Repair 2")},
        group_required_skills={"Repair": ("Repair",)},
        center_minimums={"Repair 1": 1, "Repair 2": 1},
        center_capacities={"Repair 1": 2, "Repair 2": 2},
        runnable_centers={"Repair 1", "Repair 2"},
        block_effects=[effect],
        minimum_only=False,
    )

    assert out.assignments["Repair 1"] == ["Trainer"]
    assert "Trainer" not in out.assignments.get("Repair 2", [])


def test_existing_trim_saw_pair_is_not_relocated():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14),
        mode="normal",
        roster=[
            staffing.Person(name="Saw One", skills={"Trim Saw": 3}),
            staffing.Person(name="Saw Two", skills={"Trim Saw": 3}),
            staffing.Person(name="Leftover", skills={"Repair": 3, "Trim Saw": 3}),
        ],
        base_assignments={"Trim Saw 1": ["Saw One", "Saw Two"]},
        group_locations={
            "Trim Saw": ("Trim Saw 1", "Trim Saw 2"),
            "Repair": ("Repair 1",),
        },
        group_required_skills={"Trim Saw": ("Trim Saw",), "Repair": ("Repair",)},
        center_minimums={"Trim Saw 1": 2, "Trim Saw 2": 2, "Repair 1": 1},
        center_capacities={"Trim Saw 1": 2, "Trim Saw 2": 2, "Repair 1": 1},
        runnable_centers={"Trim Saw 1", "Trim Saw 2", "Repair 1"},
        minimum_only=False,
    )

    assert out.assignments["Trim Saw 1"] == ["Saw One", "Saw Two"]
    assert "Saw One" not in out.assignments.get("Trim Saw 2", [])
    assert "Saw Two" not in out.assignments.get("Trim Saw 2", [])
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_suggestions.py::test_existing_assignments_stay_and_leftover_fills_remaining_capacity tests/test_rotation_suggestions.py::test_existing_assignment_is_not_pulled_to_exact_default tests/test_rotation_suggestions.py::test_leftover_uses_other_center_when_default_is_full tests/test_rotation_suggestions.py::test_unqualified_existing_assignment_stays tests/test_rotation_suggestions.py::test_training_block_does_not_relocate_seated_trainer tests/test_rotation_suggestions.py::test_existing_trim_saw_pair_is_not_relocated -v`

Expected: FAIL because Auto-on `base_assignments` still drop unqualified/over-capacity names, and seated people can be treated as free solver inventory.

- [ ] **Step 3: Keep every seated Auto-on name**

In `suggest_recycled_assignments`, replace the Auto-on branch of the `base_assignments` copy loop so it keeps every non-blank seated name. Skip only a second copy of a person already copied (first center wins). Do not drop for qualification, roster membership, or capacity. Invalid **locks** later in the function stay on the existing lock sanitizer.

```python
    for center, names in (base_assignments or {}).items():
        if center not in allowed_centers:
            copied = [str(name) for name in (names or []) if str(name or "").strip()]
            assignments[center] = copied
            assigned.update(copied)
            continue
        for raw_name in names or ():
            name = str(raw_name or "").strip()
            if not name or name in assigned:
                continue
            assignments.setdefault(center, []).append(name)
            assigned.add(name)
```

Leave the rest of leftover ranking, `minimum_only` remaining-capacity math, and training-block `name in assigned` skips as they are. Training already refuses to relocate a person who is already seated.

- [ ] **Step 4: Re-run the new tests and the engine file**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_suggestions.py -q`

Expected: PASS. If an older engine test assumed Auto-on `base_assignments` were stripped or unqualified seats were deleted, update that test to the keep-then-fill rule rather than restoring the strip.

- [ ] **Step 5: Commit**

```bash
git add tests/test_rotation_suggestions.py src/zira_dashboard/rotation_suggestions.py
git commit -m "$(cat <<'EOF'
feat: keep seated people when Auto fills leftovers

EOF
)"
```

---

### Task 2: Wire rebuild to fill leftovers without moving the board

**Files:**
- Modify: `src/zira_dashboard/routes/staffing.py` (`_recycled_suggestion_for_day`, `staffing_page` recycled context)
- Modify: `src/zira_dashboard/routes/rotations.py` (`_rebuild_with_state`, `_build_assignment_sources`, `_validate_complete_rebuild`)
- Test: `tests/test_staffing_rotations.py`

**Interfaces:**
- Consumes: Task 1 keep-then-fill engine; current `staffing.Schedule.assignments` and `assignment_sources`.
- Produces: `POST /api/rotations/rebuild` saves the current seats plus leftover fills; existing sources survive; only new seats are `generated`; `minimum_only=False` for weekday and Saturday goal clicks; hard validation rejects only unsafe **new** seats.

- [ ] **Step 1: Write the failing rebuild tests**

In `tests/test_staffing_rotations.py`:

1. Change `test_goal_button_rebuild_still_staffs_to_minimum_crew` so the captured kwargs assert `minimum_only is False`. Rename it to `test_goal_button_rebuild_fills_remaining_capacity`.
2. Replace `test_rebuild_drops_stale_generated_source` with a keep-then-fill case:

```python
def test_rebuild_keeps_generated_assignment_and_fills_unassigned(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    _stub_recommendation_inputs(monkeypatch)

    saved: list = []
    sched = staffing.Schedule(
        day=TARGET_DAY,
        assignments={"Repair 1": ["Seated"]},
        assignment_sources={"Repair 1": {"Seated": "generated"}},
        auto_enabled_work_centers=["Repair 1"],
    )
    monkeypatch.setattr(
        rotations.staffing,
        "load_roster",
        lambda: [_person("Seated", 3), _person("Leftover", 3)],
    )
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda d: sched)
    monkeypatch.setattr(rotations.staffing, "save_schedule", lambda s: saved.append(s))
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)

    resp = client.post("/api/rotations/rebuild", json={"day": "2026-07-14", "mode": "normal"})

    assert resp.status_code == 200
    body = resp.json()
    assert "Seated" in body["assignments"]["Repair 1"]
    assert body["sources"]["Repair 1"]["Seated"] == "generated"
    assert "Leftover" in {n for names in body["assignments"].values() for n in names}
    assert saved[-1].assignment_sources["Repair 1"]["Seated"] == "generated"
```

3. Add a source-preservation case for a `default` seat that Auto must not relabel:

```python
def test_rebuild_preserves_default_source_on_seated_person(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    _stub_recommendation_inputs(monkeypatch)

    saved: list = []
    sched = staffing.Schedule(
        day=TARGET_DAY,
        assignments={"Repair 1": ["Default Seat"]},
        assignment_sources={"Repair 1": {"Default Seat": "default"}},
        auto_enabled_work_centers=["Repair 1"],
    )
    monkeypatch.setattr(rotations.staffing, "load_roster", lambda: [_person("Default Seat", 3)])
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda d: sched)
    monkeypatch.setattr(rotations.staffing, "save_schedule", lambda s: saved.append(s))
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)

    resp = client.post("/api/rotations/rebuild", json={"day": "2026-07-14", "mode": "optimized"})

    assert resp.status_code == 200
    assert resp.json()["assignments"]["Repair 1"] == ["Default Seat"]
    assert resp.json()["sources"]["Repair 1"]["Default Seat"] == "default"
    assert saved[-1].assignment_sources["Repair 1"]["Default Seat"] == "default"
```

4. Update `test_rebuild_generates_and_reports_reasons` if it still expects only one of two empty-board people to be placed. An empty board filling remaining max on Repair 1 (stub max 3) should place both greens.

- [ ] **Step 2: Run the rebuild tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py::test_goal_button_rebuild_fills_remaining_capacity tests/test_staffing_rotations.py::test_rebuild_keeps_generated_assignment_and_fills_unassigned tests/test_staffing_rotations.py::test_rebuild_preserves_default_source_on_seated_person tests/test_staffing_rotations.py::test_rebuild_generates_and_reports_reasons -v`

Expected: FAIL because `_recycled_suggestion_for_day` still strips Auto-on assignments, weekday rebuild still passes `minimum_only=True`, and `_build_assignment_sources` still replaces managed-center sources with the engine map.

- [ ] **Step 3: Pass the current board into the solver**

In `_recycled_suggestion_for_day`, stop filtering through `_auto_solver_base_assignments`. Pass the current assignment map through:

```python
        suggestion = rotation_suggestions.suggest_recycled_assignments(
            day=d,
            mode=mode,
            roster=available,
            preferences=preferences,
            base_assignments=base_assignments,
            group_locations=group_locations,
            group_required_skills=group_required_skills,
            history=history,
            locked_assignments=scoped_locks,
            block_effects=block_effects,
            training_cap=_RECYCLED_TRAINING_CAP,
            center_minimums=resolved_minimums,
            center_capacities=resolved_capacities,
            runnable_centers=enabled,
            exact_defaults=exact_defaults,
            group_defaults=group_defaults,
            user_group_centers=user_group_centers,
            minimum_only=minimum_only,
        )
```

In `staffing_page`, pass `sched.assignments` into `_recycled_context_for_day` instead of `_auto_solver_base_assignments(...)`. Leave `_auto_solver_base_assignments` in place for its unit test; rebuild and page preview no longer call it.

- [ ] **Step 4: Fill remaining max seats and keep existing sources**

In `_rebuild_with_state`:

- Keep weekday locks as current **manual-only** locks (`include_saved_defaults=False`). Do not pass every seated person as a lock.
- Change Saturday locks to the same manual-only map so Saturday defaults are not restamped `manual`:

```python
            manual_locks = staffing_route._manual_locks_from_sources(
                existing_sources,
                base_assignments,
            )
            manual_locks = {
                wc: names for wc, names in manual_locks.items() if wc in set(enabled_centers)
            }
```

Use that for both weekday and Saturday. Remove the `_saturday_protected_locks(...)` call from this path. If that helper has no remaining callers, delete it.

Seated people stay because they are in `base_assignments`, not because they are locks. Update `test_holiday_auto_uses_only_effective_volunteers_and_optional_locks` if it still expects Saturday-only lock filtering; leftover Auto roster filtering stays on the optional-day roster.

- Pass `minimum_only=False` for every goal-button rebuild, including weekdays:

```python
            minimum_only=False,
```

- Pass `previous_assignments=base_assignments` into `_validate_complete_rebuild`.

Replace `_build_assignment_sources` so seated people keep their prior source and only engine-reported seats overlay:

```python
def _build_assignment_sources(existing_sources, suggestion) -> dict[str, dict[str, str]]:
    """Keep prior sources for seated people; overlay new engine sources."""
    managed = {c for centers in suggestion.group_locations.values() for c in centers}
    new_sources: dict[str, dict[str, str]] = {}
    for wc, sources in (existing_sources or {}).items():
        if wc not in managed:
            new_sources[wc] = dict(sources)
    assigned_names = {
        wc: set(str(name) for name in names or ())
        for wc, names in suggestion.assignments.items()
    }
    for wc, names in suggestion.assignments.items():
        if wc not in managed:
            continue
        previous = dict((existing_sources or {}).get(wc) or {})
        kept = {
            name: source
            for name, source in previous.items()
            if name in assigned_names.get(wc, set())
        }
        kept.update(dict(suggestion.sources.get(wc) or {}))
        kept = {
            name: source
            for name, source in kept.items()
            if name in assigned_names.get(wc, set())
        }
        if kept:
            new_sources[wc] = kept
    return new_sources
```

- [ ] **Step 5: Reject only unsafe new seats**

Extend `_validate_complete_rebuild` with `previous_assignments: Mapping[str, Sequence[str]] | None = None`.

Build `previous_pairs` and `previous_centers_by_person` from that map. Then:

- `person_assigned_multiple_centers`: add the issue only when the person's proposed center set is not a subset of their previous centers.
- `center_capacity_exceeded`: add the issue only when proposed headcount exceeds both configured max **and** the previous headcount for that center.
- `generated_assignment_unqualified` and `generated_assignment_center_disabled`: add the issue only when that `(center, name)` pair was not already seated there.

Unplaced leftovers, unmet minimums, and unused defaults stay non-hard.

- [ ] **Step 6: Run focused rebuild tests**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py tests/test_rotation_suggestions.py -q`

Expected: PASS. Update any remaining rebuild test that still asserts weekday `minimum_only is True` or that generated seats are wiped.

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/routes/staffing.py src/zira_dashboard/routes/rotations.py tests/test_staffing_rotations.py
git commit -m "$(cat <<'EOF'
feat: Auto fills unassigned people without moving the board

EOF
)"
```

---

### Task 3: Help copy, scheduler invariants, and What's New

**Files:**
- Modify: `src/zira_dashboard/routes/staffing.py` (`_ROTATION_MODE_HELP`)
- Modify: `src/zira_dashboard/static/staffing.js` (`HELP`)
- Modify: `tests/test_staffing_rotations.py` (`test_staffing_has_rotation_mode_controls_without_automated_person_notes`)
- Modify: `CLAUDE.md` (Global Auto scheduling bullets)
- Modify: `CHANGELOG.md` (today's date)

**Interfaces:**
- Consumes: Task 2 fill-only rebuild behavior.
- Produces: matching help strings in Python and JS; CLAUDE.md invariants that match the spec; a kid-friendly What's New note.

- [ ] **Step 1: Write the failing help-copy assertions**

In `test_staffing_has_rotation_mode_controls_without_automated_person_notes`, import `staffing as staffing_route` if needed and add:

```python
    from zira_dashboard.routes import staffing as staffing_route

    assert all(
        text.startswith("Auto fills unassigned people without moving the current board.")
        for text in staffing_route._ROTATION_MODE_HELP.values()
    )
    assert "Auto fills unassigned people without moving the current board" in js
```

Do not assert that sentence in `staffing.html`; the template renders `{{ rotation_mode_help }}` from Python.

- [ ] **Step 2: Run the static contract test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py::test_staffing_has_rotation_mode_controls_without_automated_person_notes -v`

Expected: FAIL because the help lines still describe full-board coverage only.

- [ ] **Step 3: Update help copy and invariants**

Set:

```python
_ROTATION_MODE_HELP = {
    "optimized": (
        "Auto fills unassigned people without moving the current board. "
        "Optimized favors the strongest leftover coverage."
    ),
    "normal": (
        "Auto fills unassigned people without moving the current board. "
        "Normal balances leftover coverage, preferences, and fair rotation."
    ),
    "training": (
        "Auto fills unassigned people without moving the current board. "
        "Training develops leftover level-1/2 operators while protecting coverage."
    ),
}
```

Mirror those three strings in `staffing.js` `HELP`.

In `CLAUDE.md` Global Auto scheduling, replace the complete-reshuffle bullets with:

- A goal-button rebuild keeps every currently seated person and only assigns unassigned available people into remaining Auto-on maximum capacity. Full-day absences are not available for new Auto seats.
- Leftover people with no safe opening stay unassigned. That fill is saved. Hard failures remain malformed requests, missing Auto configuration, zero Auto-on centers, or an unsafe **new** seat.
- Automatic assignments may use only work centers whose Auto checkbox is enabled; the solver never enables or populates another center on its own.
- Exact/group defaults never pull a seated person to another center. For leftovers, a default is used when that center still has room; otherwise Auto may use another safe open Auto center.
- The non-reset goal button is the only path that runs the rotation engine (`minimum_only=False`, fills remaining maximum seats around the current board). Seed and reset stay in lockstep — both produce `defaults_only_schedule`'s output.

Also update the key invariant that says only `manual` locks survive and generated entries are recomputed: seated people of every source stay put.

In `CHANGELOG.md`, under `## 2026-08-17`, add a Features entry **above** the existing fixes:

```markdown
### Auto leaves scheduled people in place

#### Features

- **The Auto buttons now only fill empty spots.** People already on a work center stay there. People with no work center yet are placed in leftover open seats. Reset to defaults and Clear schedule are still the ways to start the day over.
```

- [ ] **Step 4: Run focused tests**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py::test_staffing_has_rotation_mode_controls_without_automated_person_notes tests/test_staffing_rotations.py tests/test_rotation_suggestions.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/routes/staffing.py src/zira_dashboard/static/staffing.js tests/test_staffing_rotations.py CLAUDE.md CHANGELOG.md
git commit -m "$(cat <<'EOF'
docs: explain Auto fill-only staffing

EOF
)"
```

---
