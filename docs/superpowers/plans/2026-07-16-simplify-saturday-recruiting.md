# Simplify Saturday Recruiting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recruit Saturday minimum crews directly from enabled Scheduler work centers, replacing the separate Saturday configuration panel.

**Architecture:** A new activation endpoint derives requested counts from the Saturday draft schedule. The Scheduler renders either the blue Recruit action or compact response counts, while existing Timeclock and recruiting lifecycle behavior remain unchanged.

**Tech Stack:** FastAPI, Jinja2, vanilla JavaScript, pytest, Ruff.

## Global Constraints

- Counts derive from `enabled_work_centers` and configured `min_ops`; the browser supplies only the day.
- Recruit activates immediately, before the green Publish action, with no confirmation screen.
- Existing Timeclock, deadline, staffing, Spanish-primary, and reminder behavior is unchanged.

---

### Task 1: Add schedule-derived recruitment activation

**Files:**
- Modify: `src/zira_dashboard/routes/saturday_recruiting.py`
- Modify: `src/zira_dashboard/routes/staffing.py`
- Test: `tests/test_saturday_recruiting_manager_routes.py`
- Test: `tests/test_staffing_saturday_recruiting.py`

**Interfaces:** Produces `POST /api/staffing/saturday-recruiting/activate-from-schedule` taking `{"day": "YYYY-MM-DD"}` and Scheduler context `saturday_recruit_enabled_count`, `saturday_response_summary`.

- [ ] **Step 1: Write failing endpoint tests**

```python
def test_activate_from_schedule_uses_enabled_center_minimums(monkeypatch):
    monkeypatch.setattr(routes.staffing, "load_schedule", lambda _: staffing.Schedule(day=SATURDAY, enabled_work_centers={"Repair 1"}))
    monkeypatch.setattr(routes.staffing, "LOCATIONS", (staffing.Location("Repair 1", "Repair", "Bay", "Recycled", None, min_ops=2, max_ops=4),))
    seen = {}
    monkeypatch.setattr(routes.store, "activate", lambda **kw: seen.update(kw) or _bundle())
    assert client.post("/api/staffing/saturday-recruiting/activate-from-schedule", json={"day": "2026-07-25"}).status_code == 200
    assert seen["requested_counts"] == {REPAIR_ID: 2}

def test_activate_from_schedule_rejects_no_enabled_centers(monkeypatch):
    monkeypatch.setattr(routes.staffing, "load_schedule", lambda _: staffing.Schedule(day=SATURDAY, enabled_work_centers=set()))
    assert client.post("/api/staffing/saturday-recruiting/activate-from-schedule", json={"day": "2026-07-25"}).status_code == 422
```

- [ ] **Step 2: Run the tests to prove they fail**

Run: `.venv/bin/pytest tests/test_saturday_recruiting_manager_routes.py -q`

Expected: FAIL because `activate-from-schedule` is absent.

- [ ] **Step 3: Implement the endpoint**

```python
@router.post("/activate-from-schedule")
async def activate_from_schedule(request: Request):
    day = date.fromisoformat(str((await _body(request))["day"]))
    if day.weekday() != 5:
        raise HTTPException(status_code=422, detail="Saturday recruiting requires a Saturday")
    enabled = set(staffing.load_schedule(day).enabled_work_centers or ())
    positions_by_name = {position.wc_name: position.wc_id for position in store.available_positions()}
    counts = {
        positions_by_name[loc.name]: loc.min_ops
        for loc in staffing.LOCATIONS
        if loc.name in enabled and loc.min_ops > 0 and loc.name in positions_by_name
    }
    if not counts:
        raise HTTPException(status_code=422, detail="Turn on at least one work center before recruiting.")
```

Reuse existing `activate()` shift/deadline computation, `store.activate`, conflict mapping, and cache invalidation. In the Scheduler route, derive enabled-center count and sorted response lists: `committed -> yes`, `declined -> no`, every unresolved response -> deciding; omit cancelled commitments.

- [ ] **Step 4: Verify and commit**

Run: `.venv/bin/pytest tests/test_saturday_recruiting_manager_routes.py tests/test_staffing_saturday_recruiting.py -q`

Expected: PASS, except database-gated skips.

```bash
git add src/zira_dashboard/routes/saturday_recruiting.py src/zira_dashboard/routes/staffing.py tests/test_saturday_recruiting_manager_routes.py tests/test_staffing_saturday_recruiting.py
git commit -m "feat: recruit Saturday centers from scheduler"
```

### Task 2: Replace the panel with Scheduler-native controls

**Files:**
- Modify: `src/zira_dashboard/templates/staffing.html`
- Modify: `src/zira_dashboard/static/saturday-recruiting.js`
- Modify: `src/zira_dashboard/static/saturday-recruiting.css`
- Remove: `src/zira_dashboard/templates/_saturday_recruiting_panel.html`
- Test: `tests/test_saturday_recruiting_static.py`

**Interfaces:** Consumes Task 1's endpoint and context. Produces `data-saturday-action="activate-from-schedule"` and `.saturday-response-summary`.

- [ ] **Step 1: Write failing static tests**

```python
def test_scheduler_uses_recruit_action_not_separate_panel():
    template = Path("src/zira_dashboard/templates/staffing.html").read_text()
    assert 'data-saturday-action="activate-from-schedule"' in template
    assert "Recruit for {{ saturday_recruit_enabled_count }} work centers" in template
    assert "_saturday_recruiting_panel.html" not in template

def test_response_counts_are_focusable_and_list_names():
    template = Path("src/zira_dashboard/templates/staffing.html").read_text()
    assert 'class="saturday-response-summary"' in template
    assert 'tabindex="0"' in template
    assert 'saturday_response_summary[key]|join' in template
```

- [ ] **Step 2: Run tests to prove they fail**

Run: `.venv/bin/pytest tests/test_saturday_recruiting_static.py -q`

Expected: FAIL because the old panel is included.

- [ ] **Step 3: Render compact controls before Publish**

```jinja2
{% if day_is_saturday and not saturday_recruiting and saturday_recruit_enabled_count %}
<button type="button" class="save-button saturday-recruit-button" data-saturday-action="activate-from-schedule" data-day="{{ day }}">Recruit for {{ saturday_recruit_enabled_count }} work centers</button>
{% elif day_is_saturday and saturday_recruiting and saturday_recruiting.status != 'cancelled' %}
<div class="saturday-response-summary" aria-label="Saturday recruiting responses">
{% for key, label in [('yes', 'yes'), ('no', 'no'), ('deciding', 'still deciding')] %}
<span class="saturday-response-count" tabindex="0" title="{{ saturday_response_summary[key]|join(', ') or 'None' }}">{{ saturday_response_summary[key]|length }} {{ label }}</span>{% if not loop.last %} · {% endif %}
{% endfor %}
</div>
{% endif %}
```

Remove the panel include and file; preserve Publish and its existing lock.

- [ ] **Step 4: Replace panel JavaScript and add styling**

```javascript
document.addEventListener('click', async event => {
  const button = event.target.closest('[data-saturday-action="activate-from-schedule"]');
  if (!button || button.disabled) return;
  button.disabled = true;
  try {
    const response = await fetch('/api/staffing/saturday-recruiting/activate-from-schedule', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({day:button.dataset.day})});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Could not start Saturday recruiting.');
    window.location.reload();
  } catch (error) { button.disabled = false; window.alert(error.message); }
});
```

```css
.saturday-recruit-button { background:#2563eb; border-color:#2563eb; color:#fff; }
.saturday-response-summary { display:inline-flex; gap:.35rem; font-weight:700; color:#475569; }
.saturday-response-count { cursor:help; text-decoration:underline dotted; }
.saturday-response-count:focus { outline:2px solid #2563eb; outline-offset:3px; }
```

Delete manual count, position, and shift controls from the JavaScript.

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/pytest tests/test_saturday_recruiting_static.py tests/test_staffing_saturday_recruiting.py -q && .venv/bin/ruff check src/zira_dashboard/routes/staffing.py src/zira_dashboard/routes/saturday_recruiting.py`

Expected: PASS.

```bash
git add src/zira_dashboard/templates/staffing.html src/zira_dashboard/static/saturday-recruiting.js src/zira_dashboard/static/saturday-recruiting.css tests/test_saturday_recruiting_static.py tests/test_staffing_saturday_recruiting.py
git rm src/zira_dashboard/templates/_saturday_recruiting_panel.html
git commit -m "feat: simplify Saturday scheduler recruiting"
```

### Task 3: Verify Timeclock activation and document the workflow

**Files:**
- Modify: `README.md`
- Modify: `tests/test_saturday_recruiting_manager_routes.py`
- Test: `tests/test_timeclock_saturday_recruiting.py`

- [ ] **Step 1: Add a live-offer regression**

```python
def test_schedule_activation_makes_timeclock_banner_live(monkeypatch):
    response = client.post("/api/staffing/saturday-recruiting/activate-from-schedule", json={"day": "2026-07-25"})
    assert response.status_code == 200
    assert store.home_banner(NOW).remaining_count > 0
```

- [ ] **Step 2: Update README**

Add: “Open the Saturday Scheduler, turn on the work centers you plan to run, and click the blue **Recruit for X work centers** action. Each enabled center requests its configured minimum crew and the offer immediately appears in Timeclock. Accepted people appear in Unassigned for normal assignment; green Publish remains the final step.”

- [ ] **Step 3: Run regression gate and commit**

Run: `.venv/bin/pytest -q tests/test_saturday_recruiting.py tests/test_saturday_recruiting_store.py tests/test_saturday_recruiting_manager_routes.py tests/test_saturday_recruiting_static.py tests/test_staffing_saturday_recruiting.py tests/test_timeclock_saturday_recruiting.py tests/test_saturday_work_reminder.py tests/test_employee_notifications.py tests/test_timeclock_notifications_routes.py tests/test_timeclock_i18n.py && .venv/bin/ruff check src/zira_dashboard tests && git diff --check`

Expected: PASS; only database-gated tests may skip.

```bash
git add README.md tests/test_saturday_recruiting_manager_routes.py tests/test_timeclock_saturday_recruiting.py
git commit -m "docs: explain scheduler-led Saturday recruiting"
```
