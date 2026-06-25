# Unpublished Schedule Inbox Badge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an inbox reminder for unpublished next-business-day plant schedules after 1:30 PM, and make the Inbox nav treatment red whenever there is any open inbox to-do.

**Architecture:** Keep the schedule reminder in `exception_inbox.py`, beside the other inbox source adapters. Compute the next plant business day from `schedule_store.current().work_weekdays`, load that schedule through `staffing.load_schedule()`, and add one warning section when it is not published. Update the existing global footer Inbox summary styling so any positive total uses red alert styling.

**Tech Stack:** Python 3.11+, FastAPI/Jinja2, pytest, plain JavaScript and CSS.

## Global Constraints

- The cutoff is exactly `1:30 PM` plant-local time.
- A missing schedule row counts as not published.
- Business days follow the configured plant work-week, with no holiday calendar.
- The schedule reminder links to `/staffing?day=YYYY-MM-DD`.
- Any positive inbox total turns the Inbox link/count red; source-error-only remains amber.
- Do not change schedule publishing behavior.
- Do not add a dismiss or snooze control for this reminder.
- Subagents must use the same model as the main session; omit model overrides unless setting the same model explicitly.

---

## File Structure

- `src/zira_dashboard/exception_inbox.py`: owns inbox source aggregation; add a private schedule-reminder helper and include it in summary and snapshot output.
- `tests/test_exception_inbox.py`: add TDD coverage for schedule reminder behavior and update existing aggregate expectations for the new zero-count section.
- `src/zira_dashboard/static/footer.js`: change nav summary state so `is-degraded` is used only for source-error-only states; positive totals stay in the `has-open` state.
- `src/zira_dashboard/static/footer.css`: add red styling for `.inbox-nav-link.has-open` and its count badge.

## Task 1: Add Plant Schedule Reminder To Inbox Data

**Files:**
- Modify: `tests/test_exception_inbox.py`
- Modify: `src/zira_dashboard/exception_inbox.py`

**Interfaces:**
- Consumes: `plant_day.now() -> datetime`, `schedule_store.current().work_weekdays`, `staffing.load_schedule(day) -> staffing.Schedule`.
- Produces: `_plant_schedule_reminder() -> tuple[int, list[dict]]` and `_next_business_day(day: date) -> date`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_exception_inbox.py`, update the imports:

```python
from types import SimpleNamespace
```

Change the existing `zira_dashboard` import to include `staffing`:

```python
from zira_dashboard import db, exception_inbox, missing_wc, missed_punch_out, staffing
```

Add these helper and tests after `test_pending_time_off_section_links_to_approvals_page`:

```python
def _empty_inbox_sources(monkeypatch):
    monkeypatch.setattr(staffing_routes, "assignments_todo_payload", lambda: {"count": 0})
    monkeypatch.setattr(staffing_routes, "late_report_payload", lambda: {"count": 0})
    monkeypatch.setattr(missing_wc, "current_rows", lambda: [])
    monkeypatch.setattr(missed_punch_out, "current_rows", lambda: [])
    monkeypatch.setattr(exception_inbox, "_pending_time_off", lambda today: (0, []))
    monkeypatch.setattr(exception_inbox, "_pending_time_off_count", lambda today: 0)
    monkeypatch.setattr(exception_inbox, "_work_center_names", lambda: [])


def test_plant_schedule_reminder_waits_until_cutoff(monkeypatch):
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "now",
        lambda: datetime(2026, 6, 25, 13, 29),
    )

    count, rows = exception_inbox._plant_schedule_reminder()

    assert count == 0
    assert rows == []


def test_plant_schedule_reminder_adds_unpublished_next_business_day(monkeypatch):
    loaded_days = []
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "now",
        lambda: datetime(2026, 6, 25, 13, 30),
    )
    monkeypatch.setattr(
        exception_inbox.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )

    def fake_load_schedule(day):
        loaded_days.append(day)
        return staffing.Schedule(day=day, published=False)

    monkeypatch.setattr(exception_inbox.staffing, "load_schedule", fake_load_schedule)

    count, rows = exception_inbox._plant_schedule_reminder()

    assert loaded_days == [date(2026, 6, 26)]
    assert count == 1
    assert rows == [{
        "name": "Plant Schedule",
        "label": "Friday, Jun 26",
        "detail": "Not published",
        "priority": "warn",
        "badge": "Publish",
        "href": "/staffing?day=2026-06-26",
        "row_key": "plant_schedule:2026-06-26",
    }]


def test_plant_schedule_reminder_skips_published_target(monkeypatch):
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "now",
        lambda: datetime(2026, 6, 25, 14, 0),
    )
    monkeypatch.setattr(
        exception_inbox.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )
    monkeypatch.setattr(
        exception_inbox.staffing,
        "load_schedule",
        lambda day: staffing.Schedule(day=day, published=True),
    )

    count, rows = exception_inbox._plant_schedule_reminder()

    assert count == 0
    assert rows == []


def test_plant_schedule_reminder_friday_after_cutoff_targets_monday(monkeypatch):
    loaded_days = []
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "now",
        lambda: datetime(2026, 6, 26, 14, 0),
    )
    monkeypatch.setattr(
        exception_inbox.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )

    def fake_load_schedule(day):
        loaded_days.append(day)
        return staffing.Schedule(day=day, published=False)

    monkeypatch.setattr(exception_inbox.staffing, "load_schedule", fake_load_schedule)

    count, rows = exception_inbox._plant_schedule_reminder()

    assert loaded_days == [date(2026, 6, 29)]
    assert count == 1
    assert rows[0]["label"] == "Monday, Jun 29"


def test_snapshot_includes_unpublished_schedule_section_after_cutoff(monkeypatch):
    _empty_inbox_sources(monkeypatch)
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "today",
        lambda: date(2026, 6, 25),
    )
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "now",
        lambda: datetime(2026, 6, 25, 13, 45),
    )
    monkeypatch.setattr(
        exception_inbox.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )
    monkeypatch.setattr(
        exception_inbox.staffing,
        "load_schedule",
        lambda day: staffing.Schedule(day=day, published=False),
    )

    snap = exception_inbox.build_snapshot()

    plant_schedule = next(s for s in snap["sections"] if s["id"] == "plant_schedule")
    assert snap["total"] == 1
    assert snap["urgent_total"] == 0
    assert plant_schedule["count"] == 1
    assert plant_schedule["title"] == "Plant Schedule"
    assert plant_schedule["tone"] == "warn"
    assert plant_schedule["href"] == "/staffing?day=2026-06-26"
    assert plant_schedule["rows"][0]["badge"] == "Publish"


def test_summary_includes_unpublished_schedule_after_cutoff(monkeypatch):
    _empty_inbox_sources(monkeypatch)
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "today",
        lambda: date(2026, 6, 25),
    )
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "now",
        lambda: datetime(2026, 6, 25, 13, 45),
    )
    monkeypatch.setattr(
        exception_inbox.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )
    monkeypatch.setattr(
        exception_inbox.staffing,
        "load_schedule",
        lambda day: staffing.Schedule(day=day, published=False),
    )

    summary = exception_inbox.build_summary()

    assert summary["total"] == 1
    assert summary["sections"]["plant_schedule"] == 1


def test_schedule_source_failure_marks_inbox_degraded(monkeypatch):
    _empty_inbox_sources(monkeypatch)
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "today",
        lambda: date(2026, 6, 25),
    )
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "now",
        lambda: datetime(2026, 6, 25, 13, 45),
    )

    def fail_schedule():
        raise RuntimeError("schedule settings unavailable")

    monkeypatch.setattr(exception_inbox, "_plant_schedule_reminder", fail_schedule)

    snap = exception_inbox.build_snapshot()

    assert snap["total"] == 0
    assert {"source": "Plant Schedule"} in snap["source_errors"]
    plant_schedule = next(s for s in snap["sections"] if s["id"] == "plant_schedule")
    assert plant_schedule["count"] == 0
    assert plant_schedule["rows"] == []
```

Update the existing aggregate expectations in `tests/test_exception_inbox.py`:

```python
assert counts == {
    "assignments": 1,
    "plant_schedule": 0,
    "late": 2,
    "missing_wc": 1,
    "missed_punch_out": 1,
    "time_off": 1,
}
```

```python
assert summary["sections"] == {
    "assignments": 2,
    "plant_schedule": 0,
    "late": 3,
    "missing_wc": 1,
    "missed_punch_out": 1,
    "time_off": 4,
}
```

```python
assert [s["id"] for s in snap["sections"]] == [
    "assignments",
    "plant_schedule",
    "late",
    "missing_wc",
    "missed_punch_out",
    "time_off",
]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_exception_inbox.py::test_plant_schedule_reminder_waits_until_cutoff -v`

Expected: FAIL with `AttributeError` because `_plant_schedule_reminder` does not exist.

- [ ] **Step 3: Write minimal implementation**

In `src/zira_dashboard/exception_inbox.py`, update imports and module imports:

```python
from datetime import date, time, timedelta

from . import plant_day, schedule_store, staffing
```

Add these helpers after `_work_center_names()`:

```python
_SCHEDULE_REMINDER_CUTOFF = time(13, 30)


def _next_business_day(day: date) -> date:
    work_weekdays = schedule_store.current().work_weekdays or frozenset({0, 1, 2, 3, 4})
    nxt = day + timedelta(days=1)
    for _ in range(14):
        if nxt.weekday() in work_weekdays:
            return nxt
        nxt += timedelta(days=1)
    return day + timedelta(days=1)


def _plant_schedule_reminder() -> tuple[int, list[dict]]:
    now = plant_day.now()
    if now.time() < _SCHEDULE_REMINDER_CUTOFF:
        return 0, []

    target_day = _next_business_day(now.date())
    sched = staffing.load_schedule(target_day)
    if sched.published:
        return 0, []

    return 1, [{
        "name": "Plant Schedule",
        "label": target_day.strftime("%A, %b %-d"),
        "detail": "Not published",
        "priority": "warn",
        "badge": "Publish",
        "href": f"/staffing?day={target_day.isoformat()}",
        "row_key": _row_key("plant_schedule", target_day.isoformat()),
    }]
```

In `build_summary()`, add the schedule source:

```python
    schedule_count = _capture(
        source_errors, "Plant Schedule", lambda: _plant_schedule_reminder()[0], 0
    )
```

Include `schedule_count` in the total:

```python
    total = assignment_count + schedule_count + late_count + missing_count + missed_count + pending_count
```

Include the section count:

```python
            "plant_schedule": schedule_count,
```

In `build_snapshot()`, add the schedule source after `assignments`:

```python
    schedule_count, schedule_rows = _capture(
        source_errors, "Plant Schedule", _plant_schedule_reminder, (0, [])
    )
```

Add this section immediately after the Assignments To Do section:

```python
        {
            "id": "plant_schedule",
            "title": "Plant Schedule",
            "count": schedule_count,
            "tone": "warn",
            "action_key": None,
            "action_label": None,
            "href": schedule_rows[0]["href"] if schedule_rows else "/staffing",
            "empty": "All clear",
            "context": {},
            "rows": schedule_rows,
        },
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_exception_inbox.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/exception_inbox.py tests/test_exception_inbox.py
git commit -m "feat(inbox): remind when next schedule is unpublished"
```

## Task 2: Make Any Open Inbox Count Red In The Menu

**Files:**
- Modify: `tests/test_exception_inbox.py`
- Modify: `src/zira_dashboard/static/footer.js`
- Modify: `src/zira_dashboard/static/footer.css`

**Interfaces:**
- Consumes: `/api/exceptions/summary` JSON with `total`, `urgent_total`, and `source_errors`.
- Produces: global Inbox nav state where `has-open` is the red alert state and `is-degraded` is only for source-error-only states.

- [ ] **Step 1: Write the failing static test**

Replace `test_footer_enhances_inbox_nav_with_summary_count()` in `tests/test_exception_inbox.py` with:

```python
def test_footer_enhances_inbox_nav_with_summary_count():
    js = (STATIC_DIR / "footer.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "footer.css").read_text(encoding="utf-8")

    assert "/api/exceptions/summary" in js
    assert "startInboxSummary(ensureInboxLink())" in js
    assert "readInboxSummaryBootstrap" in js
    assert "updateInboxSummaryLink(link, initial)" in js
    assert "window.gpiRefreshInboxSummary" in js
    assert "inbox-nav-count" in js
    assert "source_errors" in js
    assert "link.classList.toggle('has-open', total > 0)" in js
    assert "link.classList.toggle('is-degraded', degraded && total <= 0)" in js
    assert ".inbox-nav-count" in css
    assert ".inbox-nav-link.has-open" in css
    assert ".inbox-nav-link.has-open .inbox-nav-count" in css
    assert ".inbox-nav-link.is-degraded .inbox-nav-count" in css
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_exception_inbox.py::test_footer_enhances_inbox_nav_with_summary_count -v`

Expected: FAIL because `footer.js` still toggles `is-degraded` for all degraded states and `footer.css` has no `.inbox-nav-link.has-open` red styling.

- [ ] **Step 3: Write minimal implementation**

In `src/zira_dashboard/static/footer.js`, change this line inside `updateInboxSummaryLink()`:

```javascript
    link.classList.toggle('is-degraded', degraded);
```

to:

```javascript
    link.classList.toggle('is-degraded', degraded && total <= 0);
```

In `src/zira_dashboard/static/footer.css`, replace the urgent-only rule:

```css
  .inbox-nav-link.has-urgent .inbox-nav-count {
    background: #fee2e2;
    color: #b91c1c;
  }
```

with:

```css
  .inbox-nav-link.has-open {
    color: #b91c1c;
    font-weight: 700;
  }
  .inbox-nav-link.has-open .inbox-nav-count {
    background: #fee2e2;
    color: #b91c1c;
  }
```

Leave the existing `.inbox-nav-link.is-degraded .inbox-nav-count` rule after the red rule. Because `is-degraded` is now applied only when `total <= 0`, it will not override the red state when there are open to-dos.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_exception_inbox.py::test_footer_enhances_inbox_nav_with_summary_count -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/static/footer.js src/zira_dashboard/static/footer.css tests/test_exception_inbox.py
git commit -m "style(inbox): make open inbox count red"
```

## Final Verification

- [ ] Run focused tests:

```bash
pytest tests/test_exception_inbox.py tests/test_whatsnew_panel_static.py -v
```

Expected: PASS.

- [ ] Run a quick status check:

```bash
git status --short
```

Expected: only unrelated pre-existing worktree changes remain.
