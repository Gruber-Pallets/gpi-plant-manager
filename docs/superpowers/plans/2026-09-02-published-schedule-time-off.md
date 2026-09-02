# Published Schedule Time Off Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show full-day absences at the top of printed and Slack-published schedules while keeping partial-day timing inline beside each scheduled employee.

**Architecture:** Extend the pure staffing render model with one normalized `published_off_names` list assembled from full-day time off and optional-day Off assignments. Render that list in a screen-hidden, print-visible summary; continue using the existing partial timing map for inline labels, with print-only styling that removes the clear affordance. Slack needs no separate output logic because it already renders the posted staffing view through the shared print stylesheet.

**Tech Stack:** Python 3, FastAPI, Jinja2, pytest, HTML, CSS, Playwright-backed PDF rendering.

## Global Constraints

- The interactive browser scheduler must remain unchanged.
- Full-day time-off entries and employees explicitly placed in Saturday Off appear once in the print summary.
- Partial-day entries stay out of the full-day summary and keep their timing beside assigned employees.
- Browser printing and Slack publishing must continue to share one print/PDF layout.
- A failure to load time-off data must continue to render a schedule without an Off summary.
- Do not change time-off approval, synchronization, or classification behavior.

## File Structure

- `src/zira_dashboard/staffing_view.py`: derive the presentation-ready full-day Off name list from existing normalized inputs.
- `src/zira_dashboard/templates/staffing.html`: render the print-only Off summary and mark the partial clear glyph separately from its timing text.
- `src/zira_dashboard/static/staffing.css`: keep the new summary invisible in the interactive scheduler.
- `src/zira_dashboard/static/staffing-print.css`: show and format the summary, and flatten partial timing labels for paper/PDF.
- `tests/test_staffing_view.py`: cover full-day/partial/Saturday combination, deduplication, and deterministic ordering.
- `tests/test_staffing_static.py`: pin the shared template and print-style contracts.
- `tests/test_share_route.py`: retain the existing proof that Slack requests the posted view and sends its rendered PDF; no production share-route change is needed.
- `CHANGELOG.md`: add a plain-language shipped feature note only with the completed implementation.

---

### Task 1: Derive the Published Full-Day Off List

**Files:**
- Modify: `tests/test_staffing_view.py`
- Modify: `src/zira_dashboard/staffing_view.py:482-505`

**Interfaces:**
- Consumes: `full_day_entries: list[dict]` and `off: list[str]`, both already derived inside `build_staffing_bays(...)`.
- Produces: `build_staffing_bays(...)` result key `published_off_names: list[str]`.

- [ ] **Step 1: Write the failing render-model tests**

Update the exact return-key contract to include `published_off_names`, then add focused coverage:

```python
def test_published_off_names_include_full_days_and_exclude_partials(patch_wcs):
    patch_wcs([])
    model = staffing_view.build_staffing_bays(
        roster=[_person("Zoe"), _person("Ana"), _person("Mia")],
        sched=_sched(),
        time_off_entries=[
            {"name": "Zoe", "hours": None},
            {"name": "Mia", "hours": 2.0},
            {"name": "Ana", "hours": None},
        ],
        publish_blocked=0,
    )

    assert model["published_off_names"] == ["Ana", "Zoe"]


def test_published_off_names_include_optional_day_off_and_deduplicate_entries(patch_wcs):
    patch_wcs([])
    model = staffing_view.build_staffing_bays(
        roster=[_person("Ana"), _person("Ben")],
        sched=_sched(),
        time_off_entries=[
            {"name": "Ana", "hours": None},
            {"name": "Ana", "hours": None},
        ],
        publish_blocked=0,
        optional_commitments={},
    )

    assert model["published_off_names"] == ["Ana", "Ben"]
```

The first test proves partial-day `Mia` is excluded on a normal workday. With no optional-day commitments, active non-reserve `Ben` is explicitly Off; the second test also proves repeated full-day entries yield one `Ana`.

- [ ] **Step 2: Run the new tests to verify RED**

Run:

```bash
pytest -q \
  tests/test_staffing_view.py::test_return_keys_are_exactly_the_bands_ab_context_keys \
  tests/test_staffing_view.py::test_published_off_names_include_full_days_and_exclude_partials \
  tests/test_staffing_view.py::test_published_off_names_include_optional_day_off_and_deduplicate_entries
```

Expected: FAIL because `published_off_names` is absent from the returned model.

- [ ] **Step 3: Add the minimal render-model value**

Immediately before the return dictionary, derive the combined names:

```python
    published_off_names = sorted(
        time_off_set | set(off),
        key=lambda name: (name.lower(), name),
    )
```

Add it to the returned context beside the related keys:

```python
        "time_off_names": sorted(e["name"] for e in full_day_entries),
        "published_off_names": published_off_names,
        "time_off_entries": sorted(full_day_entries, key=lambda e: e["name"].lower()),
```

Also update the builder docstring's exact output list to name `published_off_names`.

- [ ] **Step 4: Run the focused render-model tests to verify GREEN**

Run:

```bash
pytest -q tests/test_staffing_view.py
```

Expected: all tests in `tests/test_staffing_view.py` PASS.

- [ ] **Step 5: Commit the independently tested render-model change**

```bash
git add CHANGELOG.md src/zira_dashboard/staffing_view.py tests/test_staffing_view.py
git commit -m "feat: derive published schedule absences"
git push origin main
```

Before this intermediate push, add this plain-language `CHANGELOG.md` note:

```markdown
### Prepare shared schedules to list full-day absences

- **Plant Manager can now gather one clean list of everyone who is off all day.** Printed schedules have not changed yet; the next step will place this list near the top.
```

### Task 2: Render Full-Day Off and Flatten Partial Timing for Print

**Files:**
- Modify: `tests/test_staffing_static.py`
- Modify: `src/zira_dashboard/templates/staffing.html:17-23,35-38,345`
- Modify: `src/zira_dashboard/static/staffing.css:1071-1074`
- Modify: `src/zira_dashboard/static/staffing-print.css:23-55,57-90,92-129`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Jinja context `published_off_names: list[str]`, `partial_range_by_name: dict[str, str]`, and `partial_hours_by_name: dict[str, float]`.
- Produces: `.print-off-summary` markup and `.partial-clear-mark` markup styled consistently in browser print and Slack PDF output.

- [ ] **Step 1: Write failing static contract tests**

Replace the old assertion that all time-off output is hidden with explicit contracts:

```python
def test_printed_scheduler_shows_full_day_off_summary_but_hides_interactive_rails():
    html = _template()
    screen_css = _style()
    print_css = _print_css()

    assert '{% if published_off_names %}<div class="print-off-summary">' in html
    assert '{% for name in published_off_names %}{{ name }}{% if not loop.last %}, {% endif %}{% endfor %}' in html
    assert ".print-off-summary { display: none; }" in screen_css
    assert ".print-off-summary {" in print_css

    hidden_sections = print_css.split(".section.reserves,", 1)[1].split("{", 1)[0]
    assert ".section.saturday-off," in hidden_sections
    assert ".section.timeoff," in hidden_sections


def test_printed_partial_time_off_stays_inline_without_clear_mark():
    html = _template()
    print_css = _print_css()

    assert html.count('class="partial-clear-mark"') == 3
    assert ".partial-clear-mark" in print_css
    assert "display: none !important;" in print_css
    assert "button.partial-hours-badge" in print_css
```

- [ ] **Step 2: Run the static tests to verify RED**

Run:

```bash
pytest -q \
  tests/test_staffing_static.py::test_printed_scheduler_shows_full_day_off_summary_but_hides_interactive_rails \
  tests/test_staffing_static.py::test_printed_partial_time_off_stays_inline_without_clear_mark
```

Expected: FAIL because the print summary and separately styled clear mark do not exist.

- [ ] **Step 3: Add the print-only summary markup**

At the start of `#staffing-form`, before any screen-only banners, render only a non-empty list:

```jinja2
{% if published_off_names %}<div class="print-off-summary"><strong>Off:</strong> {% for name in published_off_names %}{{ name }}{% if not loop.last %}, {% endif %}{% endfor %}</div>{% endif %}
```

Wrap the existing partial badge's trailing clear glyph in all three render sites: assigned, unscheduled, and reserves.

```jinja2
<span class="partial-clear-mark" aria-hidden="true"> ✕</span>
```

Keep the timing expression outside this span so it remains visible on paper.

- [ ] **Step 4: Add screen and print styles**

Extend the existing screen-only print-header rule:

```css
.print-header,
.print-off-summary { display: none; }
```

Add print presentation immediately after the print header rules:

```css
.print-off-summary {
  display: block;
  margin: 0 0 0.3rem;
  padding: 0.2rem 0.35rem;
  border: 1px solid #777;
  font-size: 9pt;
  color: black;
}

button.partial-hours-badge {
  appearance: none;
  background: transparent !important;
  border: 0 !important;
  color: black !important;
  padding: 0 !important;
  font: inherit;
}
.partial-clear-mark { display: none !important; }
```

Leave `.section.timeoff` and `.section.saturday-off` in the print hide list; the dedicated summary replaces those interactive rails without duplicating partial information.

- [ ] **Step 5: Run focused and related tests to verify GREEN**

Run:

```bash
pytest -q tests/test_staffing_static.py tests/test_share_route.py tests/test_staffing_view.py
```

Expected: all selected tests PASS, including the existing assertion that Slack renders `view="posted"` before PDF upload.

- [ ] **Step 6: Add the user-facing What's New note**

Add the newest entry under `## 2026-09-02` in `CHANGELOG.md`:

```markdown
### Show who is off on shared schedules

- **Printed schedules and schedules sent to Slack now show everyone who is off all day near the top.** A person who is off for only part of the day keeps the time beside their name.
```

- [ ] **Step 7: Run final verification**

Run:

```bash
pytest -q tests/test_staffing_view.py tests/test_staffing_static.py tests/test_share_route.py
git diff --check
```

Expected: pytest reports zero failures and `git diff --check` exits 0 with no output.

- [ ] **Step 8: Commit and push the completed feature**

```bash
git add CHANGELOG.md src/zira_dashboard/templates/staffing.html src/zira_dashboard/static/staffing.css src/zira_dashboard/static/staffing-print.css tests/test_staffing_static.py
git commit -m "feat: show time off on shared schedules"
git push origin main
```

### Task 3: Complete Feedback Tracking After Deployment-Ready Verification

**Files:**
- No repository files.

**Interfaces:**
- Consumes: pushed implementation commit on `origin/main`, passing validation, authenticated Plant Manager admin feedback record `GPI-PM-FB-44`, and its existing Odoo mirror.
- Produces: completed local feedback with a short resolution note and verified Odoo Status `Completed` after synchronization.

- [ ] **Step 1: Verify the implementation is on `origin/main`**

Run:

```bash
git status --short --branch
git rev-parse HEAD
git rev-parse origin/main
```

Expected: the two revisions match; unrelated pre-existing worktree changes may remain, but none of the feature files are uncommitted.

- [ ] **Step 2: Complete the authoritative Plant Manager feedback**

Through the authenticated admin lifecycle, complete feedback ID `44` as the current actor with this resolution note:

```text
Printed schedules and Slack schedule PDFs now show full-day absences near the top, while partial-day times stay beside each person's name.
```

Do not edit the Odoo mirror directly.

- [ ] **Step 3: Wait for and verify the existing Odoo mirror**

Read the single matching row identified by Source ID `GPI-PM-FB-44`. Confirm its Status is exactly `Completed`. If access, completion, synchronization, or readback fails, leave the task active and report the blocker without exposing secrets.
