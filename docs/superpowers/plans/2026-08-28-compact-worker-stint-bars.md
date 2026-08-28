# Compact Worker Stint Bars Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every worker stint in one normal-height production bar and reveal its person, time, goal, and result only on hover, keyboard focus, or tap.

**Architecture:** Preserve the existing server-side segment geometry and shared Jinja bar macro. Replace the visible in-bar and below-bar detail text with one focusable hit area per stint, then let one dependency-free browser controller display a shared popover for all horizontal and vertical bars. Native `title` and `aria-label` text keep the details available if JavaScript fails.

**Tech Stack:** Python 3.11, Jinja2, shared dashboard HTML/CSS, dependency-free browser JavaScript, pytest, and Node.js for the interaction harness.

## Global Constraints

- Keep the horizontal production track at its existing exact height of `20px`.
- Keep completed checkered finish lines, live solid goal lines, red and green fills, and hatched shortfalls unchanged.
- Keep all existing stint start, actual, shortfall, goal, and finish percentages authoritative; do not change scoring or attribution code.
- Remove all always-visible person, time, actual/goal, and result text from segmented bars and vertical stint lists.
- Every stint detail must be available through hover, keyboard focus, and tap; hover must not be the only interaction.
- Only one popover may be open at a time; outside tap and Escape close it.
- Unsegmented bars and multi-day range bars must remain unchanged.
- Do not add a JavaScript or CSS dependency.
- Any new What's New text must use short, common words and state how the change helps the person using the app.

---

## File map

- Modify `src/zira_dashboard/templates/_department_dashboard_widgets.html`: render compact horizontal and vertical stint hit areas and remove persistent detail markup.
- Modify `src/zira_dashboard/static/recycling.css`: style stint boundaries, keyboard focus, and the shared popover while retaining the 20-pixel track.
- Create `src/zira_dashboard/static/worker-stint-popover.js`: own the single hover/focus/tap popover and its close/reposition behavior.
- Modify `src/zira_dashboard/templates/new_dept.html`: load the shared popover controller in screen and TV modes.
- Modify `src/zira_dashboard/templates/recycling.html`: load the same controller in screen and TV modes.
- Modify `tests/test_new_dashboard_template.py`: prove compact markup, accessible detail text, finish markers, orientations, TV rendering, and unchanged legacy bars.
- Create `tests/test_worker_stint_popover_static.py`: prove the 20-pixel CSS contract and exercise the interaction controller in Node with a small fake DOM.
- Modify `CHANGELOG.md`: add the completed user-facing improvement under `2026-08-28`.

### Task 1: Deliver the complete compact worker-stint interaction

**Files:**
- Modify: `tests/test_new_dashboard_template.py:254-395`
- Create: `tests/test_worker_stint_popover_static.py`
- Modify: `src/zira_dashboard/templates/_department_dashboard_widgets.html:20-181`
- Modify: `src/zira_dashboard/static/recycling.css:413-512,591-655`
- Create: `src/zira_dashboard/static/worker-stint-popover.js`
- Modify: `src/zira_dashboard/templates/new_dept.html:160-176`
- Modify: `src/zira_dashboard/templates/recycling.html:216-236`
- Modify: `CHANGELOG.md:12-20`

**Interfaces:**
- Consumes: each existing segment dictionary field `person_label`, `time_label`, `actual_units`, `goal_units`, `result`, `result_label`, `start_pct`, `actual_pct`, `shortfall_pct`, `shortfall_start_pct`, `finish_pct`, and `is_active`.
- Produces: `.worker-stint-hitarea` and `.vworker-stint-hitarea` buttons with `data-stint-detail`, `aria-label`, and native `title`; `makeWorkerStintPopover(document, window)` exported through CommonJS for tests and auto-initialized in the browser.

- [ ] **Step 1: Replace the visible-detail expectations with failing compact-markup tests**

In `tests/test_new_dashboard_template.py`, replace the segmented-bar tests from `test_new_horizontal_bar_renders_worker_segments_and_finish_states` through `test_new_vertical_bar_renders_segment_blocks_finish_markers_and_visible_list` with the following behavior-focused assertions. Keep the intervening unsegmented and number-position tests unchanged.

```python
def test_new_horizontal_bar_keeps_segments_and_moves_details_to_hitareas():
    html = _render_new(new_bars=[_segmented_bar()])

    assert 'class="worker-segment-fill result-behind"' in html
    assert 'class="worker-segment-shortfall"' in html
    assert 'class="worker-segment-goal completed"' in html
    assert 'class="worker-segment-goal live"' in html
    assert html.count('type="button" class="worker-stint-hitarea') == 2
    assert 'class="worker-stint-hitarea has-boundary"' in html
    assert 'style="left:0.0%;width:80.0%"' in html
    assert 'style="left:80.0%;width:15.0%"' in html
    assert 'aria-label="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
    assert 'title="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
    assert 'aria-label="Ana M. · since 2:35p · 32/25 · 7 ahead"' in html
    assert 'class="worker-segment-name"' not in html
    assert 'class="worker-segment-labels"' not in html
    assert 'class="bar-target-line"' not in html


def test_stopped_sole_producer_name_is_left_while_finish_marker_stays_in_bar():
    html = _render_new(new_bars=[_stopped_sole_producer_bar()])

    assert '<span class="name-primary">Humberto S.</span>' in html
    assert '<span class="name-secondary">Repair 4</span>' in html
    assert 'class="worker-segment-goal completed"' in html
    assert 'aria-label="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
    assert '<span class="worker-segment-person">Humberto S.</span>' not in html
    assert "No one here now" not in html


def test_multiple_producer_details_are_accessible_but_not_always_visible():
    html = _render_new(new_bars=[_segmented_bar()])

    assert 'aria-label="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
    assert 'aria-label="Ana M. · since 2:35p · 32/25 · 7 ahead"' in html
    assert '<span class="worker-segment-person">' not in html
    assert '<span class="name-primary">Humberto S.</span>' not in html
    assert '<span class="name-primary">Ana M.</span>' not in html


def test_active_multi_producer_row_identifies_work_center_on_left():
    bar = _segmented_bar()
    bar.update(who="Ana M.", no_one_here_now=False)

    html = _render_new(new_bars=[bar])

    assert '<span class="name-primary">Repair 4</span>' in html
    assert 'aria-label="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
    assert 'aria-label="Ana M. · since 2:35p · 32/25 · 7 ahead"' in html
    assert '<span class="name-primary">Ana M.</span>' not in html


def test_vacant_multi_producer_row_keeps_empty_status_on_left():
    html = _render_new(new_bars=[_segmented_bar()])

    assert '<span class="name-primary current-empty">No one here now</span>' in html
    assert 'aria-label="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
    assert 'aria-label="Ana M. · since 2:35p · 32/25 · 7 ahead"' in html


def test_unassigned_stint_uses_a_neutral_accessible_name():
    bar = _segmented_bar()
    bar["segments"][1].update(
        person_name=None,
        person_label="Unassigned production",
        result="neutral",
        result_label="unassigned",
    )

    html = _render_new(new_bars=[bar])

    assert (
        'aria-label="Unassigned production · since 2:35p · 32/25 · unassigned"'
        in html
    )
    assert '<span class="worker-segment-person">Unassigned production</span>' not in html


def test_vertical_and_tv_views_keep_sole_producer_left_without_duplication():
    vertical = _render_new(
        customs={"new-bars": {"orientation": "vertical"}},
        new_bars=[_stopped_sole_producer_bar()],
    )
    tv = _render_new(tv_mode=True, new_bars=[_stopped_sole_producer_bar()])

    for html in (vertical, tv):
        assert '<span class="name-primary">Humberto S.</span>' in html
        assert '<span class="worker-segment-person">Humberto S.</span>' not in html
        assert 'aria-label="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
        assert "worker-segment-goal completed" in html


def test_new_tv_keeps_worker_details_available_without_visible_detail_rows():
    html = _render_new(tv_mode=True, new_bars=[_segmented_bar()])

    assert 'aria-label="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
    assert 'aria-label="Ana M. · since 2:35p · 32/25 · 7 ahead"' in html
    assert 'class="worker-segment-name"' not in html
    assert 'class="worker-segment-labels"' not in html


def test_new_vertical_bar_keeps_geometry_without_visible_worker_list():
    html = _render_new(
        customs={"new-bars": {"orientation": "vertical"}},
        new_bars=[_segmented_bar()],
    )

    assert 'class="vworker-segment-fill result-behind"' in html
    assert 'class="vworker-segment-shortfall"' in html
    assert 'class="vworker-segment-goal completed"' in html
    assert 'class="vworker-segment-goal live"' in html
    assert html.count('type="button" class="vworker-stint-hitarea') == 2
    assert 'class="vworker-stint-hitarea has-boundary"' in html
    assert 'aria-label="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
    assert 'aria-label="Ana M. · since 2:35p · 32/25 · 7 ahead"' in html
    assert 'class="vworker-segment-list"' not in html
```

Create `tests/test_worker_stint_popover_static.py` with the initial CSS contract:

```python
from pathlib import Path
import json
import subprocess


ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "src/zira_dashboard/static/recycling.css").read_text(encoding="utf-8")
SCRIPT = ROOT / "src/zira_dashboard/static/worker-stint-popover.js"


def test_worker_stint_styles_keep_the_normal_bar_height_and_remove_detail_rows():
    assert ".bar-track { height: 20px" in CSS
    assert ".worker-stint-hitarea" in CSS
    assert ".vworker-stint-hitarea" in CSS
    assert ".worker-segment-labels" not in CSS
    assert ".vworker-segment-list" not in CSS
```

- [ ] **Step 2: Run the compact-markup tests and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/pytest -q \
  tests/test_new_dashboard_template.py \
  tests/test_worker_stint_popover_static.py
```

Expected: FAIL because the template still renders visible detail spans/lists, no stint hit-area buttons exist, and the CSS has no hit-area selectors.

- [ ] **Step 3: Implement compact horizontal and vertical hit areas in the shared template**

In the vertical `b.has_segments` loop in `src/zira_dashboard/templates/_department_dashboard_widgets.html`, keep the existing fill, shortfall, and goal blocks unchanged, then add this button before `{% endfor %}`:

```jinja2
                {% set runway_pct = s.actual_pct + s.shortfall_pct %}
                {% if runway_pct > 0 %}
                  <button type="button"
                          class="vworker-stint-hitarea{% if not loop.first %} has-boundary{% endif %}"
                          style="bottom:{{ s.start_pct }}%;height:{{ runway_pct }}%"
                          data-stint-detail="{{ s.person_label }} · {{ s.time_label }} · {{ s.actual_units|round|int }}/{{ s.goal_units|round|int }} · {{ s.result_label }}"
                          aria-label="{{ s.person_label }} · {{ s.time_label }} · {{ s.actual_units|round|int }}/{{ s.goal_units|round|int }} · {{ s.result_label }}"
                          title="{{ s.person_label }} · {{ s.time_label }} · {{ s.actual_units|round|int }}/{{ s.goal_units|round|int }} · {{ s.result_label }}"></button>
                {% endif %}
```

Delete the entire vertical block beginning with `{% if b.has_segments %}<div class="vworker-segment-list">` and ending at its matching `{% endif %}`. Do not alter `.vbar-name` or the station total.

In the horizontal loop, replace the current filled segment block so it contains no `.worker-segment-name` child:

```jinja2
              {% if s.actual_pct > 0 %}
                <div class="worker-segment-fill result-{{ s.result }}"
                     style="left:{{ s.start_pct }}%;width:{{ s.actual_pct }}%"
                     aria-hidden="true"></div>
              {% endif %}
```

Keep the existing horizontal shortfall and finish-goal blocks unchanged, then add this button before `{% endfor %}`:

```jinja2
              {% set runway_pct = s.actual_pct + s.shortfall_pct %}
              {% if runway_pct > 0 %}
                <button type="button"
                        class="worker-stint-hitarea{% if not loop.first %} has-boundary{% endif %}"
                        style="left:{{ s.start_pct }}%;width:{{ runway_pct }}%"
                        data-stint-detail="{{ s.person_label }} · {{ s.time_label }} · {{ s.actual_units|round|int }}/{{ s.goal_units|round|int }} · {{ s.result_label }}"
                        aria-label="{{ s.person_label }} · {{ s.time_label }} · {{ s.actual_units|round|int }}/{{ s.goal_units|round|int }} · {{ s.result_label }}"
                        title="{{ s.person_label }} · {{ s.time_label }} · {{ s.actual_units|round|int }}/{{ s.goal_units|round|int }} · {{ s.result_label }}"></button>
              {% endif %}
```

Delete the entire horizontal `{% if b.has_segments %}<div class="worker-segment-labels">` block. Keep `.segment-total`, `.val`, and the unsegmented `bar-fill` branch unchanged.

- [ ] **Step 4: Replace persistent detail styling with compact hit-area and popover styling**

In `src/zira_dashboard/static/recycling.css`, delete the rules for `.worker-segment-name`, `.worker-segment-name small`, `.worker-segment-labels`, `.worker-segment-callout`, `.worker-segment-callout.result-ahead`, `.worker-segment-callout.result-behind`, `.vworker-segment-list`, `.vworker-segment-list span`, and its result variants. Add these rules after the horizontal and vertical segment geometry rules:

```css
  .worker-stint-hitarea,
  .vworker-stint-hitarea {
    position: absolute;
    z-index: 3;
    box-sizing: border-box;
    margin: 0;
    padding: 0;
    border: 0;
    appearance: none;
    background: transparent;
    cursor: help;
  }
  .worker-stint-hitarea {
    top: 0;
    bottom: 0;
  }
  .worker-stint-hitarea.has-boundary {
    border-left: 1px solid color-mix(in srgb, var(--panel) 72%, transparent);
  }
  .vworker-stint-hitarea {
    left: 0;
    right: 0;
  }
  .vworker-stint-hitarea.has-boundary {
    border-bottom: 1px solid color-mix(in srgb, var(--panel) 72%, transparent);
  }
  .worker-stint-hitarea:focus-visible,
  .vworker-stint-hitarea:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .worker-stint-popover {
    position: absolute;
    z-index: 9600;
    max-width: min(320px, calc(100vw - 16px));
    padding: 0.45rem 0.6rem;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--panel);
    color: var(--fg);
    box-shadow: 0 8px 24px rgb(0 0 0 / 22%);
    font-size: 0.78rem;
    font-variant-numeric: tabular-nums;
    line-height: 1.25;
    pointer-events: none;
    white-space: normal;
  }
  .worker-stint-popover[hidden] {
    display: none;
  }
```

Do not change `.bar-track { height: 20px; ... }`, the fill colors, hatching, or finish-marker rules.

- [ ] **Step 5: Run the compact-markup tests and verify GREEN**

Run the same focused command from Step 2.

Expected: PASS. Confirm the rendered HTML has exactly two focusable stint sections, complete native/accessibility detail text, no persistent detail row, and the unchanged finish markers.

- [ ] **Step 6: Add the failing browser-controller and script-inclusion tests**

Append this test to `tests/test_worker_stint_popover_static.py`:

```python
def test_worker_stint_popover_supports_hover_focus_tap_outside_and_escape():
    harness = f"""
const makeController = require({json.dumps(str(SCRIPT))});
const documentListeners = {{}};
const windowListeners = {{}};
let appended = null;
let focusCount = 0;
const popover = {{
  hidden: true,
  style: {{}},
  textContent: '',
  id: '',
  className: '',
  setAttribute(name, value) {{ this[name] = value; }},
  contains(node) {{ return node === this; }},
  getBoundingClientRect() {{ return {{width: 180, height: 30}}; }},
}};
const document = {{
  body: {{appendChild(node) {{ appended = node; }}}},
  createElement() {{ return popover; }},
  addEventListener(type, callback) {{ documentListeners[type] = callback; }},
}};
const windowObject = {{
  innerWidth: 800,
  innerHeight: 600,
  scrollX: 0,
  scrollY: 0,
  addEventListener(type, callback) {{ windowListeners[type] = callback; }},
}};
function trigger(detail, left) {{
  const attributes = {{}};
  return {{
    dataset: {{stintDetail: detail}},
    closest(selector) {{ return selector.includes('worker-stint') ? this : null; }},
    contains(node) {{ return node === this; }},
    getBoundingClientRect() {{ return {{left, right: left + 80, top: 100, bottom: 120, width: 80}}; }},
    setAttribute(name, value) {{ attributes[name] = value; }},
    removeAttribute(name) {{ delete attributes[name]; }},
    getAttribute(name) {{ return attributes[name]; }},
    focus() {{ focusCount += 1; }},
  }};
}}
const first = trigger('Humberto S. · 7a-2:33p · 516/700 · 184 behind', 100);
const second = trigger('Ana M. · since 2:35p · 32/25 · 7 ahead', 180);
const outside = {{closest() {{ return null; }}}};
const event = (target) => ({{target, preventDefault() {{}}}});
const controller = makeController(document, windowObject);
controller.init();

documentListeners.pointerover(event(first));
if (popover.hidden || popover.textContent !== first.dataset.stintDetail) throw new Error('hover did not open');
if (first.getAttribute('aria-describedby') !== 'worker-stint-popover') throw new Error('missing description link');
documentListeners.pointerout({{target: first, relatedTarget: outside}});
if (!popover.hidden) throw new Error('hover leave did not close');

documentListeners.focusin(event(first));
if (popover.hidden) throw new Error('focus did not open');
documentListeners.focusout({{target: first, relatedTarget: outside}});
if (!popover.hidden) throw new Error('focus leave did not close');

documentListeners.click(event(first));
documentListeners.pointerout({{target: first, relatedTarget: outside}});
if (popover.hidden) throw new Error('tap did not pin');
documentListeners.click(event(second));
if (popover.textContent !== second.dataset.stintDetail) throw new Error('second tap did not replace details');
documentListeners.pointerdown(event(outside));
if (!popover.hidden) throw new Error('outside tap did not close');

documentListeners.click(event(first));
documentListeners.keydown({{key: 'Escape'}});
if (!popover.hidden || focusCount !== 1) throw new Error('Escape did not close and restore focus');
if (!appended || appended.id !== 'worker-stint-popover') throw new Error('shared popover was not created');
"""
    result = subprocess.run(
        ["node", "--eval", harness],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
```

In `tests/test_new_dashboard_template.py`, add `/static/worker-stint-popover.js` to the `surface` tuple in `test_new_keeps_shared_dashboard_surfaces_and_refresh_behavior`, then add:

```python
def test_recycling_and_new_load_worker_stint_details_in_screen_and_tv_modes():
    recycling = (
        ROOT / "src/zira_dashboard/templates/recycling.html"
    ).read_text(encoding="utf-8")
    new_source = _html()
    new_tv = _render_new(tv_mode=True, new_bars=[_segmented_bar()])

    for html in (recycling, new_source, new_tv):
        assert "/static/worker-stint-popover.js" in html
```

- [ ] **Step 7: Run the interaction tests and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/pytest -q \
  tests/test_new_dashboard_template.py::test_recycling_and_new_load_worker_stint_details_in_screen_and_tv_modes \
  tests/test_worker_stint_popover_static.py::test_worker_stint_popover_supports_hover_focus_tap_outside_and_escape
```

Expected: FAIL because `worker-stint-popover.js` does not exist and neither dashboard loads it.

- [ ] **Step 8: Create the dependency-free shared popover controller**

Create `src/zira_dashboard/static/worker-stint-popover.js` with:

```javascript
/* Compact worker-stint details shared by Recycling and New dashboards. */
(function (root, makeWorkerStintPopover) {
  "use strict";
  if (typeof module === 'object' && module.exports) {
    module.exports = makeWorkerStintPopover;
    return;
  }
  makeWorkerStintPopover(root.document, root).init();
})(typeof window !== 'undefined' ? window : globalThis, function (document, windowObject) {
  "use strict";
  var selector = '.worker-stint-hitarea, .vworker-stint-hitarea';
  var active = null;
  var pinned = false;
  var popover = null;

  function triggerFrom(target) {
    return target && target.closest ? target.closest(selector) : null;
  }

  function ensurePopover() {
    if (popover) return popover;
    popover = document.createElement('div');
    popover.id = 'worker-stint-popover';
    popover.className = 'worker-stint-popover';
    popover.setAttribute('role', 'tooltip');
    popover.hidden = true;
    document.body.appendChild(popover);
    return popover;
  }

  function position(trigger) {
    var box = trigger.getBoundingClientRect();
    var tip = popover.getBoundingClientRect();
    var minLeft = windowObject.scrollX + 8;
    var maxLeft = windowObject.scrollX + windowObject.innerWidth - tip.width - 8;
    var left = box.left + windowObject.scrollX + (box.width / 2) - (tip.width / 2);
    var top = box.top + windowObject.scrollY - tip.height - 8;
    left = Math.max(minLeft, Math.min(left, maxLeft));
    if (top < windowObject.scrollY + 8) top = box.bottom + windowObject.scrollY + 8;
    popover.style.left = left + 'px';
    popover.style.top = top + 'px';
  }

  function open(trigger, shouldPin) {
    var tip = ensurePopover();
    if (active && active !== trigger) active.removeAttribute('aria-describedby');
    active = trigger;
    pinned = Boolean(shouldPin);
    tip.textContent = trigger.dataset.stintDetail;
    tip.hidden = false;
    trigger.setAttribute('aria-describedby', tip.id);
    position(trigger);
  }

  function close(returnFocus) {
    var previous = active;
    if (previous) previous.removeAttribute('aria-describedby');
    if (popover) popover.hidden = true;
    active = null;
    pinned = false;
    if (returnFocus && previous && previous.focus) previous.focus();
  }

  function leftTrigger(trigger, relatedTarget) {
    return relatedTarget !== trigger
      && !(trigger.contains && trigger.contains(relatedTarget));
  }

  function init() {
    document.addEventListener('pointerover', function (event) {
      var trigger = triggerFrom(event.target);
      if (trigger && !pinned) open(trigger, false);
    });
    document.addEventListener('pointerout', function (event) {
      var trigger = triggerFrom(event.target);
      if (trigger && trigger === active && !pinned && leftTrigger(trigger, event.relatedTarget)) close(false);
    });
    document.addEventListener('focusin', function (event) {
      var trigger = triggerFrom(event.target);
      if (trigger) open(trigger, false);
    });
    document.addEventListener('focusout', function (event) {
      var trigger = triggerFrom(event.target);
      if (trigger && trigger === active && !pinned && leftTrigger(trigger, event.relatedTarget)) close(false);
    });
    document.addEventListener('click', function (event) {
      var trigger = triggerFrom(event.target);
      if (!trigger) return;
      event.preventDefault();
      if (trigger === active && pinned) close(false);
      else open(trigger, true);
    });
    document.addEventListener('pointerdown', function (event) {
      if (!active || triggerFrom(event.target)) return;
      if (popover && popover.contains(event.target)) return;
      close(false);
    }, true);
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && active) close(true);
    });
    windowObject.addEventListener('resize', function () {
      if (active) position(active);
    });
    windowObject.addEventListener('scroll', function () {
      if (active) position(active);
    }, true);
  }

  return {init: init, open: open, close: close};
});
```

- [ ] **Step 9: Load the controller once on both dashboard families**

In the `{% block scripts %}` of both `src/zira_dashboard/templates/new_dept.html` and `src/zira_dashboard/templates/recycling.html`, add this line immediately after `dashboard-grid.js`, outside every `tv_mode` condition:

```jinja2
<script src="/static/worker-stint-popover.js?v={{ static_v('worker-stint-popover.js') }}"></script>
```

This must load on screen and TV pages. Keep `assign-popover.js` screen-only and keep `tv-refresh.js` conditions unchanged.

- [ ] **Step 10: Run the interaction tests and verify GREEN**

Run the command from Step 7.

Expected: PASS. The Node harness must prove hover preview, keyboard preview, pinned tap, replacement by another tap, outside close, Escape close with focus restoration, and one shared popover.

- [ ] **Step 11: Add the plain-language completed change note**

Under `## 2026-08-28` at the top of `CHANGELOG.md`, add:

```markdown
### Worker times fit in one short bar

- **Production bars now keep each person's work time in one normal-size bar.** Point to, select, or tap a section to see the person's name, time, goal, and result without filling the screen with extra lines.
```

Keep the earlier design and plan notes unchanged.

- [ ] **Step 12: Run the full focused regression and quality checks**

Run:

```bash
DATABASE_URL= .venv/bin/pytest -q \
  tests/test_new_dashboard_template.py \
  tests/test_worker_stint_popover_static.py \
  tests/test_recycling_data.py \
  tests/test_production_segments.py
.venv/bin/ruff check \
  tests/test_new_dashboard_template.py \
  tests/test_worker_stint_popover_static.py
git diff --check
```

Expected: all pytest tests PASS, Ruff reports `All checks passed!`, and `git diff --check` prints nothing. Review the rendered assertions one final time: horizontal and vertical segment geometry and finish lines remain, no visible detail rows remain, TV markup stays compact, unsegmented bars keep their old fill and target line, and both pages load the controller.

- [ ] **Step 13: Commit and push the complete feature**

First confirm only the intended implementation files plus pre-existing user changes are present:

```bash
git status --short
git diff -- \
  CHANGELOG.md \
  src/zira_dashboard/templates/_department_dashboard_widgets.html \
  src/zira_dashboard/static/recycling.css \
  src/zira_dashboard/static/worker-stint-popover.js \
  src/zira_dashboard/templates/new_dept.html \
  src/zira_dashboard/templates/recycling.html \
  tests/test_new_dashboard_template.py \
  tests/test_worker_stint_popover_static.py
```

Stage only those files, commit once so `main` never contains a half-finished touch interaction, and push immediately:

```bash
git add \
  CHANGELOG.md \
  src/zira_dashboard/templates/_department_dashboard_widgets.html \
  src/zira_dashboard/static/recycling.css \
  src/zira_dashboard/static/worker-stint-popover.js \
  src/zira_dashboard/templates/new_dept.html \
  src/zira_dashboard/templates/recycling.html \
  tests/test_new_dashboard_template.py \
  tests/test_worker_stint_popover_static.py
git commit -m "feat: compact worker stint bars"
git push origin main
```

Expected: the commit and push succeed, `main` and `origin/main` point to the same implementation commit, and unrelated user files remain untouched.
