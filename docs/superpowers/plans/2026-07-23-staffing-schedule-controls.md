# Staffing Schedule Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clarify custom-hour scheduling state and enforce Recruit → schedule → Publish in the staffing title bar.

**Architecture:** Keep this view-only. The staffing route already exposes `hours_source`, effective start/end values, custom-hours persistence, and Saturday recruiting/preparation state. The template derives semantic classes from that context; CSS owns color treatment and the existing hours-editor script gains a simple toggle.

**Tech Stack:** FastAPI/Jinja templates, vanilla JavaScript, CSS, pytest.

## Global Constraints

- Do not add persisted data or alter the Saturday lifecycle.
- A custom badge reads `CUSTOM 7:00–12:00p`; only its ending time carries the meridiem.
- Publish is unavailable while Saturday recruiting is active and returns only after staffing preparation.
- Custom-hour enabled centers are soft blue; other day types retain their current color.

---

### Task 1: Scheduler title controls

**Files:**
- Modify: `src/zira_dashboard/templates/staffing.html:128-220`
- Modify: `src/zira_dashboard/static/staffing.js:1127-1150`
- Modify: `src/zira_dashboard/static/staffing.css:900-920`
- Test: `tests/test_staffing_static.py`
- Test: `tests/test_saturday_recruiting_static.py`

**Interfaces:**
- Consumes: Jinja `hours_source`, `eff_hours_label`, `saturday_recruiting`, and `saturday_staffing_prepared` context values.
- Produces: `hours-pill custom` markup with compact custom copy and a toggleable `#hours-editor`.

- [ ] **Step 1: Write failing static tests**

```python
def test_staffing_title_uses_date_picker_without_past_shortcut():
    html = Path("src/zira_dashboard/templates/staffing.html").read_text()
    assert 'href="/staffing/past"' not in html

def test_custom_hours_badge_uses_compact_copy_and_toggle_script():
    html = Path("src/zira_dashboard/templates/staffing.html").read_text()
    js = Path("src/zira_dashboard/static/staffing.js").read_text()
    assert "CUSTOM" in html
    assert "pill.addEventListener('click', () => editor.hidden ? open() : close());" in js
```

- [ ] **Step 2: Run the tests to verify failure**

Run: `.venv/bin/pytest -q tests/test_staffing_static.py tests/test_saturday_recruiting_static.py`

Expected: FAIL because the Past link remains, the custom pill still uses generic Hours/break text, and clicking the pill only opens the editor.

- [ ] **Step 3: Implement title-state markup and interaction**

```jinja2
{% if hours_source == 'custom' %}
  <span class="label">Custom</span><span>{{ eff_custom_hours_label }}</span>
{% else %}
  <span class="label">Hours</span><span>{{ eff_hours_label }}</span>
{% endif %}
```

```javascript
pill.addEventListener('click', () => editor.hidden ? open() : close());
```

Remove the Past anchor. Add a route context label that formats both times
without leading zeroes and adds the `a`/`p` suffix only to the end time. Hide
the publish submit while `saturday_recruiting` is active; do not render the
existing lock copy in that state.

- [ ] **Step 4: Run focused title-control tests**

Run: `.venv/bin/pytest -q tests/test_staffing_static.py tests/test_saturday_recruiting_static.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/routes/staffing.py src/zira_dashboard/templates/staffing.html src/zira_dashboard/static/staffing.js src/zira_dashboard/static/staffing.css tests/test_staffing_static.py tests/test_saturday_recruiting_static.py
git commit -m "feat: clarify staffing title controls"
```

### Task 2: Custom-day work-center treatment

**Files:**
- Modify: `src/zira_dashboard/templates/staffing.html:280`
- Modify: `src/zira_dashboard/static/staffing.css:510-530`
- Test: `tests/test_staffing_static.py`

**Interfaces:**
- Consumes: Jinja `hours_source` and each row's `_center_on` boolean.
- Produces: `custom-hours-day` table-row class whose enabled cells use a soft-blue background.

- [ ] **Step 1: Write a failing static test**

```python
def test_custom_hours_enabled_work_centers_use_soft_blue():
    html = Path("src/zira_dashboard/templates/staffing.html").read_text()
    css = Path("src/zira_dashboard/static/staffing.css").read_text()
    assert "custom-hours-day" in html
    assert "tr.custom-hours-day[data-on=\"true\"] td" in css
```

- [ ] **Step 2: Run the test to verify failure**

Run: `.venv/bin/pytest -q tests/test_staffing_static.py -k custom_hours_enabled`

Expected: FAIL because enabled rows always use `var(--accent-dim)`.

- [ ] **Step 3: Implement the custom-day row class and override**

```jinja2
<tr class="{% if hours_source == 'custom' %}custom-hours-day {% endif %}{% if not _center_on %}work-center-off{% endif %}">
```

```css
tr.custom-hours-day[data-on="true"] td { background: #dbeafe; }
tr.custom-hours-day[data-on="true"] td.bay { background: var(--panel-3); }
```

Keep the off-row and bay-cell styling unchanged.

- [ ] **Step 4: Run all focused staffing tests**

Run: `.venv/bin/pytest -q tests/test_staffing_static.py tests/test_staffing_saturday_recruiting.py tests/test_saturday_recruiting_static.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/staffing.html src/zira_dashboard/static/staffing.css tests/test_staffing_static.py
git commit -m "feat: highlight custom-hour work centers"
```

## Self-review

- Spec coverage: Task 1 covers Past removal, compact custom badge, editor toggle, blue Recruit treatment preservation, and Publish suppression during recruiting. Task 2 covers soft-blue enabled work centers on custom days.
- Placeholder scan: no placeholders or deferred implementation items.
- Type consistency: all template context variables already exist except `eff_custom_hours_label`, which Task 1 creates in the staffing route and uses only in that task's template markup.
