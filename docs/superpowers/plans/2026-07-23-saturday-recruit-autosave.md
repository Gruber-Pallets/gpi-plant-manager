# Saturday Recruitment Autosave Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Saturday recruitment wait for the visible Scheduler state to finish saving before the server checks for existing assignments.

**Architecture:** Keep the server-side assignment guard unchanged. Sequence the existing client-side operations by awaiting `window.flushAutosave()` before the recruitment activation request, and fail closed with a clear message when saving cannot be verified.

**Tech Stack:** Browser JavaScript, FastAPI/Jinja application, pytest static regression tests.

## Global Constraints

- Do not delete or weaken the server-side guard against overwriting saved assignments.
- Do not start recruitment when the Scheduler save fails or cannot be verified.
- Keep the current direct Recruit action with no confirmation dialog.
- Write new What's New text with short sentences and common words.

---

### Task 1: Sequence Scheduler save before recruitment activation

**Files:**
- Modify: `tests/test_saturday_recruiting_static.py`
- Modify: `src/zira_dashboard/static/saturday-recruiting.js`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `window.flushAutosave() -> Promise<void>` from `src/zira_dashboard/static/staffing.js`.
- Produces: The existing `activate-from-schedule` click handler now posts only after autosave resolves.

- [ ] **Step 1: Write the failing regression test**

Add this test after `test_scheduler_recruit_script_posts_directly_without_confirmation_dialog`:

```python
def test_scheduler_recruit_waits_for_autosave_before_activation():
    js = Path("src/zira_dashboard/static/saturday-recruiting.js").read_text()

    flush_call = "await window.flushAutosave();"
    activation_call = (
        "const response = await fetch("
        "'/api/staffing/saturday-recruiting/activate-from-schedule'"
    )
    assert flush_call in js
    assert activation_call in js
    assert js.index(flush_call) < js.index(activation_call)
    assert "Could not save the schedule. Recruiting was not started." in js
```

- [ ] **Step 2: Run the focused test to verify RED**

Run:

```bash
.venv/bin/pytest tests/test_saturday_recruiting_static.py::test_scheduler_recruit_waits_for_autosave_before_activation -q
```

Expected: FAIL because `saturday-recruiting.js` does not yet call `window.flushAutosave()`.

- [ ] **Step 3: Implement the minimal sequencing fix**

Replace the activation script with:

```javascript
document.addEventListener('click', async event => {
  const button = event.target.closest('[data-saturday-action="activate-from-schedule"]');
  if (!button || button.disabled) return;

  const saveErrorMessage = 'Could not save the schedule. Recruiting was not started.';
  button.disabled = true;
  try {
    if (typeof window.flushAutosave !== 'function') {
      throw new Error(saveErrorMessage);
    }
    try {
      await window.flushAutosave();
    } catch (_error) {
      throw new Error(saveErrorMessage);
    }
    const response = await fetch('/api/staffing/saturday-recruiting/activate-from-schedule', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({day: button.dataset.day}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Could not start Saturday recruiting.');
    window.location.reload();
  } catch (error) {
    button.disabled = false;
    window.alert(error.message);
  }
});
```

- [ ] **Step 4: Run the focused static tests to verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_saturday_recruiting_static.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Add the user-facing patch note**

Under `## 2026-07-23`, add:

```markdown
### Fixes

- **Saturday recruiting now waits for the schedule to save.** If you clear the Saturday schedule and click Recruit right away, the app saves the empty schedule first. If saving fails, recruiting does not start.
```

- [ ] **Step 6: Run the Saturday recruitment regression gate**

Run:

```bash
.venv/bin/pytest -q tests/test_saturday_recruiting.py tests/test_saturday_recruiting_store.py tests/test_saturday_recruiting_manager_routes.py tests/test_saturday_recruiting_static.py tests/test_staffing_saturday_recruiting.py tests/test_timeclock_saturday_recruiting.py tests/test_saturday_work_reminder.py
.venv/bin/ruff check src/zira_dashboard tests/test_saturday_recruiting_static.py
git diff --check
```

Expected: all tests pass, Ruff reports no errors, and `git diff --check` exits successfully.

- [ ] **Step 7: Commit and push**

```bash
git add tests/test_saturday_recruiting_static.py src/zira_dashboard/static/saturday-recruiting.js CHANGELOG.md
git commit -m "fix: save Saturday schedule before recruiting"
git push origin main
```
