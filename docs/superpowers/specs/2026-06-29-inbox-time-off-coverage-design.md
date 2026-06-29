# Inbox time-off coverage indicator — design

**Date:** 2026-06-29
**Status:** Approved (brainstorm), pending implementation plan

## Problem

When a time-off request lands in the inbox (`/exceptions`, the *Pending Time Off*
rows), the approver has no at-a-glance sense of how many other people are already
off during that window, or who they are. They have to leave the queue to check the
Who's-Out calendar or the separate approvals page. We want the coverage context
right on the row, so the approve/deny decision can be made in place.

A department-scoped coverage *count* already exists on the standalone
`/staffing/time-off/approvals` page (`time_off_context.coverage_for`). This feature
brings a richer version to the inbox itself.

## What we're building

On every *Pending Time Off* row in the inbox, a small **coverage chip** inline in
the detail line:

```
Maria Gonzalez   [Pending Time Off] [Approval]
Jul 6 – Jul 8 · Vacation · confirm · ▲ 4 off peak · 2 in Recycling      [Approve] [Deny]
```

Hovering the chip (or tapping it on touch) opens a tooltip with a **per-day
breakdown** of everyone else off during the request window — names, their
department, partial-day timing, pending-vs-approved marking, and plant-closure
lines.

### Display rules

- **Headline = peak day.** "4 off" is the most *other* people off on any single day
  the request spans. The requester is always excluded.
- **Department emphasis.** "2 in Recycling" = how many of the peak day's off-people
  are in the requester's own department (derived from their default work-center
  membership). Omitted when the requester maps to no department (plant-only scope).
- **What counts toward the number:** approved leaves (`state='validate'`) **plus**
  other pending requests, intersected day-by-day. Pending people are marked
  distinctly in the tooltip.
- **Holidays are a flag, not a number.** A plant-closure day means everyone is off,
  so it is never added to the people count. Instead the tooltip shows a
  `Plant closed — <name>` line for that day; if the entire window is closed, the
  chip says so.
- **Severity color** (default, tunable in code):
  - Amber when coverage looks thin — anyone in the requester's own department is off
    on the peak day, **or** the plant-wide peak is ≥ 3.
  - Green/quiet otherwise.
- **Zero others off:** a muted green `✓ no overlap` chip (positive signal for a
  clean approve), not hidden. When a day in the window is a plant closure, the
  closure flag takes display precedence over the zero-overlap text (the chip reads
  e.g. `Plant closed` rather than `✓ no overlap`).
- **Privacy:** approvers see *who* and *when* (including partial-day timing like
  "arrives 9:00am"), never the leave *type/reason* — consistent with the existing
  kiosk Who's-Out stance.

## Architecture

The data foundation already exists; this is an extension plus a UI surface.

### 1. Data layer — extend `src/zira_dashboard/time_off_context.py`

Two new functions, keeping logic and I/O separate so the logic is unit-testable:

- **`coverage_breakdown(approved_rows, pending_rows, holiday_dates, depts, date_from, date_to)`**
  — **pure, no DB.** Intersects each source day-by-day over the request window,
  excludes the requester, separates approved vs pending, attaches holiday lines,
  computes the peak day, the peak dept-count, scope, and severity. Returns:

  ```python
  {
    "severity": "warn" | "ok" | "clear",
    "peak_count": int,            # most OTHER people off on a single day
    "peak_date": date | None,
    "peak_dept_count": int,
    "scope": "department" | "plant",
    "has_holiday": bool,
    "by_day": [
      {
        "date": date,
        "count": int,
        "dept_count": int,
        "holiday": str | None,    # holiday name, or None
        "people": [
          {"name": str, "dept": str | None, "label": str,
           "pending": bool, "same_dept": bool}
        ],
      },
      # days that have someone off OR are a plant closure; capped (see edge cases)
    ],
  }
  ```

- **`coverage_breakdowns_for(rows)`** — **I/O.** Takes the inbox's shown time-off
  rows and runs **three batched DB queries** over the union date-range of all rows,
  plus one cached holiday fetch:
  1. approved leaves (`state='validate'`) overlapping the union range, joined to
     `people` for the name;
  2. other pending requests overlapping the union range, same join;
  3. departments for every involved person (requesters + everyone off), one
     `work_center_default_people → work_centers` lookup keyed by `odoo_id = ANY(...)`;
  4. public holidays via `odoo_client.fetch_public_holidays` (cached; fail-soft to
     `[]` on error, matching `_approved_by_day`) — not a DB query.

  Then calls the pure helper per row (excluding that row's requester) and returns a
  `{request_id: breakdown}` map. Fixed query count regardless of row count.

`time_off_context.coverage_for` (used by the approvals page) stays untouched.

### 2. Inbox builder — `src/zira_dashboard/exception_inbox.py`

In `_pending_time_off`, after shaping the rows, attach `row["coverage"]` from
`coverage_breakdowns_for`. No change to ordering, counts, or any other section.

### 3. Template — `src/zira_dashboard/templates/exceptions.html`

In the `time_off` branch of the detail line, render the chip + tooltip markup
(structure from approved mockup variant A), driven by `row.coverage`.

### 4. CSS

A scoped block (chip, tooltip, severity colors) following existing inbox styling.

### 5. JS

Hover is pure CSS. Add a minimal tap-to-toggle (open on tap, close on outside tap)
for touch, alongside the existing inbox script.

### Why batched queries

The inbox snapshot also feeds the 60s page-warmer and the topnav count, and the
project has a history of DB connection-pool exhaustion from fan-out renders. Three
fixed queries beat up to 24 (8 rows × 3 sources) per render.

## Testing

TDD — pure logic first.

- **`coverage_breakdown` unit tests** (the bulk): peak-day selection across a
  multi-day window; requester excluded; approved vs pending separation and marking;
  same-dept counting; holiday surfaced as a line and never in the count;
  partial-day labels; severity thresholds (amber on same-dept-off / plant peak ≥ 3,
  else green; zero → "clear").
- **Batch loader test:** stub DB + holiday fetch; assert each row gets a
  correctly-shaped `coverage` and exactly 3 queries fire.
- **Inbox builder test** (extend `tests/test_exception_inbox.py`): a pending row
  carries a `coverage` object of the expected shape.
- **Template render test:** chip renders in the time-off detail line; tooltip lists
  names; holiday line styled distinctly; zero-case shows the quiet chip.

## Edge cases

- **Holiday fetch fails** → empty list; feature degrades to approved + pending
  (matches existing `_approved_by_day` fail-soft behavior).
- **Requester has no department** → plant-only scope; dept emphasis omitted (reuse
  the `department_for_person` empty-set fallback).
- **Long windows** → the tooltip lists only days that actually have someone off
  (empty days skipped); cap at ~10 off-days with a "+ N more days" line so the
  tooltip never runs off-screen.
- **Unresolved names** (person not in `people`) → fall back to `#<odoo_id>`, as the
  rest of the inbox does.
- **Requester's own other pending requests** → excluded; we count other *people*,
  not the same person's other rows.

## Out of scope

- Changing the standalone `/staffing/time-off/approvals` page or its
  `coverage_for`. (A later pass could unify both on the richer breakdown.)
- Live, no-reload updates of the coverage chip (the inbox's Phase 4b live-polling
  work is tracked separately).
- Counting holidays as headcount.
