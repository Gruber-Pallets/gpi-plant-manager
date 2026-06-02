# Declared-Absent → "Absent" in the Scheduler Time Off — Design

## Context

When a manager declares someone absent in the Late/Absence Report, it writes a row to `manual_absences` (keyed by `person_id`, with an optional `reason`). Today that does two things:
- Excuses the person from the Late/Absence report (they stop showing as "late").
- Drops them from the Recycling/New department man-hours (`attendance.full_day_absent_names`).

It does **nothing on the staffing scheduler**: the person stays in their work-center slot, stays in the Unscheduled list, and never appears in the Time Off panel. This design closes that gap — a declared-absent person should behave like a full-day time-off entry: out of the assignable lists and the station slot, shown in Time Off as "Absent" in light red.

**Key discovery — the hooks already exist (dormant from the StratusTime era):**
- Scheduler time-off entries carry `manual_absent` / `derived` flags; the template ([staffing.html:85](../../../src/zira_dashboard/templates/staffing.html)) already maps `derived or manual_absent` → an `absent` CSS class.
- `.timeoff .time-off-row.absent` ([staffing.css:71](../../../src/zira_dashboard/static/staffing.css)) is **already light red** (`background:#fee2e2`, red border, dark-red text) — matches the request with no CSS change.
- A full-day entry (`hours: None`) is already added to `time_off_set`, which excludes the person from the assignable pool, Unscheduled, and Reserves.

**The one real gap:** an already-*assigned* person is **not** removed from their station. The station summary (`visible_assigned`, [staffing.html:234](../../../src/zira_dashboard/templates/staffing.html)) only rejects people who already have a production attribution — not time-off/absent people. And the headcount (`count = len(assigned)`) and publish validation count them. So "pulling them out of the schedule" requires a display/count change in addition to emitting the entry.

## Goals

1. A **manually declared-absent** person appears in the scheduler's Time Off panel as a full-day **"Absent"** entry, styled **light red**.
2. They are removed from the **assignable pool, Unscheduled, and Reserves** (already happens once they're a full-day entry).
3. They are removed from any **assigned work-center slot** they held — from the slot's **display and its headcount/publish count** — so the gap is visible and promptable for backfill.
4. The underlying **saved assignment is preserved**: "undo absent" (existing) restores the person to their slot with no re-assignment.
5. **Scope:** applies to **all full-day time off** (declared-absent *and* approved full-day leave) for the station-slot removal — one consistent rule: if you're out for the day, you don't show staffing a station.

## Non-goals

- Auto-detected no-shows (scheduled, no punch) do **not** trigger this — manual declares only (a no-punch person may just be late). They keep showing in the Late/Absence report as today.
- No change to how absences feed the dashboards (`attendance.full_day_absent_names` already includes manual absences via a separate path — no double-count).
- No new "reason" capture UI; we surface the existing `manual_absences.reason` if present, else the literal "Absent".
- Partial-day time off is unaffected (still shown as a badge on the schedulable roster).

## Design

### 1. Emit "Absent" entries (`scheduler_time_off.time_off_entries_for_day`)
After building the Odoo-mirror entries, append one **full-day** entry per declared-absent person for `day`, sourced from `late_report.absences_for_day(day)` (already filters archived/excluded people):

```
{ name, hours: None, pay_type: "Absent", time_range: "",
  timing_label: "Absent",            # or "Absent — <reason>" if a reason was captured
  derived: False, manual_absent: True, pending: False }
```

- `hours: None` → counted as full-day → flows into `full_day_entries` → `time_off_set` (route) → excluded from pool/Unscheduled/Reserves, and into `time_off_names` (template).
- `manual_absent: True` → template `is_absent` → `.absent` CSS (light red) + "Absent" meta.
- **Dedupe:** if a name is already a full-day Odoo leave for that day, keep one entry; the Absent flag wins (so it renders red). Implementation: build the absent set first, skip an Odoo full-day entry whose name is in it, then append the Absent entries.

### 2. Free the assigned slot (display + count), preserve the assignment
In `routes/staffing.py`, per work-center row, derive a **present** view that excludes full-day-off/absent names (`time_off_set`) while leaving the saved `assigned`/`assigned_set` (which drive the picker checkboxes and form save) untouched:

- `present_assigned = [a for a in assigned if a["name"] not in time_off_set]`
- Use `present_assigned` for: the station summary, the headcount `count`/status (`empty`/`under`/`over`), and the publish-block validation message (`requires N operators — currently len(present_assigned)`).
- Keep `assigned`/`assigned_set` (full) for the picker dropdown's checked state and the `loc__{wc}` form inputs → **saving the schedule does not un-assign the absent person**, and **undo-absent makes them reappear** (the absent overlay is render-only; `sched.assignments` is never mutated by declaring absent).

Template: render the station summary from a per-row `present_assigned` (added to the `row` dict next to `assigned`), replacing the current `row.assigned`-based `visible_assigned` ([staffing.html:234](../../../src/zira_dashboard/templates/staffing.html)). The existing "already has a production attribution" rejection still applies on top of it.

### 3. Reason + color
- Label/meta: "Absent" (or "Absent — <reason>" when `manual_absences.reason` is set).
- Color: none needed — dormant `.absent` style already light red.

## Data flow

`manual_absences` (declare-absent, existing) → `late_report.absences_for_day(day)` → `scheduler_time_off.time_off_entries_for_day(day)` appends Absent full-day entries → `routes/staffing.py` `time_off_entries`/`time_off_set`/`present_assigned` → template (Time Off panel red + station summary/headcount minus absent) + existing JS `__timeOffNames` (Unscheduled/Reserves sweep). Undo: `late_report.undo_absent` (existing) removes the `manual_absences` row → next render drops the Absent entry and `present_assigned` includes them again.

## Edge cases

- **Assigned + absent:** hidden from station summary/count, still checked in the picker (assignment preserved); undo restores. ✓
- **Absent + on Odoo full-day leave (same day):** one entry, Absent styling. ✓
- **Absent person not assigned anywhere:** simply shows in Time Off; nothing to strip. ✓
- **Past/future day view:** `absences_for_day(day)` is per-day, so a historical day shows that day's absences consistently. ✓
- **Reserve who is absent:** excluded from Reserves (time_off_set) and shown in Time Off. ✓

## Testing

- `scheduler_time_off.time_off_entries_for_day`: a declared-absent person (stub `late_report.absences_for_day`) yields a full-day entry with `manual_absent=True`, `pay_type="Absent"`, `hours=None`; deduped against an Odoo full-day leave of the same name.
- `routes/staffing` (TestClient, venv): declaring a person absent → they're absent from Unscheduled and from the WC summary, the WC headcount drops by one (slot reads short), they appear in the Time Off panel with the `absent` class; the `loc__{wc}` input for them is still present (assignment preserved). Undo → back in the slot.
- Run via `ZIRA_API_KEY=test .venv/bin/python -m pytest`.

## Done criteria

- Declaring someone absent moves them into Time Off as "Absent" (light red), out of Unscheduled and their station slot, with the station headcount reflecting the gap.
- Undo-absent restores them to their original slot.
- Full suite stays green.
