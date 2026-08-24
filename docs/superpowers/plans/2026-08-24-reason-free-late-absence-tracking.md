# Reason-Free Late and Absence Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let supervisors mark an absence or a 60-minute running-late snooze without a reason, and automatically record every scheduled eligible employee who punches in more than five minutes late with exact minutes on their employee page.

**Architecture:** The existing Late / Absence payload remains the common source for the Exception Inbox and footer. Its 20-second background warm records confirmed late punches idempotently. A nullable `minutes_late` column preserves old history while supplying the exact duration for new records.

**Tech Stack:** Python 3.11+, FastAPI, PostgreSQL, Jinja2, vanilla JavaScript, pytest, Ruff.

## Global Constraints

- Retain the 15-minute missing-clock-in alert threshold.
- Record late only for a first punch strictly more than 5 minutes after shift start.
- Use the existing eligible population: scheduled, hourly, fixed-schedule people.
- A same-day manual absence prevents automatic late recording.
- Running-late is a 60-minute transient snooze, never attendance history.
- Preserve legacy reason values in the database, but do not request, render, or edit them.
- Do not drop the legacy `late_expected_arrivals` table; remove it from the active report path only.
- Before the implementation push, replace the 2026-08-24 planned What's New bullet with completed user-facing wording.

---

## File Structure

- `src/zira_dashboard/attendance.py` — calculate lateness after five minutes, even for a clocked-out record.
- `src/zira_dashboard/_schema.py` and `src/zira_dashboard/late_report.py` — store and return immutable exact minutes.
- `src/zira_dashboard/routes/staffing.py` and `src/zira_dashboard/routes/late_report.py` — automatically record confirmed late arrivals and make absence/running-late actions reason-free.
- `src/zira_dashboard/exception_inbox.py`, `src/zira_dashboard/templates/exceptions.html`, `src/zira_dashboard/static/exceptions.js`, and `src/zira_dashboard/static/footer.js` — remove reason/expected-time controls from both report surfaces.
- `src/zira_dashboard/routes/people.py` and `src/zira_dashboard/templates/player_card.html` — render date, type, and minutes late without a reason editor.
- `tests/test_attendance.py`, `tests/test_late_report.py`, `tests/test_late_report_running_late.py`, `tests/test_late_report_absence_odoo.py`, `tests/test_late_report_excludes_unscheduled.py`, `tests/test_exception_inbox.py`, `tests/test_exception_inbox_breakdown_template.py`, and `tests/test_player_card.py` — behavior coverage.
- `CHANGELOG.md` — user-facing patch note.

### Task 1: Capture and Store Exact Lateness

**Files:**

- Modify: `src/zira_dashboard/attendance.py:16-54`
- Modify: `src/zira_dashboard/_schema.py:349-357`
- Modify: `src/zira_dashboard/late_report.py:16-18, 67-71, 481-539`
- Modify: `tests/test_attendance.py:14-26`
- Modify: `tests/test_late_report.py:174-225`

**Interfaces:**

- Consumes: `attendance.compute_status(punches, ids, now_local, shift_start_local, grace_minutes=...) -> dict[str, dict]`.
- Produces: `late_report.record_late_arrival(day, emp_id, name, minutes_late) -> None`, `late_report.clear_snooze(day, emp_id) -> None`, and `late_arrivals_history_for_name(...) -> list[{day, minutes_late}]`.

- [ ] **Step 1: Write the failing attendance and persistence tests**

Add this boundary test to `tests/test_attendance.py`:

```python
def test_compute_status_marks_six_minutes_late_by_default():
    shift_start = _shift_start()
    punches = {
        "6": {
            "first_check_in": _utc_iso(shift_start + timedelta(minutes=6)),
            "currently_open": True,
        }
    }

    out = attendance.compute_status(punches, ["6"], shift_start, shift_start)

    assert out["6"]["status"] == "late"
    assert out["6"]["minutes_late"] == 6
```

Add this database-gated test to `tests/test_late_report.py`:

```python
@requires_db
def test_record_late_arrival_keeps_the_first_exact_minutes():
    d = date(2026, 5, 7)
    db.execute("DELETE FROM late_arrivals WHERE day = %s AND emp_id = %s", (d, "777"))
    late_report.record_late_arrival(d, "777", "Late Person", 14)
    late_report.record_late_arrival(d, "777", "Late Person", 19)

    rows = db.query(
        "SELECT name, reason, minutes_late FROM late_arrivals WHERE day = %s AND emp_id = %s",
        (d, "777"),
    )
    assert rows == [{"name": "Late Person", "reason": None, "minutes_late": 14}]
```

Update the existing history assertion to `[{"day": d2, "minutes_late": 18}]`.

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest tests/test_attendance.py tests/test_late_report.py -q`

Expected: FAIL because default grace is seven minutes and the new storage function/column do not exist.

- [ ] **Step 3: Add the idempotent schema and data helpers**

Set `GRACE_MINUTES = 5` and change `compute_status` so it calculates minutes before choosing `clocked_out`, `late`, or `on_time`:

```python
minutes_late = max(0, int((ci_local - shift_start_local).total_seconds() // 60))
entry["minutes_late"] = minutes_late
if not entry["currently_open"]:
    entry["status"] = "clocked_out"
elif minutes_late > grace_minutes:
    entry["status"] = "late"
else:
    entry["status"] = "on_time"
```

Add idempotent DDL to `_schema.py`:

```sql
ALTER TABLE late_arrivals
  ADD COLUMN IF NOT EXISTS minutes_late INTEGER
  CHECK (minutes_late IS NULL OR minutes_late > 0);
```

In `late_report.py`, set `DEFAULT_SNOOZE_MINUTES = 60`, add `AUTO_RECORD_LATE_AFTER_MINUTES = 5`, and implement:

```python
def clear_snooze(day, emp_id: str) -> None:
    db.execute(
        "DELETE FROM late_snoozes WHERE day = %s AND emp_id = %s",
        (day, str(emp_id)),
    )

def record_late_arrival(day, emp_id: str, name: str, minutes_late: int) -> None:
    if minutes_late <= AUTO_RECORD_LATE_AFTER_MINUTES:
        return
    db.execute(
        """
        INSERT INTO late_arrivals (day, emp_id, name, reason, minutes_late)
        VALUES (%s, %s, %s, NULL, %s)
        ON CONFLICT (day, emp_id) DO NOTHING
        """,
        (day, str(emp_id), name, minutes_late),
    )
```

Make attendance-history helpers return only `{day}` for absences and `{day, minutes_late}` for late arrivals.

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `pytest tests/test_attendance.py tests/test_late_report.py -q`

Expected: PASS; Postgres-backed tests may be SKIPPED without `DATABASE_URL`.

- [ ] **Step 5: Commit the completed persistence unit**

```bash
git add src/zira_dashboard/attendance.py src/zira_dashboard/_schema.py src/zira_dashboard/late_report.py tests/test_attendance.py tests/test_late_report.py
git commit -m "feat: record exact late arrival minutes"
```

### Task 2: Automate Late Recording and Make Server Actions Reason-Free

**Files:**

- Modify: `src/zira_dashboard/routes/staffing.py:3089-3256`
- Modify: `src/zira_dashboard/routes/late_report.py:38-154, 281-393`
- Modify: `tests/test_late_report_excludes_unscheduled.py:23-83`
- Modify: `tests/test_late_report_running_late.py:1-260`
- Modify: `tests/test_late_report_absence_odoo.py:1-125`

**Interfaces:**

- Consumes: Task 1 helpers plus `late_report.absent_emp_ids_for_day(day)`.
- Produces: a report payload with `scheduled_late`, `unscheduled_late`, `snoozed`, legacy `late`, `count`, and `today`; it has no `needs_reason` or `running_late` section.

- [ ] **Step 1: Write failing payload and endpoint tests**

In `tests/test_late_report_running_late.py`, replace expected-arrival tests with a mocked payload test asserting one automatic record for a six-minute scheduled punch:

```python
record = MagicMock()
monkeypatch.setattr(staffing_routes.late_report, "record_late_arrival", record)
# Use the existing payload fixture shape: scheduled_ids=["7"], status="late",
# minutes_late=6, and id_to_name {"7": "Jesus Galindo"}.
payload = staffing_routes.late_report_payload(force=True)

record.assert_called_once_with(FIXED_DAY, "7", "Jesus Galindo", 6)
assert payload["scheduled_late"] == []
assert "needs_reason" not in payload
```

Add companion tests where five minutes causes no write, a same-day absence causes no write, and an active snooze is cleared when the employee has punched. Add a Running Late endpoint test that calls `_running_late_sync({"emp_id": "7", "name": "Jesus Galindo"})` and expects `late_report.snooze(FIXED_DAY, "7", "Jesus Galindo", 60)` plus cache invalidation. Update absence tests to post no reason and assert Odoo receives `reason=""` while `late_report.declare_absent` receives `reason=None`.

- [ ] **Step 2: Run the new server-side tests to verify they fail**

Run: `pytest tests/test_late_report_running_late.py tests/test_late_report_absence_odoo.py tests/test_late_report_excludes_unscheduled.py -q`

Expected: FAIL because the payload emits `needs_reason`, absence requires a reason, and Running Late requires a clock time.

- [ ] **Step 3: Implement automatic recording and direct action contracts**

Add this focused helper to `routes/staffing.py` and call it after computing eligible scheduled ids and the `id_to_name` map, before generating no-punch rows:

```python
def _record_confirmed_late_arrivals(
    day, scheduled_ids, eligible_ids, attendance_by_id, absent_ids,
    already_recorded_ids, id_to_name,
) -> set[str]:
    recorded: set[str] = set()
    for raw_emp_id in scheduled_ids:
        emp_id = str(raw_emp_id)
        if emp_id not in eligible_ids or emp_id in absent_ids or emp_id in already_recorded_ids:
            continue
        minutes_late = int((attendance_by_id.get(emp_id) or {}).get("minutes_late") or 0)
        if minutes_late <= late_report.AUTO_RECORD_LATE_AFTER_MINUTES:
            continue
        late_report.record_late_arrival(
            day, emp_id, id_to_name.get(emp_id) or f"Unknown ({emp_id})", minutes_late
        )
        recorded.add(emp_id)
    return recorded
```

Merge its result into `already_recorded_late_ids`, clear only active snoozes whose status is no longer `no_punch`, and remove `late_expected_arrivals`, `running_late`, and `needs_reason` handling from the payload and `late_people_for_day_v2` contract.

In `routes/late_report.py`, remove required-reason validation. Pass `""` to Odoo absence helpers, `None` to the local `declare_absent` and inbox log. Remove the manual save-late endpoint. Make `_running_late_sync` validate only id/name, call `late_report.snooze(today, emp_id, name, 60)`, bust caches, and return `{ "ok": True, "minutes": 60 }`.

- [ ] **Step 4: Run the focused server-side tests to verify they pass**

Run: `pytest tests/test_late_report_running_late.py tests/test_late_report_absence_odoo.py tests/test_late_report_excludes_unscheduled.py tests/test_page_warmer.py -q`

Expected: PASS; the existing warmer test still proves `late_report_payload(force=True)` is refreshed by the 20-second inbox tick.

- [ ] **Step 5: Commit the completed automatic-recording unit**

```bash
git add src/zira_dashboard/routes/staffing.py src/zira_dashboard/routes/late_report.py tests/test_late_report_running_late.py tests/test_late_report_absence_odoo.py tests/test_late_report_excludes_unscheduled.py
git commit -m "feat: automatically record late clock-ins"
```

### Task 3: Simplify Both Late / Absence User Interfaces

**Files:**

- Modify: `src/zira_dashboard/exception_inbox.py:335-485`
- Modify: `src/zira_dashboard/templates/exceptions.html:110-143`
- Modify: `src/zira_dashboard/static/exceptions.js:181-190, 571-645, 834-846`
- Modify: `src/zira_dashboard/static/footer.js:638-859`
- Modify: `tests/test_exception_inbox.py:108-118, 179-205, 1408-1501`
- Modify: `tests/test_exception_inbox_breakdown_template.py:81-87`

**Interfaces:**

- Consumes: Task 2's simplified late-report payload.
- Produces: direct `Absent` and `Running Late — 60 min` controls for `late_absence`; no `late_reason` action or expected-arrival-time UI.

- [ ] **Step 1: Write failing rendered-page and static-handler tests**

Replace the Inbox template test with:

```python
assert ">Absent</button>" in response.text
assert ">Running Late — 60 min</button>" in response.text
assert "js-reason-input" not in response.text
assert "js-running-late-time" not in response.text
assert "js-running-late-save" not in response.text
```

Add static JavaScript assertions:

```python
assert "minutes: 60" in js
assert "reason: absentReason" not in js
assert "/api/late-report/save-late-arrival" not in js
```

Update snapshot fixtures to omit `needs_reason` and `running_late`, then assert a snoozed employee is the only muted late follow-up.

- [ ] **Step 2: Run the UI-focused tests to verify they fail**

Run: `pytest tests/test_exception_inbox.py tests/test_exception_inbox_breakdown_template.py -q`

Expected: FAIL because reason fields and expected-arrival controls are still rendered and mapped.

- [ ] **Step 3: Implement the two-button flow in both surfaces**

Delete `needs_reason` and `running_late` row mapping from `exception_inbox.py`; count only snoozes as late follow-up work.

For `late_absence` in `exceptions.html`, retain Missed Punch but render only:

```html
<button type="button" class="row-btn warn js-absent">Absent</button>
<button type="button" class="row-btn js-running-late">Running Late — 60 min</button>
```

Delete the reason select/input, save-late branch, expected-time input/confirmation, and separate Snooze button. In `exceptions.js`, remove `requireReason`, quick-pick handling, time handling, and manual late save. Post these exact payloads:

```javascript
postJson('/api/late-report/declare-absent', { emp_id: empId, name: personName })
postJson('/api/late-report/running-late', { emp_id: empId, name: personName })
```

Resolve the first as `Marked absent` with its event id; resolve the second as `Re-checks in 60 min`. Update shared-badge logic to recognise only `late_absence`.

In `footer.js`, delete `reasonRow`, `needs_reason`, and reason listeners. Render `Mark Absent` and `Running Late — 60 min` for no-punch rows with the same endpoints; close the modal after successful Running Late. Change empty help text to mention only a scheduled employee who has not clocked in 15 minutes after start.

- [ ] **Step 4: Run the UI-focused tests to verify they pass**

Run: `pytest tests/test_exception_inbox.py tests/test_exception_inbox_breakdown_template.py -q`

Expected: PASS, with the screenshot's Exception Inbox using the direct two-button workflow.

- [ ] **Step 5: Commit the completed interaction unit**

```bash
git add src/zira_dashboard/exception_inbox.py src/zira_dashboard/templates/exceptions.html src/zira_dashboard/static/exceptions.js src/zira_dashboard/static/footer.js tests/test_exception_inbox.py tests/test_exception_inbox_breakdown_template.py
git commit -m "feat: simplify late absence actions"
```

### Task 4: Show Reason-Free Attendance History and Release It

**Files:**

- Modify: `src/zira_dashboard/routes/people.py:259-275, 337-367`
- Modify: `src/zira_dashboard/templates/player_card.html:372-425`
- Modify: `tests/test_player_card.py:55-108`
- Modify: `CHANGELOG.md:12-18`

**Interfaces:**

- Consumes: `absences_history_for_name(...) -> list[{day}]` and `late_arrivals_history_for_name(...) -> list[{day, minutes_late}]`.
- Produces: `attendance_rows` with `{date, type, minutes_late}`; no reason-edit endpoint.

- [ ] **Step 1: Write the failing employee-page test**

Replace the reason test data and assertions with:

```python
abs_rows = [{"day": date(2026, 5, 6)}]
late_rows = [{"day": date(2026, 5, 7), "minutes_late": 17}]

assert "Days Absent" in html and "Days Late" in html
assert "Minutes Late" in html
assert "17 min" in html
assert "Reason" not in html
assert "contenteditable" not in html
assert "/attendance/reason" not in html
```

Retain date-link and empty-history assertions.

- [ ] **Step 2: Run the employee-page test to verify it fails**

Run: `pytest tests/test_player_card.py -q`

Expected: FAIL because the route still supplies `reason`, the template has an editable Reason column, and the route exposes the old reason endpoint.

- [ ] **Step 3: Render minutes and remove the reason editor**

Build employee rows as:

```python
attendance_rows = (
    [{"date": r["day"].isoformat(), "type": "Absent", "minutes_late": None} for r in abs_rows]
    + [
        {"date": r["day"].isoformat(), "type": "Late", "minutes_late": r["minutes_late"]}
        for r in late_rows
    ]
)
```

Delete `update_attendance_reason`. Replace the third player-card table header/cell with:

```html
<tr><th>Date</th><th>Type</th><th class="num">Minutes Late</th></tr>
<td class="num">{% if r.minutes_late is not none %}{{ r.minutes_late }} min{% else %}—{% endif %}</td>
```

Remove row data attributes and the `_saveAttendanceReason` script. Replace the existing 2026-08-24 plan note with:

```markdown
- **Late and absence reports are now quicker.** Mark an absence or tell Plant Manager to check again in one hour without writing a reason. When someone clocks in more than five minutes late, their page saves the date and how many minutes late they were.
```

- [ ] **Step 4: Run focused and complete validation**

Run:

```bash
pytest tests/test_attendance.py tests/test_late_report.py tests/test_late_report_running_late.py tests/test_late_report_absence_odoo.py tests/test_late_report_excludes_unscheduled.py tests/test_exception_inbox.py tests/test_exception_inbox_breakdown_template.py tests/test_player_card.py tests/test_page_warmer.py -q
ruff check src tests
pytest -q
```

Expected: all runnable tests PASS; database-only coverage may be SKIPPED locally without `DATABASE_URL`; Ruff reports no errors.

- [ ] **Step 5: Commit and push the completed feature**

```bash
git add src/zira_dashboard/routes/people.py src/zira_dashboard/templates/player_card.html tests/test_player_card.py CHANGELOG.md
git commit -m "feat: show late minutes on employee pages"
git push origin main
```

## Plan Self-Review

- **Spec coverage:** Task 1 implements exact minutes and durable storage. Task 2 implements automatic, one-per-day recording, absence precedence, snooze clearing, and reason-free actions. Task 3 updates both the Exception Inbox screenshot surface and footer report. Task 4 supplies the employee history/count view and release validation.
- **Placeholder scan:** No incomplete steps, deferred code, unspecified error handling, or unbound interfaces remain.
- **Type consistency:** New automatic writes use a positive integer. Late history carries `{day, minutes_late}`; absences use `{day}` and render an em dash. Both UIs call `/api/late-report/running-late`, whose handler calls `late_report.snooze(..., 60)`.
