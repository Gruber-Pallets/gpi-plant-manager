# People Performance Compact Header Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the People dashboard's large repeated heading and hourly axis with one sticky manager strip and day-specific schedule markers inside every green section header.

**Architecture:** Keep schedule geometry in `people_performance_view.py`, where the existing timeline percentages are calculated, and expose deterministic marker ticks plus collision-safe visible label groups to Jinja. Move every day-level value and control into the live partial so polling refreshes the whole manager strip, use one delegated JavaScript change listener for filter submission, and keep horizontal-scroll synchronization shared by section headers and person timelines.

**Tech Stack:** Python 3.11, FastAPI, Jinja2, vanilla JavaScript, CSS, pytest, Node-based controller tests, and Playwright preview tests.

## Global Constraints

- Do not repeat “People performance,” “Today,” or a separate page heading below the existing People tab.
- Keep one sticky manager strip no taller than the existing 2.75rem controls on desktop.
- The strip contains working-now, worked-earlier, and needs-attention counts; conditional source warnings; last-updated time; selected date; Needs attention filter; and the Today shortcut when a different date is selected.
- Remove the Apply button. Date and Needs attention changes submit directly.
- Replace the hourly axis with shift start, every configured break start, and shift end in each green section header.
- Resolve marker times from the selected day's schedule data already present in `DashboardModel`; never hard-code weekday times.
- Keep section order, person rows, calculations, attention rules, timeline intervals, break shading, summaries, and source behavior unchanged.
- Preserve polling state: date, filter, horizontal scroll, keyboard focus, and open interval detail.
- At narrow widths the manager strip uses no more than two compact rows; warnings scroll horizontally inside their own region and controls remain reachable.
- New `CHANGELOG.md` copy must use short, common words and explain how the change helps the manager.

## File map

- `src/zira_dashboard/people_performance_view.py`: build unique schedule ticks and collision-safe visible label groups from the model's shift window and breaks.
- `src/zira_dashboard/templates/people_performance.html`: remove the redundant toolbar and leave the page shell plus live partial.
- `src/zira_dashboard/templates/_people_performance_rows.html`: render the sticky manager strip and the schedule rail inside every section header.
- `src/zira_dashboard/static/people-performance.js`: auto-submit date/filter changes with one delegated listener while preserving the existing live-refresh controller.
- `src/zira_dashboard/static/people-performance.css`: implement the thin sticky strip, three-column green header, time ticks, label groups, and two-row responsive behavior.
- `scripts/preview_people_performance.py`: replace preview-only hourly labels with the same schedule marker context expected by the templates.
- `tests/test_people_performance_view.py`: cover marker selection, deduplication, positions, accessibility labels, and close-time grouping.
- `tests/test_people_performance_template.py`: cover the simplified document structure and manager-strip content.
- `tests/test_people_performance_static.py`: cover auto-submit behavior and the CSS/template contracts.
- `tests/test_preview_people_performance.py`: verify sticky geometry, local overflow, aligned section headers, and non-overlapping visible time groups at manager viewports.
- `CHANGELOG.md`: add the plain-language shipped note only after all implementation checks pass.

---

### Task 1: Present schedule ticks and collision-safe time groups

**Files:**
- Modify: `src/zira_dashboard/people_performance_view.py:17-35,285-326`
- Test: `tests/test_people_performance_view.py:1-40`

**Interfaces:**
- Consumes: `DashboardModel.window_start_utc: datetime`, `DashboardModel.window_end_utc: datetime`, and `DashboardModel.breaks: tuple[BreakSpan, ...]`.
- Produces: `dashboard_context(...)["schedule_markers"]: tuple[dict, ...]` and `dashboard_context(...)["schedule_time_groups"]: tuple[dict, ...]`. A marker dictionary has `left_pct`, `kind`, and `aria_label`; a group dictionary has `left_pct`, `label`, and `edge`.

- [ ] **Step 1: Replace the hourly-axis presenter test with schedule-marker tests**

Import `timedelta` from `datetime`, then add coverage using the existing `replace`, `START`, `END`, and `BreakSpan` fixtures:

```python
def test_presenter_uses_only_shift_and_break_start_markers():
    model = replace(
        busy_dashboard_model(),
        breaks=(
            BreakSpan(START + timedelta(hours=2), START + timedelta(hours=2, minutes=15), "Morning break"),
            BreakSpan(START + timedelta(hours=4), START + timedelta(hours=4, minutes=30), "Lunch"),
            BreakSpan(END - timedelta(minutes=15), END, "Cleanup"),
        ),
    )

    context = dashboard_context(model)

    assert [(item["kind"], item["left_pct"]) for item in context["schedule_markers"]] == [
        ("start", 0.0),
        ("break", 25.0),
        ("break", 50.0),
        ("break", 96.875),
        ("end", 100.0),
    ]
    assert [item["label"] for item in context["schedule_time_groups"]] == [
        "6:00 AM",
        "8:00",
        "10:00",
        "1:45 · 2:00 PM",
    ]
    assert "Cleanup starts at 1:45 PM" in context["schedule_markers"][-2]["aria_label"]


def test_schedule_markers_deduplicate_equal_break_and_shift_end_times():
    model = replace(
        busy_dashboard_model(),
        breaks=(BreakSpan(END, END, "End marker"),),
    )

    context = dashboard_context(model)

    assert [item["left_pct"] for item in context["schedule_markers"]].count(100.0) == 1
    assert context["schedule_time_groups"][-1]["edge"] == "end"


def test_schedule_without_breaks_has_only_shift_boundaries():
    context = dashboard_context(replace(busy_dashboard_model(), breaks=()))

    assert [item["kind"] for item in context["schedule_markers"]] == ["start", "end"]
    assert [item["label"] for item in context["schedule_time_groups"]] == [
        "6:00 AM",
        "2:00 PM",
    ]
```

Also retain the short-interval assertions from the replaced test as their own `test_presenter_preserves_short_intervals` test so this UI change cannot weaken row geometry coverage.

- [ ] **Step 2: Run the focused presenter tests and verify they fail**

Run:

```bash
pytest tests/test_people_performance_view.py::test_presenter_uses_only_shift_and_break_start_markers tests/test_people_performance_view.py::test_schedule_markers_deduplicate_equal_break_and_shift_end_times -q
```

Expected: FAIL because `schedule_markers` and `schedule_time_groups` are not in the context.

- [ ] **Step 3: Add the schedule-marker helpers and replace `axis_labels`**

Add the following focused helpers near `_time` in `people_performance_view.py`:

```python
_MIN_TIME_LABEL_GAP_PCT = 8.0


def _break_time(value: datetime) -> str:
    return value.astimezone(shift_config.SITE_TZ).strftime("%-I:%M")


def _schedule_markers(model: DashboardModel) -> tuple[dict, ...]:
    candidates = [
        (model.window_start_utc, "start", "Shift starts"),
        *(
            (item.start_utc, "break", f"{item.label} starts")
            for item in model.breaks
        ),
        (model.window_end_utc, "end", "Shift ends"),
    ]
    markers = []
    seen = set()
    for value, kind, description in sorted(
        candidates,
        key=lambda item: (item[0], item[1] == "break"),
    ):
        clamped = min(max(value, model.window_start_utc), model.window_end_utc)
        if clamped in seen:
            continue
        seen.add(clamped)
        full_time = _time(clamped)
        markers.append(
            {
                "left_pct": _pct(clamped, model.window_start_utc, model.window_end_utc),
                "kind": kind,
                "visible_label": full_time if kind != "break" else _break_time(clamped),
                "aria_label": f"{description} at {full_time}",
            }
        )
    return tuple(markers)


def _schedule_time_groups(markers: tuple[dict, ...]) -> tuple[dict, ...]:
    groups: list[list[dict]] = []
    for marker in markers:
        if groups and marker["left_pct"] - groups[-1][-1]["left_pct"] < _MIN_TIME_LABEL_GAP_PCT:
            groups[-1].append(marker)
        else:
            groups.append([marker])
    return tuple(
        {
            "left_pct": sum(item["left_pct"] for item in group) / len(group),
            "label": " · ".join(item["visible_label"] for item in group),
            "edge": (
                "start" if group[0]["left_pct"] == 0.0
                else "end" if group[-1]["left_pct"] == 100.0
                else "middle"
            ),
        }
        for group in groups
    )
```

In `dashboard_context`, delete the `total_minutes`, `axis_step`, and `axis_labels` block. Add `schedule_markers = _schedule_markers(model)` immediately before the existing return dictionary, then replace the old `"axis_labels": tuple(axis_labels),` entry with these exact entries:

```python
        "schedule_markers": schedule_markers,
        "schedule_time_groups": _schedule_time_groups(schedule_markers),
```

Keep the existing section and row construction exactly as-is.

- [ ] **Step 4: Run presenter coverage**

Run:

```bash
pytest tests/test_people_performance_view.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the presenter unit**

```bash
git add src/zira_dashboard/people_performance_view.py tests/test_people_performance_view.py
git commit -m "feat: present people schedule markers"
```

---

### Task 2: Render one live manager strip and times in every green header

**Files:**
- Modify: `src/zira_dashboard/templates/people_performance.html:8-35`
- Modify: `src/zira_dashboard/templates/_people_performance_rows.html:1-36`
- Test: `tests/test_people_performance_template.py:25-95`

**Interfaces:**
- Consumes: the existing count, warning, date, filter, freshness, and section context plus Task 1's `schedule_markers` and `schedule_time_groups` tuples.
- Produces: `.pp-manager-strip`, `[data-pp-auto-submit]`, `.pp-section-header`, `.pp-schedule-viewport`, `.pp-schedule-track`, `.pp-schedule-tick`, and `.pp-schedule-time-group` DOM contracts used by Tasks 3-5.

- [ ] **Step 1: Write failing template structure and content assertions**

Extend `tests/test_people_performance_template.py`:

```python
def test_page_uses_one_compact_live_manager_strip(rendered_html):
    assert 'class="pp-manager-strip"' in rendered_html
    assert 'id="pp-live-status"' in rendered_html
    assert '<strong>5</strong> working now' in rendered_html
    assert '<strong>1</strong> worked earlier' in rendered_html
    assert '<strong>4</strong> need attention' in rendered_html
    assert "Forklift data unavailable" in rendered_html
    assert 'name="day"' in rendered_html
    assert 'name="attention"' in rendered_html
    assert 'data-pp-auto-submit' in rendered_html
    assert '>Apply<' not in rendered_html
    assert '>Today<' not in rendered_html


def test_manager_strip_omits_warning_region_when_sources_are_healthy(client, monkeypatch):
    monkeypatch.setattr(
        route,
        "load_dashboard",
        lambda day, client, now_utc=None: replace(
            busy_dashboard_model(), day=day, source_warnings=()
        ),
    )

    response = client.get(f"/people-performance?day={DAY.isoformat()}")

    assert response.status_code == 200
    assert 'class="pp-source-warnings"' not in response.text


def test_historical_manager_strip_offers_today_shortcut(client, monkeypatch):
    historical = DAY - timedelta(days=1)
    monkeypatch.setattr(
        route,
        "load_dashboard",
        lambda day, client, now_utc=None: replace(
            busy_dashboard_model(), day=day, is_today=False
        ),
    )

    response = client.get(f"/people-performance?day={historical.isoformat()}")

    assert response.status_code == 200
    assert '<a href="/people-performance">Today</a>' in response.text


def test_page_does_not_repeat_tab_identity_or_render_hourly_axis(rendered_html):
    assert 'class="pp-toolbar"' not in rendered_html
    assert 'class="pp-eyebrow"' not in rendered_html
    assert '<h1>' not in rendered_html
    assert 'class="pp-axis"' not in rendered_html


def test_each_green_section_header_contains_schedule_ticks_and_summary(rendered_html):
    assert rendered_html.count('class="pp-section-header"') == 3
    assert rendered_html.count('class="pp-schedule-tick ') == 9
    assert rendered_html.count('class="pp-schedule-time-group ') == 9
    assert rendered_html.count('class="pp-section-summary"') == 3
    assert "Shift starts at 6:00 AM" in rendered_html
    assert "Planned break starts at 10:30 AM" in rendered_html
    assert "Shift ends at 2:00 PM" in rendered_html
```

Import `timedelta` from `datetime` at the top of the template test module.

Use the fixture's one break, so each of three section headers has three ticks and three visible time groups.

- [ ] **Step 2: Run the template tests and verify they fail**

Run:

```bash
pytest tests/test_people_performance_template.py -q
```

Expected: FAIL because the toolbar and hourly axis still render and the new manager/header classes do not exist.

- [ ] **Step 3: Remove the redundant full-page toolbar**

Reduce the body of `people_performance.html` to the page shell and live partial:

```html
<section class="pp-page"
         data-day="{{ day }}"
         data-today="{{ 1 if is_today else 0 }}"
         data-is-today="{{ 1 if is_today else 0 }}"
         data-poll-disabled="{{ 1 if poll_disabled else 0 }}"
         data-rows-url="{{ rows_url }}">
  {% include "_people_performance_rows.html" %}
</section>
```

Keep the title block value, stylesheet, body attributes, and script includes unchanged because they support the browser tab, application shell, and polling.

- [ ] **Step 4: Move all day-level detail and controls into the live partial**

Replace the separate counts, warnings, and axis at the top of `_people_performance_rows.html` with:

```html
  <form class="pp-manager-strip" action="/people-performance" method="get" aria-label="People dashboard controls">
    <div class="pp-counts" aria-label="Day totals">
      <span><strong>{{ working_now }}</strong> working now</span>
      <span><strong>{{ worked_earlier }}</strong> worked earlier</span>
      <span><strong>{{ needs_attention }}</strong> need attention</span>
    </div>
    {% if source_warnings %}
    <aside class="pp-source-warnings" role="status" aria-label="Source warnings">
      {% for warning in source_warnings %}<span>{{ warning }}</span>{% endfor %}
    </aside>
    {% endif %}
    <p class="pp-updated" id="pp-live-status" aria-live="polite">Updated {{ as_of }}</p>
    <div class="pp-controls">
      <label class="pp-date-control">
        <span class="sr-only">Date</span>
        <input data-pp-auto-submit type="date" name="day" value="{{ day }}" max="{{ today }}">
      </label>
      <label class="pp-check">
        <input data-pp-auto-submit type="checkbox" name="attention" value="1"{% if attention_only %} checked{% endif %}>
        Needs attention
      </label>
      {% if not is_today %}<a href="/people-performance">Today</a>{% endif %}
    </div>
  </form>
```

Keep the form inside `#people-performance-live` so every successful polling replacement refreshes counts, warnings, and freshness together.

- [ ] **Step 5: Replace each section heading with a three-column schedule header**

Inside the section loop, replace the `<h2>` with:

```html
      <header class="pp-section-header">
        <h2>{{ section.label }} <span>{{ section.rows|length }}</span></h2>
        <div class="pp-schedule-viewport pp-horizontal-scroll">
          <div class="pp-schedule-track" aria-label="Workday schedule">
            {% for marker in schedule_markers %}
            <i class="pp-schedule-tick pp-schedule-tick-{{ marker.kind }}"
               style="left:{{ marker.left_pct }}%">
              <span class="sr-only">{{ marker.aria_label }}</span>
            </i>
            {% endfor %}
            {% for group in schedule_time_groups %}
            <span class="pp-schedule-time-group is-{{ group.edge }}"
                  style="left:{{ group.left_pct }}%">{{ group.label }}</span>
            {% endfor %}
          </div>
        </div>
        <span class="pp-section-summary">Summary</span>
      </header>
```

Do not change the empty state or any person-row markup.

- [ ] **Step 6: Run template and route coverage**

Run:

```bash
pytest tests/test_people_performance_template.py tests/test_people_performance_route.py tests/test_people_performance_end_to_end.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit the template unit**

```bash
git add src/zira_dashboard/templates/people_performance.html src/zira_dashboard/templates/_people_performance_rows.html tests/test_people_performance_template.py
git commit -m "feat: simplify people dashboard header"
```

---

### Task 3: Auto-submit date and attention controls safely

**Files:**
- Modify: `src/zira_dashboard/static/people-performance.js:1-30,500-540`
- Test: `tests/test_people_performance_static.py:90-980`

**Interfaces:**
- Consumes: Task 2's controls marked with `[data-pp-auto-submit]` inside a GET form.
- Produces: delegated `change` handling that calls `form.requestSubmit()` with a `form.submit()` fallback; no custom query-string builder and no change to the polling response contract.

- [ ] **Step 1: Extend the Node controller harness with a filter form**

In `makeEnvironment`, add:

```javascript
          const filterSubmissions = [];
          const filterForm = {
            requestSubmit() { filterSubmissions.push('requestSubmit'); },
            submit() { filterSubmissions.push('submit'); },
          };
          const filterControl = {
            form: filterForm,
            closest(selector) {
              return selector === '[data-pp-auto-submit]' ? this : null;
            },
          };
```

Return `filterSubmissions` and `filterControl` from the harness. Add this runtime assertion after controller initialization:

```javascript
          env.document.emit('change', {target: env.filterControl});
          if (JSON.stringify(env.filterSubmissions) !== JSON.stringify(['requestSubmit'])) {
            throw new Error('filter change did not submit exactly once');
          }
```

Also assert after `controller.destroy()` that a later `change` event does not add another submission. This verifies delegated listeners are removed with the rest of the controller.

- [ ] **Step 2: Run the controller test and verify it fails**

Run:

```bash
pytest tests/test_people_performance_static.py::test_controller_runtime_handles_details_races_navigation_and_teardown -q
```

Expected: FAIL with `filter change did not submit exactly once`.

- [ ] **Step 3: Add the delegated filter handler**

Add near the controller's other small event helpers:

```javascript
  function onFilterChange(event) {
    var target = event && event.target;
    var control = target && target.closest
      ? target.closest("[data-pp-auto-submit]")
      : null;
    if (!control || !control.form) return;
    if (typeof control.form.requestSubmit === "function") control.form.requestSubmit();
    else if (typeof control.form.submit === "function") control.form.submit();
  }
```

Register it in `init()` with the controller's existing listener tracker:

```javascript
    listen(document, "change", onFilterChange);
```

Because the listener is delegated from `document`, it automatically applies to the replacement form after a live refresh. Do not add inline JavaScript to the template.

- [ ] **Step 4: Update the static source contract**

In `test_refresh_source_contract_places_capture_immediately_before_replacement`, add `data-pp-auto-submit` and `requestSubmit` to the required source tokens. Keep every existing refresh, auth, race, focus, and teardown assertion.

- [ ] **Step 5: Run all browser-controller tests**

Run:

```bash
pytest tests/test_people_performance_static.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the interaction unit**

```bash
git add src/zira_dashboard/static/people-performance.js tests/test_people_performance_static.py
git commit -m "feat: apply people filters on change"
```

---

### Task 4: Style the compact strip and aligned schedule headers

**Files:**
- Modify: `src/zira_dashboard/static/people-performance.css:10-210,530-590`
- Test: `tests/test_people_performance_static.py:15-95`

**Interfaces:**
- Consumes: Task 2's manager-strip and section-header classes plus the existing `.pp-row` and `.pp-horizontal-scroll` contracts.
- Produces: one 2.75rem desktop manager strip, at most two compact responsive rows, locally scrolling warnings, and section time rails whose column widths and horizontal scroll match person timelines.

- [ ] **Step 1: Replace the old sticky-axis static test with compact-header contracts**

Replace `test_sticky_axis_is_outside_horizontal_overflow_and_scroll_is_local` with:

```python
def test_manager_strip_is_sticky_and_overflow_stays_local():
    css = CSS_PATH.read_text(encoding="utf-8")
    strip = re.search(r"\.pp-manager-strip\s*\{([^}]*)\}", css)
    warnings = re.search(r"\.pp-source-warnings\s*\{([^}]*)\}", css)
    horizontal = re.search(r"\.pp-horizontal-scroll\s*\{([^}]*)\}", css)

    assert strip
    assert "position: sticky" in strip.group(1)
    assert "min-height: 2.75rem" in strip.group(1)
    assert "overflow" not in strip.group(1)
    assert warnings and "overflow-x: auto" in warnings.group(1)
    assert horizontal and "overflow-x: auto" in horizontal.group(1)
    assert ".pp-page { overflow-x" not in css


def test_section_headers_share_row_columns_and_time_tracks():
    css = CSS_PATH.read_text(encoding="utf-8")
    assert ".pp-section-header," in css
    assert ".pp-row" in css
    assert ".pp-schedule-track," in css
    assert ".pp-timeline" in css
    assert ".pp-schedule-time-group.is-start" in css
    assert ".pp-schedule-time-group.is-end" in css
```

Update `test_short_intervals_have_a_separate_nonoverlapping_touch_target` to require `pp-schedule-viewport pp-horizontal-scroll` and remove its assertion for the deleted axis viewport.

- [ ] **Step 2: Run the focused CSS contracts and verify they fail**

Run:

```bash
pytest tests/test_people_performance_static.py::test_manager_strip_is_sticky_and_overflow_stays_local tests/test_people_performance_static.py::test_section_headers_share_row_columns_and_time_tracks tests/test_people_performance_static.py::test_short_intervals_have_a_separate_nonoverlapping_touch_target -q
```

Expected: FAIL because the old toolbar/axis rules still exist.

- [ ] **Step 3: Replace toolbar, count-row, warning-row, and axis styles**

Delete `.pp-toolbar`, `.pp-eyebrow`, `.pp-toolbar h1`, `.pp-axis`, `.pp-axis-track`, and `.pp-axis-summary` rules. Keep `.pp-updated`, `.pp-controls`, `.pp-counts`, and `.pp-source-warnings`, but restyle them under the manager strip:

```css
.pp-manager-strip {
  position: sticky;
  top: 0;
  z-index: 30;
  display: flex;
  min-height: 2.75rem;
  align-items: center;
  gap: .45rem;
  padding: .35rem .5rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: color-mix(in srgb, var(--panel) 96%, transparent);
  backdrop-filter: blur(8px);
}

.pp-counts,
.pp-source-warnings,
.pp-controls {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: .4rem;
  margin: 0;
}

.pp-source-warnings {
  overflow-x: auto;
  overscroll-behavior-x: contain;
  scrollbar-width: thin;
}

.pp-source-warnings span,
.pp-counts span {
  flex: 0 0 auto;
  padding: .25rem .48rem;
  white-space: nowrap;
}

.pp-updated {
  flex: 0 0 auto;
  margin: 0 0 0 auto;
  white-space: nowrap;
}

.pp-controls {
  flex: 0 0 auto;
}

.pp-controls input[type="date"],
.pp-controls a {
  min-height: 2rem;
}
```

Retain the current amber warning palette, control borders, font minimums, and visible focus treatment.

- [ ] **Step 4: Align the green section header with person rows**

Make the shared grid contract explicit:

```css
.pp-section-header,
.pp-row {
  display: grid;
  grid-template-columns: minmax(10.5rem, .85fr) minmax(0, 4fr) minmax(16rem, 1.35fr);
  gap: .75rem;
}

.pp-section-header {
  min-height: 2.75rem;
  align-items: stretch;
  padding: 0 .75rem;
  border-radius: 8px 8px 0 0;
  background: color-mix(in srgb, var(--panel) 86%, var(--accent) 14%);
}

.pp-section-header h2,
.pp-section-summary {
  display: flex;
  align-items: center;
  margin: 0;
}

.pp-section-header h2 span {
  margin-left: .45rem;
  color: var(--muted);
  font-size: .75rem;
}

.pp-section-summary {
  color: var(--muted);
  font-size: .75rem;
  font-weight: 700;
  text-transform: uppercase;
}

.pp-schedule-track,
.pp-timeline {
  width: 100%;
  min-width: 34rem;
}

.pp-schedule-track {
  position: relative;
  min-height: 2.75rem;
}

.pp-schedule-tick {
  position: absolute;
  inset: auto auto 0;
  height: .45rem;
  border-left: 1px solid color-mix(in srgb, var(--muted) 65%, transparent);
}

.pp-schedule-time-group {
  position: absolute;
  top: 50%;
  color: var(--muted);
  font-size: .75rem;
  font-style: normal;
  font-weight: 700;
  white-space: nowrap;
  transform: translate(-50%, -50%);
}

.pp-schedule-time-group.is-start { transform: translate(0, -50%); }
.pp-schedule-time-group.is-end { transform: translate(-100%, -50%); }
```

Keep `.pp-horizontal-scroll` unchanged so the existing controller synchronizes every section schedule rail with every person timeline.

- [ ] **Step 5: Define the two-row responsive contract**

At `max-width: 1100px`, update the shared grid selector from `.pp-axis, .pp-row` to `.pp-section-header, .pp-row`, hide `.pp-section-summary`, and keep summaries on the timeline column. At `max-width: 760px`, use:

```css
  .pp-manager-strip {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .pp-counts,
  .pp-source-warnings {
    grid-column: 1;
  }

  .pp-updated,
  .pp-controls {
    grid-column: 2;
  }

  .pp-section-header,
  .pp-row {
    grid-template-columns: 10rem minmax(0, 1fr);
    gap: .5rem;
  }
```

Use `grid-row: 1` for counts/updated time and `grid-row: 2` for warnings/controls. When no warnings exist, let controls occupy the second row without leaving an empty warning cell.

- [ ] **Step 6: Run static and template coverage**

Run:

```bash
pytest tests/test_people_performance_static.py tests/test_people_performance_template.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit the styling unit**

```bash
git add src/zira_dashboard/static/people-performance.css tests/test_people_performance_static.py
git commit -m "style: compact people dashboard controls"
```

---

### Task 5: Update the preview, perform visual verification, and document the shipped change

**Files:**
- Modify: `scripts/preview_people_performance.py:475-520`
- Modify: `tests/test_preview_people_performance.py:35-170`
- Modify: `CHANGELOG.md:1-20`

**Interfaces:**
- Consumes: Task 1's `schedule_markers`/`schedule_time_groups` context shape and Tasks 2-4's DOM/CSS contracts.
- Produces: a deterministic preview with start, break-start, and end markers; Playwright coverage at `1440x900`, `1024x768`, and `768x1024`; and user-facing release copy.

- [ ] **Step 1: Update preview expectations before changing its fixture**

In `test_preview_contains_busy_people_fixture`, replace the old count-only and axis assumptions with:

```python
    assert 'class="pp-manager-strip"' in html
    assert 'class="pp-axis"' not in html
    assert html.count('class="pp-section-header"') == 3
    assert "6:00 AM" in html
    assert "11:30" in html
    assert "2:00 PM" in html
```

Rename `test_preview_fits_all_manager_viewports_and_keeps_sticky_time_context` to `test_preview_fits_all_manager_viewports_with_compact_sticky_controls`. In its Playwright body:

```python
                before = page.locator(".pp-manager-strip").bounding_box()
                page.evaluate("window.scrollTo(0, 900)")
                after = page.locator(".pp-manager-strip").bounding_box()
```

Replace `axisLabelsDoNotOverlap` with:

```javascript
scheduleLabelsDoNotOverlap: [...document.querySelectorAll('.pp-section-header')]
  .every(header => [...header.querySelectorAll('.pp-schedule-time-group')]
    .map(label => label.getBoundingClientRect())
    .every((box, index, boxes) => index === 0 || box.left >= boxes[index - 1].right)),
managerRows: getComputedStyle(document.querySelector('.pp-manager-strip')).gridTemplateRows,
```

Assert `scheduleLabelsDoNotOverlap is True`, the manager strip stays inside the viewport after scrolling, the document never exceeds the viewport width, and the rendered strip height is at most `88` pixels at the `768px` viewport and at most `44` pixels at wider viewports.

- [ ] **Step 2: Run preview tests and verify the old fixture fails**

Run:

```bash
pytest tests/test_preview_people_performance.py::test_preview_contains_busy_people_fixture tests/test_preview_people_performance.py::test_preview_fits_all_manager_viewports_with_compact_sticky_controls -q
```

Expected: FAIL because the preview context still provides `axis_labels` instead of schedule markers.

- [ ] **Step 3: Give the static preview the new schedule context**

Replace `axis_labels` in `scripts/preview_people_performance.py::_context()` with deterministic values matching its 6:00 AM-2:00 PM window and 11:30 AM break:

```python
        "schedule_markers": (
            {"left_pct": 0.0, "kind": "start", "aria_label": "Shift starts at 6:00 AM"},
            {"left_pct": 68.75, "kind": "break", "aria_label": "Planned break starts at 11:30 AM"},
            {"left_pct": 100.0, "kind": "end", "aria_label": "Shift ends at 2:00 PM"},
        ),
        "schedule_time_groups": (
            {"left_pct": 0.0, "label": "6:00 AM", "edge": "start"},
            {"left_pct": 68.75, "label": "11:30", "edge": "middle"},
            {"left_pct": 100.0, "label": "2:00 PM", "edge": "end"},
        ),
```

Keep preview rows, warnings, counts, and polling-disabled behavior unchanged.

- [ ] **Step 4: Run the full People dashboard suite**

Run:

```bash
pytest tests/test_people_performance_view.py tests/test_people_performance_template.py tests/test_people_performance_route.py tests/test_people_performance_rows.py tests/test_people_performance_data.py tests/test_people_performance_end_to_end.py tests/test_people_performance_static.py tests/test_preview_people_performance.py -q
```

Expected: PASS.

- [ ] **Step 5: Inspect generated screenshots**

Open these files produced by the preview test and compare them with the approved compact mockup:

```text
scripts/_preview_out/people_performance/people-performance-1440x900.png
scripts/_preview_out/people_performance/people-performance-1024x768.png
scripts/_preview_out/people_performance/people-performance-768x1024.png
```

Confirm: no repeated page title; one thin desktop strip; no more than two tablet rows; warning text readable; controls reachable; section titles, time rails, and summaries aligned; all person names and summaries visible; no overlapping time-group labels; and no page-level horizontal overflow.

- [ ] **Step 6: Add the plain-language What's New entry**

Add at the top of the current `2026-09-01` section in `CHANGELOG.md`:

```markdown
### Make the People page easier to scan

- **The People page now keeps its totals, warnings, date, and filter in one thin bar.** Each green group shows only the workday start, break starts, and end, so managers can find the important times faster.
```

- [ ] **Step 7: Run repository verification**

Run:

```bash
ruff check src tests scripts
pytest -q
git diff --check
```

Expected: all commands PASS with no lint errors, test failures, or whitespace errors.

- [ ] **Step 8: Commit and push the completed implementation**

```bash
git add scripts/preview_people_performance.py tests/test_preview_people_performance.py CHANGELOG.md
git commit -m "feat: finish compact people dashboard header"
git push origin main
```

The implementation is complete only after the push succeeds and `git status --branch --short` shows `main...origin/main` with no task-owned changes.
