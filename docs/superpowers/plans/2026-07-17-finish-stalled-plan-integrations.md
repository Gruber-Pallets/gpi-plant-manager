# Finish Stalled Plan Integrations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the completed per-day Auto work-center work and the bounded Recycling dashboard scaling work on current `main` without importing unrelated historical changes.

**Architecture:** Build two integration commits on a worktree rooted at the current `main`. The daily-Auto delivery is the ordered five-commit historical implementation, applied only after a regression test demonstrates its Settings surface is absent. The scaling delivery is a file-scoped port of seven dashboard artifacts; its static test establishes the expected CSS and widget-layout invariants before the selected source files are applied.

**Tech Stack:** Python 3.12, FastAPI/Jinja, vanilla JavaScript/CSS, pytest, Git.

## Global Constraints

- Preserve all existing `main` behavior; do not merge either historical branch.
- Do not touch the user's uncommitted files in the primary checkout.
- Daily Auto uses schedule-owned enabled-center lists; Settings remains only the template source for a newly created schedule.
- Scaling may modify only `.gitignore`, `scripts/preview_recycling.py`, `recycling.css`, `dashboard-grid.js`, `wc_dashboard.css`, `recycling.html`, and its static test.
- Do not port Trim Saw, People Matrix, Odoo, auto-lunch, time-off, Slack, or test-debt changes.
- Run each new regression test before the corresponding production change and retain it after the change.

---

## File map

| File | Responsibility |
| --- | --- |
| `src/zira_dashboard/_schema.py` | Stores the nullable JSONB daily enabled-center list. |
| `src/zira_dashboard/staffing.py` | Normalizes, persists, hydrates, snapshots, and reads the daily list. |
| `src/zira_dashboard/routes/staffing.py` | Seeds new schedules and mutates only a selected day. |
| `src/zira_dashboard/routes/settings.py` | Reads and saves the default template only. |
| `src/zira_dashboard/templates/settings.html` | Renders default Auto center controls. |
| `tests/test_settings_auto_work_centers.py` | Characterizes the Settings default-template boundary. |
| `src/zira_dashboard/static/{recycling.css,wc_dashboard.css}` | Keeps dashboard internals proportional at small TV heights. |
| `src/zira_dashboard/static/dashboard-grid.js` | Re-fits a TV grid after fonts load. |
| `src/zira_dashboard/templates/recycling.html` | Gives both bar widgets six default rows. |
| `scripts/preview_recycling.py` | Produces reproducible busy/empty dashboard previews. |
| `tests/test_recycling_scaling_static.py` | Locks the scaling layout invariants. |

### Task 1: Integrate per-day Auto work centers

**Files:**
- Modify: `src/zira_dashboard/_schema.py`
- Modify: `src/zira_dashboard/staffing.py`
- Modify: `src/zira_dashboard/routes/{settings,staffing,rotations}.py`
- Modify: `src/zira_dashboard/templates/settings.html`
- Modify: `src/zira_dashboard/{exception_inbox.py,object_models.py}`
- Modify: `tests/test_{exception_inbox,rotation_store,saturday_recruiting_manager_routes,staffing_rotations,staffing_schedule_metadata}.py`
- Create: `tests/test_settings_auto_work_centers.py`

**Consumes:** historical commits `975f034`, `74c3a7d`, `adc1e79`, `80e7eef`, and `cd63e7b` from `codex/default-auto-work-centers-by-day`.

**Produces:** `Schedule.auto_enabled_work_centers: list[str]`, daily schedule persistence, Settings default-template controls, and schedule-scoped Auto center mutation.

- [ ] **Step 1: Add the focused Settings regression test before porting source code.**

  Create `tests/test_settings_auto_work_centers.py` with the branch's three public checks: the Settings template contains `name="default_auto_work_centers"` and `Default Auto Work Centers`; `_ordered_default_auto_work_centers(["Repair 2", "Unknown"]) == ["Repair 2"]` and `DEFAULT_AUTO_WORK_CENTERS_SETTING == "rotation_auto_enabled_work_centers"`; and `_settings_default_auto_work_centers()` delegates to `_default_auto_work_centers(plant_today())` when the setting is missing.

- [ ] **Step 2: Verify the regression is red.**

  Run:

  ```bash
  ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_settings_auto_work_centers.py -q
  ```

  Expected: FAIL because current `main` has neither the daily-default Settings control nor the helper/constant boundary.

- [ ] **Step 3: Apply the complete, ordered daily-Auto implementation without merging its branch.**

  Run:

  ```bash
  git cherry-pick --no-commit \
    975f0348a57e0d9999383db8e06da7c216023222 \
    74c3a7d9517e305bb056e6e7d62b584da4a758e7 \
    adc1e79939ad8db462f2a2bf3b0202bef6af739b \
    80e7eef891cbbb93c292c25eee567e824a86d926 \
    cd63e7b320bedbfa2901cbd97eb7d829981c0f9d
  ```

  If Git reports conflicts, resolve each affected hunk by retaining current-main lifecycle fields (`published_snapshot`, delivery state, and draft behavior) and adding only the candidate branch's schedule-owned Auto-center field and reads. Do not continue a merge; use `git cherry-pick --continue` only after every conflict is resolved and the focused regression remains present.

- [ ] **Step 4: Verify the daily-Auto acceptance suite is green.**

  Run:

  ```bash
  ZIRA_API_KEY=test .venv/bin/python -m pytest \
    tests/test_settings_auto_work_centers.py \
    tests/test_rotation_store.py \
    tests/test_staffing_rotations.py \
    tests/test_staffing_schedule_metadata.py \
    tests/test_saturday_recruiting_manager_routes.py \
    tests/test_exception_inbox.py -q
  ```

  Expected: PASS with no test failures.

- [ ] **Step 5: Review and commit the bounded delivery.**

  Run `git diff --check`, confirm the changed-file list contains only the Task 1 files, stage them, then commit:

  ```bash
  git add src/zira_dashboard/_schema.py src/zira_dashboard/exception_inbox.py \
    src/zira_dashboard/object_models.py src/zira_dashboard/routes/rotations.py \
    src/zira_dashboard/routes/settings.py src/zira_dashboard/routes/staffing.py \
    src/zira_dashboard/staffing.py src/zira_dashboard/templates/settings.html \
    tests/test_exception_inbox.py tests/test_rotation_store.py \
    tests/test_saturday_recruiting_manager_routes.py \
    tests/test_settings_auto_work_centers.py tests/test_staffing_rotations.py \
    tests/test_staffing_schedule_metadata.py
  git commit -m "feat: persist auto work centers by day"
  ```

### Task 2: Port Recycling dashboard scaling only

**Files:**
- Modify: `.gitignore`
- Create: `scripts/preview_recycling.py`
- Modify: `src/zira_dashboard/static/recycling.css`
- Modify: `src/zira_dashboard/static/dashboard-grid.js`
- Modify: `src/zira_dashboard/static/wc_dashboard.css`
- Modify: `src/zira_dashboard/templates/recycling.html`
- Create: `tests/test_recycling_scaling_static.py`

**Consumes:** scaling commits `016b31c`, `0f8b151`, `1b31bf7`, `75e3b5b`, `703bc26`, and `6275bce` from `fix/recycling-dashboard-scaling`.

**Produces:** proportional chart/bar sizing, post-font TV fitting, six-row bar-widget defaults, a reproducible preview script, and static regression coverage.

- [ ] **Step 1: Add the static scaling test before touching dashboard source.**

  Create `tests/test_recycling_scaling_static.py` from `fix/recycling-dashboard-scaling`. Preserve all seven checks: no `60px`/`80px` plot floors, no `14px`/`200px` bar-track bounds, no `height: 110px`, no large widget-padding ceiling, no `2rem` KPI font floor in `wc_dashboard.css`, and six-row defaults for both bar widgets in `recycling.html`.

- [ ] **Step 2: Verify the static regression is red.**

  Run:

  ```bash
  ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_recycling_scaling_static.py -q
  ```

  Expected: FAIL because current CSS still has one or more fixed floors/caps and the bar widgets have their old default height.

- [ ] **Step 3: Apply only approved scaling files.**

  Apply the selected source-file changes from `fix/recycling-dashboard-scaling` using a file-scoped patch:

  ```bash
  git diff --binary main...fix/recycling-dashboard-scaling -- \
    scripts/preview_recycling.py \
    src/zira_dashboard/static/recycling.css \
    src/zira_dashboard/static/dashboard-grid.js \
    src/zira_dashboard/static/wc_dashboard.css \
    src/zira_dashboard/templates/recycling.html | git apply --3way
  ```

  Manually add only `scripts/_preview_out/` to `.gitignore`; do not bring in the branch's `pg_uri.txt` or `pgdata_review/` entries. The resulting CSS removes fixed chart/bar floors, uses a smaller widget-content padding ceiling, keeps bar labels readable in short widgets, and lets the operator KPI value shrink. The template uses `widget_attrs('dismantler-bars', 0, 2, 6, 6)` and `widget_attrs('repair-bars', 6, 2, 6, 6)`. The TV script calls `document.fonts.ready.then(fitGridToViewport)` behind an availability guard.

- [ ] **Step 4: Verify static behavior and preview generation.**

  Run:

  ```bash
  ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_recycling_scaling_static.py -q
  DATABASE_URL="$(cat pg_uri.txt)" ZIRA_API_KEY=test .venv/bin/python scripts/preview_recycling.py
  ```

  Expected: the static test passes; the preview command writes editor, TV, and operator HTML variants. If `pg_uri.txt` or its database is unavailable, record that environmental blocker after the static test passes; do not change production code to bypass it.

- [ ] **Step 5: Review scope and commit.**

  Run `git diff --check` and verify that only the seven Task 2 files changed. Stage them, then commit:

  ```bash
  git add .gitignore scripts/preview_recycling.py \
    src/zira_dashboard/static/recycling.css \
    src/zira_dashboard/static/dashboard-grid.js \
    src/zira_dashboard/static/wc_dashboard.css \
    src/zira_dashboard/templates/recycling.html \
    tests/test_recycling_scaling_static.py
  git commit -m "fix: scale recycling dashboards across viewports"
  ```

### Task 3: Final integration verification and delivery

**Files:** no new production files.

**Consumes:** Task 1 and Task 2 commits.

**Produces:** verified commits pushed to `origin/main`.

- [ ] **Step 1: Run the combined regression suite.**

  Run:

  ```bash
  ZIRA_API_KEY=test .venv/bin/python -m pytest \
    tests/test_settings_auto_work_centers.py \
    tests/test_rotation_store.py \
    tests/test_staffing_rotations.py \
    tests/test_staffing_schedule_metadata.py \
    tests/test_saturday_recruiting_manager_routes.py \
    tests/test_exception_inbox.py \
    tests/test_recycling_scaling_static.py -q
  ```

  Expected: PASS with no failures.

- [ ] **Step 2: Inspect the final range and push it.**

  Run:

  ```bash
  git diff --check origin/main..HEAD
  git log --oneline origin/main..HEAD
  git push origin HEAD:main
  ```

  Expected: no whitespace errors, exactly the two delivery commits after the documentation commits, and a successful push to `origin/main`.
