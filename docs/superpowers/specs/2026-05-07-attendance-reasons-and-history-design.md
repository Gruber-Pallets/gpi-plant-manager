# Attendance Reasons + History on the Player Card — Design

**Date:** 2026-05-07
**Status:** Approved (brainstorming → implementation planning)

## Context

Today's late/absence report on the global footer surfaces scheduled
people who haven't punched in past `shift_start + 15 min`. Two
actions per row: **Snooze 30 min** and **Declare Absent**. Behind the
scenes, declared absences land in the `manual_absences` table; snoozes
in `late_snoozes`. Neither row carries any reason metadata.

Dale reported two gaps today:

1. Gerardo Vergara wasn't on today's schedule, didn't punch in, and
   didn't appear in the late report — because
   `late_people_for_day(scheduled_emp_ids, ...)` filters strictly to
   scheduled people. The /staffing page already has an "Unscheduled"
   left-rail concept (active non-reserve people not assigned to any
   WC today and not on time off); that population isn't fed into the
   report.
2. There's no record of *why* anyone was absent or late — and no
   record of late arrivals at all. Once a late operator finally
   punches in, they vanish from the report and nothing's saved.

Goal: capture absent/late-arrival events with a short reason and
surface the history on the player card alongside the existing
per-WC summary and per-day breakdown.

## Goals

1. Late/absence report covers both scheduled AND unscheduled
   no-punch people. Same Snooze and Declare-Absent actions.
2. Auto-detect late arrivals — when someone punches in past
   `shift_start + 15 min`, the report surfaces a "Reason needed"
   entry until a reason is captured.
3. Capture an optional short text reason on Declare-Absent and on
   late-arrival entries. Quick-pick buttons for the three common
   reasons Dale named: **Sick · Car issues · Overslept**, plus
   **Other** (text-only fallback).
4. Player card grows an Attendance section showing per-day absent /
   late history with reasons in the requested date range. Reason
   cells are editable from the card so a "(no reason)" row from
   today can be filled in later.
5. Two new totals on the player-card stat tile row: **Days Absent**
   and **Days Late**.

## Non-goals

- Snoozes don't get a reason field. They're transient — by the time
  one expires, the same person is back in the report and gets
  treated through the normal absent / late paths.
- No calendar / heatmap / chart of attendance. The table is enough.
- No auto-conversion when a declared-absent person later punches in.
  If you marked someone absent and they show up, you manually undo
  the absence (existing button) and they go through the normal
  late-arrival auto-detect.
- No bulk actions. Each row is handled individually in the popup.
- No per-person reason patterns / suggested defaults. Quick-picks are
  the same four buttons for everyone.
- No per-WC or per-team rollups. Stats are scoped to the single
  player card.
- No StratusTime push integration — we already poll punch state via
  `attendance_for_day`; that's sufficient.

## Design

### 1. Schema changes

Add a `reason` column to `manual_absences` (nullable):

```sql
ALTER TABLE manual_absences ADD COLUMN IF NOT EXISTS reason TEXT;
```

New `late_arrivals` table mirroring `manual_absences`'s shape:

```sql
CREATE TABLE IF NOT EXISTS late_arrivals (
  day            DATE NOT NULL,
  emp_id         TEXT NOT NULL,
  name           TEXT NOT NULL,
  reason         TEXT,
  declared_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, emp_id)
);
CREATE INDEX IF NOT EXISTS late_arrivals_day_idx ON late_arrivals(day);
```

Both tables stay keyed on `(day, emp_id)`. Idempotent INSERT…ON
CONFLICT DO UPDATE on writes so a re-Save just updates the reason.

### 2. Late-report API: three sections

`GET /api/late-report` response gains two fields and renames `late`:

```json
{
  "count": 5,
  "scheduled_late": [{"emp_id": "...", "name": "...", "minutes_late": 23}, ...],
  "unscheduled_late": [{"emp_id": "...", "name": "..."}, ...],
  "needs_reason": [{"emp_id": "...", "name": "...", "minutes_late": 31}, ...],
  "snoozed":     [{"emp_id": "...", "name": "...", "mins_remaining": 12}, ...]
}
```

`count` = `scheduled_late + unscheduled_late + needs_reason` (the
nav-bar badge number).

**Backwards compatibility:** the existing `late` field stays in the
response as an alias for `scheduled_late` so any cached client
JS doesn't break mid-deploy. Removed in a follow-up.

#### scheduled_late (existing logic, unchanged)

Per `late_report.late_people_for_day(day, scheduled_emp_ids, ...)`.
No change.

#### unscheduled_late (new)

Active non-reserve roster people who:
- Are NOT in `scheduled_emp_ids` for the day
- Are NOT on time off (StratusTime full-day or non-work shifts)
- Are NOT already declared absent for the day
- Are NOT currently snoozed
- Have attendance status `no_punch` past `shift_start + 15 min`

Reuses the same threshold and the same attendance dict the
scheduled-late path already builds — no new StratusTime fetches.

#### needs_reason (new)

People who:
- Punched in (attendance status NOT `no_punch`) at a punch time past
  `shift_start + 15 min`, AND
- Don't yet have a row in `late_arrivals` for the day, AND
- Aren't declared absent for the day

The minutes-late number on each entry is `punch_time - shift_start`.

### 3. Late-report popup UI

The existing modal at `_footer.html` grows two sections:

```
┌─ Late / Absence Report ──────────────────────────────────────┐
│                                                              │
│ Scheduled — haven't clocked in (15+ min past shift start)    │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ Isaac Miller  · 23 min late                              │ │
│ │ [Snooze 30 min] [Declare Absent▾]                        │ │
│ └──────────────────────────────────────────────────────────┘ │
│                                                              │
│ Unscheduled — also haven't clocked in                        │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ Gerardo Vergara                                          │ │
│ │ [Snooze 30 min] [Declare Absent▾]                        │ │
│ └──────────────────────────────────────────────────────────┘ │
│                                                              │
│ Late arrivals — reason needed                                │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ Carlos Jimenez · clocked in 32 min late                  │ │
│ │ [Sick] [Car issues] [Overslept] [Other]                  │ │
│ │ [Reason ────────────────────────────] [Save]             │ │
│ └──────────────────────────────────────────────────────────┘ │
│                                                              │
│ Snoozed                                                      │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ Lupe Hernandez · re-checks in 18 min                     │ │
│ └──────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

**Declare Absent** button becomes a small split control: clicking
the main button reveals an inline reason editor (same four
quick-picks + text input + Save) right under the row, then commits
on Save. This keeps the affirmative-by-default behavior but adds
the reason capture without an extra click for users who just want
to mark someone absent without explanation (Save with empty reason
is fine).

**Quick-pick buttons** populate the text input on click. The Save
action sends the input value verbatim — Other is just the text
field with no pre-fill.

**No reason on snooze.** Snoozes are transient and the same person
re-surfaces in 30 min anyway.

### 4. New endpoints

```
POST /api/late-report/save-late-arrival
  body: {emp_id, name, reason}
  → INSERT INTO late_arrivals (day, emp_id, name, reason)
    ON CONFLICT (day, emp_id) DO UPDATE SET reason = EXCLUDED.reason
  → returns {ok: true}

POST /api/late-report/declare-absent
  (existing endpoint, body extended)
  body: {emp_id, name, reason?}      ← reason now optional
  → existing INSERT extended to also write reason
```

Existing `/api/late-report/snooze` and `/api/late-report/undo-absent`
unchanged.

### 5. Player-card Attendance section

In `routes/people.py:staffing_player_card`, after computing `day_rows`
also compute:

```python
attendance_rows = [
    {"date": d, "type": "Absent", "reason": r["reason"] or ""},
    {"date": d, "type": "Late",   "reason": r["reason"] or ""},
    ...
]
```

Sorted newest-first. Date range respects the existing `?start=&end=`
query params. Resolution: query both `manual_absences` and
`late_arrivals` for `name = $name AND day BETWEEN $start AND $end`,
union, sort.

Two new totals on the existing `.pc-totals` row:

```html
<div class="stat"><div class="lab">Days Absent</div><div class="v">3</div></div>
<div class="stat"><div class="lab">Days Late</div><div class="v">7</div></div>
```

New table on `templates/player_card.html`, rendered after the per-day
breakdown. Hidden when `attendance_rows` is empty:

```jinja
{% if attendance_rows %}
<h3 style="margin-top:1rem">Attendance</h3>
<table class="pc">
  <thead><tr><th>Date</th><th>Type</th><th>Reason</th></tr></thead>
  <tbody>
    {% for r in attendance_rows %}
    <tr data-attendance-key="{{ r.type|lower }}|{{ r.date }}">
      <td><a href="/recycling?start={{ r.date }}&end={{ r.date }}">{{ r.date }}</a></td>
      <td>{{ r.type }}</td>
      <td class="attendance-reason"
          contenteditable="true"
          data-original="{{ r.reason }}"
          >{{ r.reason }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% endif %}
```

**Inline-editable reason cells.** `contenteditable="true"` plus a
`blur` handler that POSTs the new value to a small update endpoint:

```
POST /api/staffing/people/{name}/attendance/reason
  body: {date, type: "absent"|"late", reason}
  → UPDATE manual_absences SET reason = ... WHERE day = ... AND name = ...
    (or late_arrivals, depending on type)
```

Look up by `name` (not `emp_id`) on the player card path — the URL
already keys on name, and it's unambiguous within a day.

### 6. Components and data flow

```
[StratusTime punch arrives]
        ↓
attendance_for_day(d) — already polled every 60s
        ↓
late_report.* expanded:
  • scheduled_late (existing)
  • unscheduled_late (NEW: active non-reserve no-punch + not assigned)
  • needs_reason (NEW: punched in late + no late_arrivals row)
        ↓
GET /api/late-report polled by global footer
        ↓
modal renders 4 sections
        ↓
user clicks Save on a needs_reason row
        ↓
POST /api/late-report/save-late-arrival → INSERT INTO late_arrivals
        ↓
next poll: row drops out of needs_reason
        ↓
appears on /staffing/people/{name} Attendance section
```

## Testing

**Unit tests** (`tests/test_late_report.py`, extend):

1. `test_late_people_unscheduled_branch_includes_unscheduled_no_punch` —
   given a roster with one scheduled-no-punch and one unscheduled-no-punch,
   both appear in the right sections.
2. `test_late_people_needs_reason_branch_skips_already_recorded` —
   a person who's punched in late but already has a `late_arrivals` row
   does not appear in `needs_reason`.
3. `test_declare_absent_persists_optional_reason` — POST with a
   reason saves it; POST without saves NULL.
4. `test_save_late_arrival_upserts_reason` — first POST inserts;
   second POST with a different reason updates the same row.

**Route tests** (`tests/test_player_card.py`, extend):

5. `test_player_card_renders_attendance_section_with_reasons` —
   given mocked manual_absences + late_arrivals rows for the
   person, renders the Attendance table with reason cells and the
   two new stat tiles.
6. `test_player_card_hides_attendance_section_when_empty` — no
   rows → no `<h3>Attendance</h3>` in the HTML.

**Manual / visual:**

- Open `/staffing` mid-shift. Don't punch in for a scheduled person
  AND an unscheduled person past the threshold. Confirm both appear
  in the popup with Snooze / Declare-Absent buttons.
- Punch in someone past the threshold. Confirm they appear in
  "Late arrivals — reason needed". Click a quick-pick, click Save,
  confirm they drop out next poll.
- Open that person's player card. Confirm the late row appears in
  the Attendance section with the reason populated. Edit the reason
  cell, blur, refresh — reason update persists.

## Files touched

- `src/zira_dashboard/db.py` — DDL: `ALTER TABLE manual_absences ADD reason`,
  `CREATE TABLE late_arrivals`.
- `src/zira_dashboard/late_report.py` — declare_absent gains optional
  `reason` arg; new `save_late_arrival(day, emp_id, name, reason)`,
  `late_arrivals_for_day(day) -> set[emp_id]`, expanded
  `late_people_for_day` returning the three groups (or split into
  three sibling functions; pick whichever is cleaner during impl).
- `src/zira_dashboard/routes/staffing.py` — `/api/late-report`
  response shape extended; `/api/late-report/declare-absent` accepts
  `reason`; new `/api/late-report/save-late-arrival`.
- `src/zira_dashboard/routes/people.py` — query attendance rows,
  pass to template.
- `src/zira_dashboard/routes/people.py` — new endpoint
  `POST /api/staffing/people/{name}/attendance/reason` for inline edit.
- `src/zira_dashboard/templates/_footer.html` — popup renders four
  sections; reason editor on Declare-Absent + needs-reason rows.
- `src/zira_dashboard/templates/player_card.html` — Attendance
  table + two new stat tiles; inline-edit JS for reason cells.
- `tests/test_late_report.py` — extend.
- `tests/test_player_card.py` — extend.
- `CHANGELOG.md` — entry for the deploy.

## Implementation notes

- The schema changes are additive (`ALTER TABLE … ADD COLUMN IF NOT
  EXISTS`, `CREATE TABLE IF NOT EXISTS`). Safe to deploy without
  migration coordination.
- `late_arrivals_for_day(day)` is a single SELECT — cheap to call
  per /api/late-report poll (every 60s).
- The existing `_LATE_REPORT_CACHE` (~30s TTL) covers the JSON
  response; the new fields fit in the same envelope, so no new
  cache to invalidate.
- For unscheduled detection: the route already builds
  `active_people` and `assigned_today`; just diff. No new roster
  or schedule load.
- For needs_reason detection: the route already builds
  `attendance_by_name` and `late_names_set`; cross-reference with
  a SELECT from `late_arrivals` for today.
- Reason quick-picks live in HTML — no settings table. If Dale wants
  more options later, edit the template.
- Inline edit on the player card uses `contenteditable` + blur-based
  save. No save button per cell. Empty edit = save empty (allowed).
