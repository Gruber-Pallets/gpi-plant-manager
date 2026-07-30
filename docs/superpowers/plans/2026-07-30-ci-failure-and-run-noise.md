# CI Failure and Run Noise Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore a green `main` workflow and stop superseded test runs from piling up during rapid pushes.

**Architecture:** Keep the shared GOAT category registry in its approved global order, but make the Recycling leaderboard's historical display order explicit by looking categories up by key. Add workflow-level GitHub Actions concurrency keyed by workflow and Git reference, with a static pytest contract that protects the configuration.

**Tech Stack:** Python 3.13, pytest, Ruff, GitHub Actions YAML

## Global Constraints

- The Recycling leaderboard order is `Dismantler GOAT`, then `Repair GOAT`.
- The shared GOAT category order remains `repairs`, `dismantlers`, `juniors`, `woodpecker`, `hand_build`.
- GitHub Actions failure notifications remain enabled.
- Tests continue to run on every push to `main` and on pull requests.
- Only an older in-progress run for the same workflow and Git reference is canceled.
- The GitHub Actions Node.js deprecation warning is outside this change.
- New `CHANGELOG.md` notes use short sentences and common words.
- Do not stage the existing untracked `.cursorignore`, `.python-version`, or `uv.lock` files.

---

### Task 1: Restore the Recycling GOAT display order

**Files:**
- Modify: `src/zira_dashboard/routes/recycling_leaderboard.py:14-17`
- Test: `tests/test_recycling_leaderboard_tv.py:111-136`
- Verify: `tests/test_goat_categories.py:6-29`
- Modify: `CHANGELOG.md:14-18`

**Interfaces:**
- Consumes: `goat_categories.category_for_key(key: str) -> GoatCategory`
- Produces: `_CURRENT_GOAT_GROUPS: tuple[tuple[str, str], ...]` ordered as Dismantlers, then Repairs

- [ ] **Step 1: Verify the existing regression test is red**

Run:

```bash
DATABASE_URL= ZIRA_API_KEY=ci-dummy-zira-key .venv/bin/python -m pytest -q \
  tests/test_recycling_leaderboard_tv.py::test_current_recycling_goats_uses_awards_with_overrides
```

Expected: FAIL because the actual labels are `["Repair GOAT", "Dismantler GOAT"]`.

- [ ] **Step 2: Make the route's display order explicit**

Replace the current `_CURRENT_GOAT_GROUPS` construction in
`src/zira_dashboard/routes/recycling_leaderboard.py` with:

```python
_CURRENT_GOAT_CATEGORIES = tuple(
    goat_categories.category_for_key(key)
    for key in ("dismantlers", "repairs")
)
_CURRENT_GOAT_GROUPS = tuple(
    (category.leaderboard_label, category.label)
    for category in _CURRENT_GOAT_CATEGORIES
)
```

Do not reorder `_CATEGORIES` in `src/zira_dashboard/goat_categories.py`.

- [ ] **Step 3: Add the plain-language patch note**

Under `## 2026-07-30` → `### Fixes` in `CHANGELOG.md`, add:

```markdown
- **Recycling GOATs are back in their usual order.** Dismantler comes first and Repair comes second, so the leaderboard stays familiar.
```

- [ ] **Step 4: Verify the regression and global category contracts**

Run:

```bash
DATABASE_URL= ZIRA_API_KEY=ci-dummy-zira-key .venv/bin/python -m pytest -q \
  tests/test_recycling_leaderboard_tv.py::test_current_recycling_goats_uses_awards_with_overrides \
  tests/test_goat_categories.py
```

Expected: PASS with zero failures. The route test proves Dismantler-first display
order, while `test_categories_match_the_approved_groups_and_auto_activation`
continues to prove the shared Repairs-first registry order.

- [ ] **Step 5: Run focused lint**

Run:

```bash
.venv/bin/python -m ruff check \
  src/zira_dashboard/routes/recycling_leaderboard.py \
  tests/test_recycling_leaderboard_tv.py \
  tests/test_goat_categories.py
```

Expected: `All checks passed!`

- [ ] **Step 6: Commit and push the green-CI repair**

```bash
git add \
  src/zira_dashboard/routes/recycling_leaderboard.py \
  CHANGELOG.md
git commit -m "fix: restore recycling GOAT display order"
git push origin main
```

Expected: the commit and push succeed without staging the unrelated untracked
files.

### Task 2: Cancel superseded workflow runs

**Files:**
- Create: `tests/test_ci_workflow.py`
- Modify: `.github/workflows/tests.yml:9-14`
- Modify: `CHANGELOG.md:14-19`

**Interfaces:**
- Consumes: the repository's `.github/workflows/tests.yml` text
- Produces: a top-level concurrency group named `tests-${{ github.workflow }}-${{ github.ref }}` with `cancel-in-progress: true`

- [ ] **Step 1: Write the failing workflow contract test**

Create `tests/test_ci_workflow.py` with:

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_WORKFLOW = (ROOT / ".github" / "workflows" / "tests.yml").read_text(
    encoding="utf-8"
)


def test_tests_workflow_cancels_superseded_runs():
    expected = """concurrency:
  group: tests-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
"""
    assert expected in TEST_WORKFLOW
```

- [ ] **Step 2: Run the workflow contract test to verify it is red**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_ci_workflow.py
```

Expected: FAIL because `.github/workflows/tests.yml` has no `concurrency` block.

- [ ] **Step 3: Add workflow-level concurrency**

In `.github/workflows/tests.yml`, insert this block between the `on` and `jobs`
sections:

```yaml
concurrency:
  group: tests-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

```

Leave the existing `push` and `pull_request` triggers unchanged.

- [ ] **Step 4: Add the plain-language patch note**

Under `## 2026-07-30` → `### Fixes` in `CHANGELOG.md`, add:

```markdown
- **Old checks stop when a newer update arrives.** This keeps extra work from piling up and lets the newest update get checked sooner.
```

- [ ] **Step 5: Verify the workflow contract**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_ci_workflow.py
```

Expected: PASS.

- [ ] **Step 6: Run the complete local verification**

Run:

```bash
.venv/bin/python -m ruff check src tests scripts
DATABASE_URL= ZIRA_API_KEY=ci-dummy-zira-key .venv/bin/python -m pytest -q
git diff --check
```

Expected: Ruff reports `All checks passed!`; pytest finishes with zero failures;
`git diff --check` prints nothing and exits zero. Database-backed tests may skip
locally because `DATABASE_URL` is intentionally empty.

- [ ] **Step 7: Commit and push the workflow behavior**

```bash
git add \
  .github/workflows/tests.yml \
  tests/test_ci_workflow.py \
  CHANGELOG.md
git commit -m "ci: cancel superseded test runs"
git push origin main
```

Expected: the commit and push succeed without staging the unrelated untracked
files.

- [ ] **Step 8: Require the pushed Postgres-backed workflow to pass**

1. Record the final commit with `git rev-parse HEAD`.
2. Open
   `https://github.com/Gruber-Pallets/gpi-plant-manager/actions/workflows/tests.yml`.
3. Open the workflow run whose commit matches the recorded SHA.
4. Wait for the run to finish and inspect the job log if it fails.

Expected: Ruff passes and pytest reports zero failures on Python 3.13 with the
Postgres 16 service. Do not mark the work complete or archive the task unless
this pushed workflow is green.
