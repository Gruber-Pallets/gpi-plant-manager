# Training Sidebar Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put Recycled training protocol management in the Staffing right rail under Notes—progress, inline edit, create, early Complete with promotion, and End—without the header modal.

**Architecture:** Keep day-scoped `active_blocks_for_day` for Auto/seeding. Add a plant-wide manageable list (`active` + `paused`) with attended counts for the sidebar. Extend `rotation_store` with update + early-completion claim helpers; put promote-and-finalize in `rotation_training.complete_block_now`; expose update/complete JSON routes; render a compact Training panel in `staffing.js` and delete the modal.

**Tech Stack:** Python 3, FastAPI, pytest, Jinja, vanilla JavaScript/CSS, PostgreSQL.

## Global Constraints

- Day-scoped schedule reservations stay on `active_blocks_for_day` (active only).
- Sidebar lists every plant-wide `active` and `paused` protocol.
- **Complete** promotes to level 1 immediately even when attended days are fewer than planned; does not invent attended days.
- **End** never promotes.
- Edit may change trainer, work center, start day, planned days; planned days ≥ attended count; skill rules match create.
- Posted / read-only Staffing: list + progress only; no mutations.
- Add a short, child-friendly CHANGELOG entry before pushing to `origin/main`.
- Spec: `docs/superpowers/specs/2026-08-06-training-sidebar-panel-design.md`.

---

### Task 1: Persist manageable list, updates, and early-completion claims

**Files:**

- Modify: `src/zira_dashboard/rotation_store.py`
- Test: `tests/test_rotation_store.py`

**Interfaces:**

- Consumes: existing `TrainingBlock`, `resolved_days`, `validate_block`, `_skill_ids_for`, `staffing.location_by_name` / `required_skills_for`.
- Produces:
  - `manageable_blocks() -> list[TrainingBlock]` — every `active` and `paused` block, ordered by `start_day`, `id`.
  - `get_block(block_id: int) -> TrainingBlock | None`
  - `attended_day_count(block_id: int) -> int`
  - `update_block(block_id, *, trainer_id, work_center, start_day, planned_attended_days) -> TrainingBlock`
  - `claim_early_completion(block_id: int) -> str | None` — atomically sets `completing` from `active` or `paused`; returns prior status or `None` if ineligible.
  - `release_early_completion_claim(block_id: int, prior_status: str) -> None` — restores `active` or `paused` after a failed promotion.
  - Extend `mark_completed` to finalize `completing` (already does) after early claim from paused.

- [ ] **Step 1: Write the failing store tests**

```python
def test_manageable_blocks_includes_active_and_paused(monkeypatch):
    rows = [
        {"id": 1, "trainee_name": "A", "trainer_name": "T", "skill": "Repair",
         "start_day": date(2026, 8, 1), "planned_attended_days": 5, "status": "active",
         "trainee_id": 1, "skill_id": 9, "work_center": "Repair 1", "skill_ids": [9]},
        {"id": 2, "trainee_name": "B", "trainer_name": "T", "skill": "Repair",
         "start_day": date(2026, 8, 2), "planned_attended_days": 3, "status": "paused",
         "trainee_id": 2, "skill_id": 9, "work_center": "Repair 2", "skill_ids": [9]},
    ]
    monkeypatch.setattr(rotation_store.db, "query", lambda *a, **k: rows)
    out = rotation_store.manageable_blocks()
    assert [b.id for b in out] == [1, 2]
    assert {b.status for b in out} == {"active", "paused"}


def test_update_block_rejects_planned_days_below_attended(monkeypatch):
    existing = rotation_store.TrainingBlock(
        id=1, trainee_name="A", trainer_name="T", skill="Repair",
        start_day=date(2026, 8, 1), planned_attended_days=5, status="active",
        trainee_id=1, skill_id=9, work_center="Repair 1", skill_ids=(9,),
    )
    monkeypatch.setattr(rotation_store, "get_block", lambda _id: existing)
    monkeypatch.setattr(rotation_store, "attended_day_count", lambda _id: 3)
    with pytest.raises(rotation_store.InvalidTrainingBlock, match="attended"):
        rotation_store.update_block(
            1, trainer_id=2, work_center="Repair 1",
            start_day=date(2026, 8, 1), planned_attended_days=2,
        )


def test_claim_early_completion_accepts_paused(monkeypatch):
    monkeypatch.setattr(
        rotation_store.db, "query",
        lambda sql, params=None: [{"id": params[0], "status": "paused"}]
        if "RETURNING" in sql else [],
    )
    assert rotation_store.claim_early_completion(7) == "paused"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_store.py -k 'manageable_blocks or update_block or claim_early' -q`

Expected: FAIL because the helpers are missing.

- [ ] **Step 3: Implement the store helpers**

```python
def manageable_blocks() -> list[TrainingBlock]:
    rows = db.query(
        "SELECT ... FROM rotation_training_blocks b ... "
        "WHERE b.status IN ('active', 'paused') "
        "ORDER BY b.start_day, b.id"
    )
    return [_block_from_row(row) for row in rows]


def update_block(block_id, *, trainer_id, work_center, start_day, planned_attended_days):
    block = get_block(block_id)
    if block is None or block.status not in ("active", "paused"):
        raise InvalidTrainingBlock("Training is not editable.")
    attended = attended_day_count(block_id)
    if planned_attended_days < attended:
        raise InvalidTrainingBlock(
            f"Planned days cannot be below attended days ({attended})."
        )
    location = staffing.location_by_name(work_center)
    if location is None:
        raise InvalidTrainingBlock(f"Unknown work center: {work_center!r}.")
    skill_ids = _skill_ids_for(staffing.required_skills_for(location))
    # Validate trainee level 0 and trainer level 3 for every skill_id (same as create_block).
    # UPDATE trainer_id, work_center, skill_id, skill_ids, start_day, planned_attended_days
    # WHERE id = block_id AND status IN ('active', 'paused') RETURNING ...
    return _block_from_row(rows[0])


def claim_early_completion(block_id: int) -> str | None:
    rows = db.query(
        "UPDATE rotation_training_blocks SET status = 'completing' "
        "WHERE id = %s AND status IN ('active', 'paused') "
        "RETURNING id, status",  # NOTE: capture prior status before update —
        # use a CTE or SELECT FOR UPDATE pattern that returns old status.
        (block_id,),
    )
    ...
```

Use a CTE that reads the prior status, then sets `completing`, returning the prior status string. `release_early_completion_claim(block_id, prior_status)` only restores when `prior_status in ("active", "paused")`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_store.py -k 'manageable_blocks or update_block or claim_early' -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/rotation_store.py tests/test_rotation_store.py
git commit -m "feat: persist training sidebar update and early-complete claims"
```

---

### Task 2: Early Complete promotion + update/complete API routes

**Files:**

- Modify: `src/zira_dashboard/rotation_training.py`
- Modify: `src/zira_dashboard/routes/rotations.py`
- Test: `tests/test_rotation_training.py`
- Test: `tests/test_staffing_rotations.py`

**Interfaces:**

- Consumes: Task 1 store helpers; `skill_levels.set_person_skill_level`; existing `_block_to_dict`, `_person_id_by_name`, `_lifecycle`.
- Produces:
  - `rotation_training.complete_block_now(block_id: int) -> None` — claim, promote every `skill_ids`, `mark_completed`; on promotion failure release prior status and re-raise/`InvalidTrainingBlock`.
  - `POST /api/rotations/training-blocks/{block_id}` (JSON body) — update editable fields; returns `{ok, block}` with progress fields.
  - `POST /api/rotations/training-blocks/{block_id}/complete` — early complete; returns `{ok, id, status: "completed"}`.
  - `_block_to_dict` includes `attended_days` and `remaining_attended_days`.

- [ ] **Step 1: Write the failing tests**

```python
def test_complete_block_now_promotes_before_planned_days_finish(monkeypatch):
    block = TrainingBlock(
        id=1, trainee_name="Adrian", trainer_name="Green", skill="Master Recycler",
        start_day=date(2026, 8, 4), planned_attended_days=5, status="active",
        trainee_id=10, skill_id=11, work_center="Master Recycler", skill_ids=(11,),
    )
    monkeypatch.setattr(rotation_training.rotation_store, "get_block", lambda _id: block)
    monkeypatch.setattr(
        rotation_training.rotation_store, "claim_early_completion", lambda _id: "active"
    )
    promoted = []
    monkeypatch.setattr(
        rotation_training.skill_levels,
        "set_person_skill_level",
        lambda pid, sid, level: promoted.append((pid, sid, level)),
    )
    marked = []
    monkeypatch.setattr(
        rotation_training.rotation_store, "mark_completed", lambda _id: marked.append(_id)
    )
    rotation_training.complete_block_now(1)
    assert promoted == [(10, 11, 1)]
    assert marked == [1]


def test_complete_endpoint_promotes_early(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    monkeypatch.setattr(rotations.rotation_training, "complete_block_now", lambda _id: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_stable_cache", lambda: None)
    resp = client.post("/api/rotations/training-blocks/42/complete", json={})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "id": 42, "status": "completed"}


def test_update_endpoint_rejects_planned_below_attended(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    monkeypatch.setattr(
        rotations.db, "query",
        lambda sql, params=None: [{"id": 2}] if "FROM people" in sql else [],
    )
    def boom(**kwargs):
        raise rotation_store.InvalidTrainingBlock(
            "Planned days cannot be below attended days (3)."
        )
    monkeypatch.setattr(rotations.rotation_store, "update_block", boom)
    monkeypatch.setattr(rotations._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(rotations._http_cache, "invalidate_stable_cache", lambda: None)
    resp = client.post(
        "/api/rotations/training-blocks/42",
        json={
            "trainer": "Green",
            "work_center": "Repair 1",
            "start_day": "2026-08-01",
            "workdays": 1,
        },
    )
    assert resp.status_code == 422
    assert "attended" in resp.json()["error"]
```


- [ ] **Step 2: Run tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_training.py -k complete_block_now tests/test_staffing_rotations.py -k 'complete_endpoint or update_endpoint' -q`

Expected: FAIL (missing function/routes).

- [ ] **Step 3: Implement complete_block_now and routes**

```python
# rotation_training.py
def complete_block_now(block_id: int) -> None:
    block = rotation_store.get_block(block_id)
    if block is None:
        raise rotation_store.InvalidTrainingBlock("Unknown training block.")
    prior = rotation_store.claim_early_completion(block_id)
    if prior is None:
        raise rotation_store.InvalidTrainingBlock("Training cannot be completed.")
    skill_ids = tuple(block.skill_ids or (block.skill_id,))
    try:
        for skill_id in skill_ids:
            skill_levels.set_person_skill_level(block.trainee_id, skill_id, 1)
        rotation_store.mark_completed(block_id)
    except Exception:
        rotation_store.release_early_completion_claim(block_id, prior)
        raise
```

```python
# routes/rotations.py
def _block_to_dict(block, *, attended_days: int | None = None) -> dict:
    if attended_days is None:
        try:
            attended_days = rotation_store.attended_day_count(block.id)
        except Exception:
            attended_days = 0
    return {
        ...,
        "attended_days": attended_days,
        "remaining_attended_days": max(0, block.planned_attended_days - attended_days),
    }

@router.post("/api/rotations/training-blocks/{block_id}")
async def update_training_block(block_id: int, request: Request): ...

@router.post("/api/rotations/training-blocks/{block_id}/complete")
async def complete_training_block(block_id: int):
    # mirror _lifecycle: validate id, to_thread complete_block_now, invalidate caches
```

Import `rotation_training` in the routes module (or call through a thin wrapper already used elsewhere). On update, resolve trainer name → id; do not allow changing trainee.

- [ ] **Step 4: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_training.py -k complete_block_now tests/test_staffing_rotations.py -k 'complete_endpoint or update_endpoint or training_protocol_endpoint' -q`

Expected: PASS (existing create/lifecycle tests still pass).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/rotation_training.py src/zira_dashboard/routes/rotations.py tests/test_rotation_training.py tests/test_staffing_rotations.py
git commit -m "feat: add training update and early-complete APIs"
```

---

### Task 3: Staffing context — plant-wide manageable list with progress

**Files:**

- Modify: `src/zira_dashboard/routes/staffing.py` (`_training_blocks_context`, `_recycled_context_for_day` / page context that sets `TRAINING_PROTOCOLS`)
- Modify: `src/zira_dashboard/templates/staffing.html` (window bootstrap key if renamed)
- Test: `tests/test_staffing_rotations.py`

**Interfaces:**

- Consumes: `rotation_store.manageable_blocks`, `attended_day_count` / `resolved_days`.
- Produces: template context key `manageable_training_blocks` (list of dicts with `attended_days`, `planned_attended_days`, `remaining_attended_days`, status, work_center, trainee, trainer, start_day, id). Keep day-scoped `active_training_blocks` only if still required for picker reservations; sidebar JS reads `window.TRAINING_PROTOCOLS` from `manageable_training_blocks`.

- [ ] **Step 1: Write the failing context test**

```python
def test_staffing_exposes_plant_wide_manageable_training_progress(monkeypatch):
    # Stub manageable_blocks to return one active + one paused block.
    # Stub attended counts 2 and 0.
    ctx = _render_staffing_page(...)  # or call the context helper directly
    blocks = ctx["manageable_training_blocks"]
    assert {b["status"] for b in blocks} == {"active", "paused"}
    adrian = next(b for b in blocks if b["trainee"] == "Adrian A.")
    assert adrian["attended_days"] == 2
    assert adrian["planned_attended_days"] == 5
    assert adrian["remaining_attended_days"] == 3
```

Update `test_recycled_context_for_day_includes_...` if it asserted only day-scoped list shape for the modal—point sidebar assertions at `manageable_training_blocks` and keep day-scoped behavior for reservations unchanged.

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py -k manageable_training -q`

Expected: FAIL (missing context key).

- [ ] **Step 3: Implement context wiring**

```python
def _manageable_training_blocks_context():
    out = []
    for block in rotation_store.manageable_blocks():
        try:
            attended = rotation_store.attended_day_count(block.id)
        except Exception:
            attended = 0
        out.append({
            "id": block.id,
            "trainee": block.trainee_name,
            "trainer": block.trainer_name,
            "work_center": block.work_center,
            "group": staffing.scheduling_group_for_skill(block.skill),
            "skill": block.skill,
            "start_day": block.start_day.isoformat(),
            "planned_attended_days": block.planned_attended_days,
            "attended_days": attended,
            "remaining_attended_days": max(0, block.planned_attended_days - attended),
            "status": block.status,
        })
    return out
```

In the staffing page render path, set `manageable_training_blocks=_manageable_training_blocks_context()` (empty list on failure). Template:

```html
window.TRAINING_PROTOCOLS = {{ manageable_training_blocks|tojson }};
```

Do not feed day-scoped `active_blocks_for_day` into the sidebar.

- [ ] **Step 4: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py -k 'manageable_training or recycled_context_for_day or training_blocks' -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/routes/staffing.py src/zira_dashboard/templates/staffing.html tests/test_staffing_rotations.py
git commit -m "feat: expose plant-wide training progress to staffing"
```

---

### Task 4: Sidebar Training panel UI (compact cards)

**Files:**

- Modify: `src/zira_dashboard/templates/staffing.html` — remove header `+ Training` button and `#training-protocol-modal`; add `#training-sidebar` under Notes inside `.day-context`.
- Modify: `src/zira_dashboard/static/staffing.css` — compact card, progress bar, action row, inline edit/create; posted read-only.
- Modify: `src/zira_dashboard/static/staffing.js` — rewire `initTrainingProtocols` to the sidebar panel.
- Test: `tests/test_staffing_rotations.py` (`test_staffing_exposes_unified_training_setup_and_removes_row_toggles` and related static contracts)

**Interfaces:**

- Consumes: `window.TRAINING_PROTOCOLS`, `TRAINING_PROTOCOL_PEOPLE`, `TRAINING_PROTOCOL_WORK_CENTERS`, `rebuildRotationForTraining`, `__viewingPosted`.
- Produces: sidebar DOM with progress `attended_days of planned_attended_days`, Edit expand (trainer, work_center, start_day, workdays), Pause/Resume/End/Complete, Start form; API calls to create / update / complete / pause / resume / end.

- [ ] **Step 1: Rewrite the static contract test (red)**

```python
def test_staffing_training_lives_in_sidebar_not_modal():
    html = (ROOT / "src/zira_dashboard/templates/staffing.html").read_text()
    js = (ROOT / "src/zira_dashboard/static/staffing.js").read_text()
    css = (ROOT / "src/zira_dashboard/static/staffing.css").read_text()

    assert 'id="training-sidebar"' in html
    assert "day-notes" in html  # panel follows notes in markup order
    assert 'id="training-protocol-modal"' not in html
    assert 'id="training-protocol-open"' not in html
    assert "/api/rotations/training-blocks/" in js  # update/complete paths
    assert "complete" in js
    assert "attended_days" in js
    assert ".training-progress" in css
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py::test_staffing_training_lives_in_sidebar_not_modal -q`

Expected: FAIL.

- [ ] **Step 3: Implement template, CSS, and JS**

Template sketch (under Notes, before Schedule Goal):

```html
<section id="training-sidebar" class="training-sidebar" aria-labelledby="training-sidebar-title"
         data-readonly="{{ '1' if viewing_posted else '0' }}">
  <div class="training-sidebar-head">
    <h3 id="training-sidebar-title">Training</h3>
    <span id="training-sidebar-count" class="hint"></span>
  </div>
  <p id="training-sidebar-error" class="training-protocol-error" role="alert" aria-live="assertive"></p>
  <p id="training-sidebar-empty" class="training-protocol-empty">No active training protocols.</p>
  <ul id="training-sidebar-list" class="training-sidebar-list"></ul>
  {% if not viewing_posted %}
  <button type="button" id="training-sidebar-start-toggle" class="training-start-toggle">+ Start training</button>
  <form id="training-sidebar-create" class="training-sidebar-create" hidden>...</form>
  {% endif %}
</section>
```

JS behavior:

- Render each protocol card: title `trainee`, meta `work_center · with trainer`, progress bar width `attended/planned`, label `N of M`, status.
- Actions (when not readonly): Edit toggles inline fields → POST `/api/rotations/training-blocks/{id}` with trainer, work_center, start_day, workdays; Pause/Resume/End unchanged URLs; Complete → POST `.../complete` then remove card.
- Start form: same fields as old modal; on success call `rebuildRotationForTraining` then reload/navigate as today.
- Replace old modal init; keep `rebuildRotationForTraining` global.

- [ ] **Step 4: Run static + related tests**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py -k 'training_lives_in_sidebar or unified_training or training_protocol or people_matrix_no_longer' -q`

Expected: PASS (delete or rewrite obsolete modal assertions).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/staffing.html src/zira_dashboard/static/staffing.js src/zira_dashboard/static/staffing.css tests/test_staffing_rotations.py
git commit -m "feat: manage training protocols from staffing sidebar"
```

---

### Task 5: Document and verify

**Files:**

- Modify: `CHANGELOG.md` (What's New, child-friendly)
- Modify: `README.md` only if it still describes the Training modal / `+ Training` button
- Test: focused suites below

- [ ] **Step 1: Update README if it mentions the modal**

Search README for “Training” / “training protocol” / “+ Training”. Replace modal wording with sidebar-under-Notes wording. Skip if already accurate.

- [ ] **Step 2: Add CHANGELOG entry under `## 2026-08-06`**

```markdown
### Training on the schedule sidebar

#### Features

- **Training plans show on the right side of the schedule.** You can see how many days are done, change the plan, finish early to make the person ready for normal work, or stop the plan without finishing.
```

- [ ] **Step 3: Run focused verification**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest \
  tests/test_rotation_store.py \
  tests/test_rotation_training.py \
  tests/test_staffing_rotations.py -k 'training or manageable or complete_block or update_block' -q
```

Expected: PASS (or only unrelated skips).

- [ ] **Step 4: Commit and push**

```bash
git add CHANGELOG.md README.md
git commit -m "docs: explain staffing training sidebar"
git push origin main
```

---

## Spec coverage checklist

| Spec requirement | Task |
| --- | --- |
| Sidebar under Notes, compact cards, progress | Task 4 |
| Plant-wide active + paused | Tasks 1, 3 |
| Attended of planned | Tasks 2–4 |
| Edit trainer / work center / start / planned days | Tasks 1, 2, 4 |
| Planned ≥ attended | Task 1 |
| Complete anytime + promote | Tasks 1, 2, 4 |
| End without promote | existing + Task 4 |
| Create in sidebar; remove modal | Task 4 |
| Posted read-only | Task 4 |
| Day-scoped Auto effects unchanged | Task 3 (keep `active_blocks_for_day`) |
| CHANGELOG | Task 5 |
