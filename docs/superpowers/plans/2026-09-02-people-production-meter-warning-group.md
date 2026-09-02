# People Production Meter Warning Group Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace separate unavailable production-meter alerts with one counted alert in the People page's top manager row and show every affected meter in its detail panel.

**Architecture:** Preserve `DashboardWarning` as the source record and add a presentation-only `DashboardWarningGroup` in `people_performance_view.py`. A deterministic grouping helper will merge only `production_metric_unavailable` warnings, and both the dashboard context and detail route will consume the same helper so polling and detail lookups agree. Jinja will render the group count and member details; CSS will move warnings into the primary row without reducing accessible touch targets.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, vanilla JavaScript, CSS, pytest, Playwright.

## Global Constraints

- Group only warnings whose kind is `production_metric_unavailable`.
- The visible group label is exactly `Production Meters Unavailable`.
- One production-meter warning still uses the grouped label and a badge of `1`.
- Other warning kinds remain separate controls.
- Keep the stable opaque 24-character hexadecimal key contract.
- Keep current warning interactions, polling, focus restoration, and touch-target sizes.
- Do not add horizontal scrolling to the manager strip.
- Do not modify unrelated dirty worktree files.

---

### Task 1: Group production-meter warning summaries

**Files:**
- Modify: `src/zira_dashboard/people_performance_view.py:1-25,404-445,504-507`
- Test: `tests/test_people_performance_view.py:1-55`

**Interfaces:**
- Consumes: `DashboardWarning` and `warning_key(kind, subject)` from `people_performance_warnings.py`.
- Produces: `DashboardWarningGroup`, `warning_groups(warnings)`, `warning_group_summary_view(group)`, and `warning_group_detail_context(group)`.

- [ ] **Step 1: Write failing grouping tests**

Add imports for `production_metric_warning`, then add tests that build two production warnings and one unrelated fixture:

```python
def production_warning(station_name: str):
    return production_metric_warning(
        station_name=station_name,
        reason_code="calculation_failure",
        checked_at_utc=datetime(2026, 9, 2, 14, 30, tzinfo=UTC),
        day=DAY,
    )


def test_warning_groups_merge_only_production_meters_with_stable_counted_summary():
    trim = production_warning("Trim Saw 1")
    hand_build = production_warning("Hand Build #1")
    forklift = unmatched_warning_fixture(call_count=135)

    groups = people_performance_view.warning_groups((trim, forklift, hand_build))

    assert len(groups) == 2
    production = groups[0]
    assert production.kind == "production_metric_unavailable"
    assert production.label == "Production Meters Unavailable"
    assert production.count == 2
    assert [item.subject for item in production.members] == ["Hand Build #1", "Trim Saw 1"]
    assert len(production.key) == 24
    assert groups[1].key == forklift.key
    assert groups[1].count is None


def test_single_production_meter_still_uses_group_label_and_count_one():
    groups = people_performance_view.warning_groups((production_warning("Trim Saw 1"),))
    summary = people_performance_view.warning_group_summary_view(groups[0])

    assert summary["label"] == "Production Meters Unavailable"
    assert summary["count"] == 1
    assert summary["accessible_label"] == "Production Meters Unavailable: 1"
```

- [ ] **Step 2: Run the grouping tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_people_performance_view.py -k 'warning_groups or single_production_meter' -q
```

Expected: FAIL because `warning_groups` and `warning_group_summary_view` do not exist.

- [ ] **Step 3: Add the presentation group and summary helper**

Add `dataclass`, import `warning_key`, and implement:

```python
@dataclass(frozen=True)
class DashboardWarningGroup:
    key: str
    kind: str
    label: str
    summary: str
    members: tuple[DashboardWarning, ...]
    count: int | None = None


def warning_groups(warnings: tuple[DashboardWarning, ...]) -> tuple[DashboardWarningGroup, ...]:
    production = tuple(sorted(
        (item for item in warnings if item.kind == "production_metric_unavailable"),
        key=lambda item: item.subject.casefold(),
    ))
    production_group = DashboardWarningGroup(
        key=warning_key("production_metric_unavailable", "production-meters"),
        kind="production_metric_unavailable",
        label="Production Meters Unavailable",
        summary=f"{len(production)} production meter{' is' if len(production) == 1 else 's are'} unavailable.",
        members=production,
        count=len(production),
    ) if production else None
    groups: list[DashboardWarningGroup] = []
    inserted_production = False
    for warning in warnings:
        if warning.kind == "production_metric_unavailable":
            if not inserted_production and production_group is not None:
                groups.append(production_group)
                inserted_production = True
            continue
        groups.append(DashboardWarningGroup(
            key=warning.key,
            kind=warning.kind,
            label=warning.label,
            summary=warning.summary,
            members=(warning,),
        ))
    return tuple(groups)


def warning_group_summary_view(group: DashboardWarningGroup) -> dict:
    accessible_label = (
        f"{group.label}: {group.count}" if group.count is not None else group.label
    )
    return {
        "key": group.key,
        "kind": group.kind,
        "label": group.label,
        "summary": group.summary,
        "count": group.count,
        "accessible_label": accessible_label,
    }
```

Change `dashboard_context()` to map `warning_group_summary_view` over `warning_groups(model.source_warnings)`.

- [ ] **Step 4: Run the grouping and existing presenter tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_people_performance_view.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the grouping unit**

```bash
git add src/zira_dashboard/people_performance_view.py tests/test_people_performance_view.py
git commit -m "feat: group People production meter warnings"
```

---

### Task 2: Resolve and render grouped warning details

**Files:**
- Modify: `src/zira_dashboard/people_performance_view.py:413-445`
- Modify: `src/zira_dashboard/routes/people_performance.py:13-16,139-151`
- Modify: `src/zira_dashboard/templates/_people_performance_warning_panel.html:1-27`
- Test: `tests/test_people_performance_view.py:30-70`
- Test: `tests/test_people_performance_route.py:140-175`

**Interfaces:**
- Consumes: `DashboardWarningGroup` and `warning_groups(warnings)` from Task 1.
- Produces: `warning_group_detail_context(group: DashboardWarningGroup | None) -> dict` with `members`, plus fresh grouped-key lookup in the detail route.

- [ ] **Step 1: Write failing detail presenter and route tests**

Add a presenter test asserting sorted member details and retained actions:

```python
def test_group_detail_keeps_each_production_meter_reason_and_actions():
    group = people_performance_view.warning_groups((
        production_warning("Trim Saw 1"),
        production_warning("Hand Build #1"),
    ))[0]

    detail = people_performance_view.warning_group_detail_context(group)

    assert detail["title"] == "Production Meters Unavailable"
    assert [item["subject"] for item in detail["members"]] == ["Hand Build #1", "Trim Saw 1"]
    assert all(item["summary"] for item in detail["members"])
    assert all(item["checked_at"] == "9:30 AM" for item in detail["members"])
    assert all(
        [action["action_id"] for action in item["actions"]]
        == ["check_again", "open_work_center"]
        for item in detail["members"]
    )
```

Add a route test that derives the grouped key from `warning_groups`, requests it, and asserts both work centers occur in the response with two work-center links.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_people_performance_view.py::test_group_detail_keeps_each_production_meter_reason_and_actions tests/test_people_performance_route.py::test_grouped_production_warning_detail_lists_every_meter -q
```

Expected: FAIL because the grouped detail context and grouped route lookup are absent.

- [ ] **Step 3: Implement grouped detail context**

Extract the current single-warning conversion into `_warning_member_context(warning)` and return this group contract:

```python
def warning_group_detail_context(group: DashboardWarningGroup | None) -> dict:
    if group is None:
        return warning_detail_context(None)
    members = tuple(_warning_member_context(item) for item in group.members)
    if group.count is None:
        return {"state": "open", "key": group.key, **members[0], "members": ()}
    return {
        "state": "open",
        "key": group.key,
        "kind": group.kind,
        "title": group.label,
        "summary": group.summary,
        "impact": "Production details are hidden for the listed work centers.",
        "facts": (("Affected meters", str(group.count)),),
        "checked_at": "",
        "last_success_at": "",
        "actions": (),
        "members": members,
    }
```

Retain `warning_detail_context()` as a compatibility wrapper around a one-member group so existing callers and tests remain valid.

- [ ] **Step 4: Make the detail route use fresh groups**

Import `warning_groups` and `warning_group_detail_context`. Replace raw-warning lookup with:

```python
    warning = next(
        (item for item in warning_groups(model.source_warnings) if item.key == warning_key_value),
        None,
    )
```

Pass `warning_group_detail_context(warning)` to the partial.

- [ ] **Step 5: Render member sections in the panel partial**

Before the existing group footer, add:

```jinja2
  {% if warning.members %}
  <div class="pp-warning-members">
    {% for member in warning.members %}
    <section class="pp-warning-member" aria-labelledby="pp-warning-member-{{ loop.index }}">
      <h3 id="pp-warning-member-{{ loop.index }}">{{ member.subject }}</h3>
      <p>{{ member.summary }}</p>
      <p class="pp-warning-impact"><strong>People page impact:</strong> {{ member.impact }}</p>
      {% if member.facts %}<dl>{% for label, value in member.facts %}<div><dt>{{ label }}</dt><dd>{{ value }}</dd></div>{% endfor %}</dl>{% endif %}
      {% if member.checked_at %}<p class="pp-warning-time">Checked {{ member.checked_at }}{% if member.last_success_at %} · Last successful update {{ member.last_success_at }}{% endif %}</p>{% endif %}
      {% if member.actions %}<footer>{% for action in member.actions %}{% if action.action_id == 'check_again' %}<button type="button" data-pp-warning-action="check_again">{{ action.label }}</button>{% else %}<a href="{{ action.href }}" data-pp-warning-action="{{ action.action_id }}">{{ action.label }}</a>{% endif %}{% endfor %}</footer>{% endif %}
    </section>
    {% endfor %}
  </div>
  {% endif %}
```

- [ ] **Step 6: Run presenter, route, and template tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_people_performance_view.py tests/test_people_performance_route.py tests/test_people_performance_template.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit grouped details**

```bash
git add src/zira_dashboard/people_performance_view.py src/zira_dashboard/routes/people_performance.py src/zira_dashboard/templates/_people_performance_warning_panel.html tests/test_people_performance_view.py tests/test_people_performance_route.py
git commit -m "feat: show details for grouped production meters"
```

---

### Task 3: Move warning controls into the compact top row

**Files:**
- Modify: `src/zira_dashboard/templates/_people_performance_rows.html:10-67`
- Modify: `src/zira_dashboard/static/people-performance.css:11-160`
- Test: `tests/test_people_performance_template.py:116-187`
- Test: `tests/test_people_performance_static.py:63-165`

**Interfaces:**
- Consumes: summary dictionaries containing `label`, `count`, and `accessible_label` from Task 1.
- Produces: one primary-row warning region and `.pp-warning-count` badge styling.

- [ ] **Step 1: Write failing template and CSS contract tests**

Update the compact-strip test to require `.pp-source-warnings` inside `.pp-manager-primary`, before `.pp-manager-actions`. Add assertions:

```python
assert 'aria-label="Production Meters Unavailable: 2"' in warning_strip
assert '<span class="pp-warning-label">Production Meters Unavailable</span>' in warning_strip
assert '<span class="pp-warning-count" aria-hidden="true">2</span>' in warning_strip
assert warning_strip.count('data-warning-kind="production_metric_unavailable"') == 1
assert "Trim Saw 1" not in warning_strip
assert "Hand Build #1" not in warning_strip
```

Update the CSS contract to require `.pp-source-warnings { flex: 0 1 auto; width: auto; }` and `.pp-warning-count` badge rules.

- [ ] **Step 2: Run the focused contracts and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_people_performance_template.py::test_page_uses_one_compact_live_manager_strip tests/test_people_performance_template.py::test_warning_strip_uses_safe_semantic_detail_triggers tests/test_people_performance_static.py::test_manager_strip_is_sticky_and_manager_groups_wrap_without_scrollbars -q
```

Expected: FAIL because warnings are still a second row and no count badge is rendered.

- [ ] **Step 3: Move and update the warning markup**

Move the existing conditional `<aside>` between `.pp-counts` and `.pp-manager-actions`, then render:

```jinja2
<button type="button" class="pp-warning-trigger"
        data-warning-key="{{ warning.key }}"
        data-warning-kind="{{ warning.kind }}"
        data-warning-summary="{{ warning.summary|e }}"
        aria-label="{{ warning.accessible_label }}"
        aria-expanded="false"
        aria-controls="pp-warning-popover">
  <span class="pp-warning-icon" aria-hidden="true">!</span>
  <span class="pp-warning-label">{{ warning.label }}</span>
  {% if warning.count is not none %}<span class="pp-warning-count" aria-hidden="true">{{ warning.count }}</span>{% endif %}
</button>
```

- [ ] **Step 4: Make the warning region a primary-row flex item**

Replace its full-width rule with:

```css
.pp-source-warnings {
  flex: 0 1 auto;
  width: auto;
  flex-wrap: wrap;
}

.pp-warning-count {
  display: inline-grid;
  min-width: 1.35rem;
  min-height: 1.35rem;
  margin-left: .4rem;
  padding: 0 .25rem;
  place-items: center;
  border-radius: 999px;
  background: #7c2d12;
  color: #fff;
  font-size: .72rem;
  font-weight: 800;
  line-height: 1;
}
```

Change `.pp-warning-trigger > span` to `.pp-warning-icon` so the badge does not inherit the icon margin.

- [ ] **Step 5: Run template and static tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_people_performance_template.py tests/test_people_performance_static.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the compact layout**

```bash
git add src/zira_dashboard/templates/_people_performance_rows.html src/zira_dashboard/static/people-performance.css tests/test_people_performance_template.py tests/test_people_performance_static.py
git commit -m "feat: move People warnings into the top row"
```

---

### Task 4: Verify live refresh and viewport behavior

**Files:**
- Modify: `scripts/preview_people_performance.py:520-545`
- Modify: `tests/test_preview_people_performance.py:80-130,450-570`
- Modify: `tests/test_people_performance_static.py:800-875`

**Interfaces:**
- Consumes: the stable grouped warning key and grouped summary markup from Tasks 1 and 3.
- Produces: deterministic preview coverage proving the group survives refresh and fits supported viewports.

- [ ] **Step 1: Write failing preview and refresh assertions**

Change the preview fixture's two production summaries into one summary:

```python
{
    "key": "111111111111111111111111",
    "kind": "production_metric_unavailable",
    "label": "Production Meters Unavailable",
    "summary": "2 production meters are unavailable.",
    "count": 2,
    "accessible_label": "Production Meters Unavailable: 2",
},
```

Keep the unrelated forklift warning, adding `count: None` and its label as `accessible_label`. Assert the rendered preview has one production trigger and the count badge. In the refresh harness, use the same key before and after row replacement and assert `aria-expanded="true"` is restored on the grouped trigger.

- [ ] **Step 2: Run focused preview/static tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_preview_people_performance.py::test_preview_contains_busy_people_fixture tests/test_people_performance_static.py -k warning -q
```

Expected: FAIL because the deterministic preview still contains two production triggers.

- [ ] **Step 3: Update the preview fixture and grouped panel response**

Make `scripts/preview_people_performance.py::_context()` return the grouped summary above. Update `_install_console_capture()` in the preview test to return grouped panel HTML containing headings for `Hand Build #1` and `Trim Saw 1`.

- [ ] **Step 4: Run all People page tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_people_performance_warnings.py tests/test_people_performance_rows.py tests/test_people_performance_view.py tests/test_people_performance_route.py tests/test_people_performance_template.py tests/test_people_performance_static.py tests/test_preview_people_performance.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit preview and refresh coverage**

```bash
git add scripts/preview_people_performance.py tests/test_preview_people_performance.py tests/test_people_performance_static.py
git commit -m "test: cover grouped People meter warning refresh"
```

---

### Task 5: Release note and final verification

**Files:**
- Modify: `CHANGELOG.md:12-20`

**Interfaces:**
- Consumes: completed, verified behavior from Tasks 1-4.
- Produces: plain-language What's New entry and final release evidence.

- [ ] **Step 1: Replace the plan-only note with the shipped result**

Change the current plan note to:

```markdown
### See missing production meters in one alert

- **The People page now combines missing production meters into one alert with a count.** The alert sits in the top row to save space. Open it to see what went wrong for each meter and what you can do next.
```

- [ ] **Step 2: Run formatting checks and the complete relevant suite**

Run:

```bash
git diff --check
.venv/bin/python -m pytest tests/test_people_performance_warnings.py tests/test_people_performance_rows.py tests/test_people_performance_view.py tests/test_people_performance_route.py tests/test_people_performance_template.py tests/test_people_performance_static.py tests/test_preview_people_performance.py -q
```

Expected: no whitespace errors and all tests PASS.

- [ ] **Step 3: Render browser previews at every permanent viewport**

Run:

```bash
.venv/bin/python -m pytest tests/test_preview_people_performance.py -q
```

Expected: PASS at `1440x900`, `1195x768`, `1024x768`, `768x1024`, and `390x844`, with no overlap, clipping, horizontal page overflow, or console errors.

- [ ] **Step 4: Commit the release note**

```bash
git add CHANGELOG.md
git commit -m "docs: announce grouped production meter alert"
```

- [ ] **Step 5: Push all implementation commits**

```bash
git push origin main
```

Expected: `origin/main` advances through every implementation commit.
