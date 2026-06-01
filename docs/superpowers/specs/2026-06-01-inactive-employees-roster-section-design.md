# Inactive Employees in the Settings Roster Filter — Design

- **Date:** 2026-06-01
- **Status:** Approved (pending spec review)
- **Author:** Dale + Claude

## Problem

The Settings → Roster Filter page is a single flat list of every Odoo-synced
person with a manual show/hide checkbox per row (the `excluded` flag). It does
not distinguish current employees from people who have left the company. We want
former employees to be clearly separated, sourced automatically from Odoo, kept
out of every live picker, but still reachable for their historical stats and
trophies.

## Key insight: most of this already works

A trace of every employee picker in the app shows the desired behavior is
already implemented at the data layer. The `people` table already carries an
`active` flag synced from Odoo, and the codebase already keys live views off it.

**Live pickers already filter to `active = true`:**

- Scheduler and its assignment dropdowns — `active_people = [p for p in roster if p.active]` (`routes/staffing.py:336`, `:378`).
- Department & recycling assignment pickers — `if p.active` (`routes/departments.py:567`, `:852`).
- Timeclock kiosk clock-in list — `WHERE active = TRUE AND NOT excluded` (`routes/timeclock.py:331`).
- Footer attendance picker — sourced from `roster if p.active` (`routes/staffing.py:970`).
- Skills / People Matrix — renders all rows but **defaults to an active-only filter** with an opt-in "show inactive" toggle (`templates/skills.html:315`, `:431`).

**Historical views already show former employees (desired):**

- Trophy case and leaderboards read production history, not the roster, so a
  former employee keeps every record and trophy (`routes/trophies.py`,
  `leaderboard.py`, `awards.py`).
- Past Schedules person filter is built from names in historical schedules
  (`routes/past_schedules.py:80`) — former employees appear, which we are
  keeping (see Decisions).
- The employee card renders for anyone by URL — `/staffing/people/{name}`
  (`routes/people.py:39`) is not gated on `active`.

**Odoo sync already flips people to inactive:** when an employee is archived in
Odoo they drop out of the `active=True` fetch and `odoo_sync` sets
`active = FALSE` on the next run (`odoo_sync.py:134`).

So the feature reduces to **two changes on the Settings → Roster Filter page**.

## Decisions

1. **Inactive is sourced purely from Odoo** (`active = false`), read-only. No
   manual "mark inactive" toggle, no new storage. Manual hiding of an *active*
   employee stays available via the existing `excluded` checkbox.
2. **The manual hide checkbox stays** on active employees. The Inactive section
   is read-only (no checkbox — inactive people are already hidden everywhere
   live).
3. **Past Schedules keeps former employees** in its person filter. It is a
   historical lookup, like the trophy case. Inactive are hidden only from
   live/scheduling pickers (which already happens). "Nowhere else" is read as
   "no live working tool," not "no historical view."

### Rejected alternative

A separate "former employees" archive table with manual archiving. Rejected:
redundant with Odoo's `active` flag, more moving parts, and contrary to
Decision 1.

## Principle

*Inactive* = Odoo's `active = false`. We are not adding a new data concept — we
are surfacing the one that already exists. No schema change, no sync change.

## Change 1 — Split the Roster Filter into Active / Inactive sections

**Route** (`routes/settings.py:64`): add `active` to the existing query and
split the rows by it.

```sql
SELECT odoo_id, name, excluded, active
FROM people
WHERE odoo_id IS NOT NULL
ORDER BY lower(name)
```

Build two lists for the template, e.g. `active_rows = [r for r in rows if
r["active"]]` and `inactive_rows = [r for r in rows if not r["active"]]`. (Either
this or an equivalent Jinja split is acceptable; route-side keeps the template
simpler.)

**Template** (`templates/settings.html:246-274`): replace the single `<ul>` with
two labeled subsections under the existing panel:

- **Active (N)** — the existing grid `<ul>`, each row keeping its
  `.roster-filter-toggle` checkbox and `data-odoo-id`. Behavior unchanged.
- **Inactive (M)** — a read-only grid `<ul>` with **no checkbox**. Each row shows
  the name (as a link, see Change 2) and the `Odoo #` meta, visually
  de-emphasized to match the "archived" status.

Render the Inactive subsection only when `inactive_rows` is non-empty; keep the
existing "No Odoo-synced people yet" empty state for when there are no rows at
all. Update the panel's intro `<p class="note">` to describe both sections:
active rows can be unchecked to hide from live views; inactive rows are archived
in Odoo, already hidden from scheduling and pickers, with history preserved.

**JS** (`templates/settings.html:1077`): unchanged. It binds to
`.roster-filter-toggle` inside `.roster-filter-row`; inactive rows have no such
checkbox, so they are naturally skipped.

**POST handler** (`/api/settings/roster-filter/toggle`): unchanged.

## Change 2 — Make names link to the employee card

In both sections, the name links to the existing player card:

```html
<a href="/staffing/people/{{ p.name | urlencode }}" class="roster-filter-name-link">{{ p.name }}</a>
```

**Important layout detail:** today each Active row is a single `<label>`
wrapping both the checkbox and the name, so clicking anywhere on the row toggles
the checkbox. A link inside that label would both navigate *and* toggle. The
Active row must be restructured so the `<label>` wraps only the checkbox (and
optionally a small spacer), with the name link as a sibling element — so
clicking the name navigates and clicking the checkbox toggles, with no overlap.
Inactive rows have no checkbox, so the whole row can simply contain the link.

Names may contain spaces; URL-encode with Jinja's `| urlencode`. The card route
is keyed by name and handles arbitrary names already.

## Data flow

Odoo archives an employee → `odoo_sync` sets `active = false` (`odoo_sync.py:134`)
→ on the next page load the person (a) no longer appears in any live picker
(already true via the `active` filters listed above) and (b) renders in the
Settings "Inactive" section. Their production history is untouched, so trophy
case, leaderboards, and their employee card retain every record.

## Edge cases

- **`excluded` but still `active`** → stays in the Active section with an
  unchecked box. Unchanged behavior.
- **Both inactive and excluded** → shows once, in the Inactive section
  (read-only). The `excluded` state has no additional effect on an inactive
  person, who is already hidden from live views.
- **Inactive person with no production history** → card still loads, just
  sparse. Acceptable.
- **Re-hire (Odoo re-activates)** → next sync flips `active = true`; the person
  reappears in the Active section and in live pickers automatically.
- **No inactive people** → Inactive subsection is omitted; page looks like
  today minus the new heading.

## Out of scope

- **Admin → Devices** lists all synced people in a table (`routes/admin.py:64`),
  but it is an admin/diagnostic view, not a menu picker — left unchanged.
- **No changes** to the scheduler, skills matrix, timeclock, leaderboards,
  trophy case, or Odoo sync — they already behave correctly.

## Testing

- **Route test:** seed active and inactive Odoo-synced people; assert the
  settings `roster_filter` context splits them into the correct active vs
  inactive lists, and that `active` is included in the row data.
- **Template/integration test:** an inactive person renders in the Inactive
  section with no checkbox and a working `/staffing/people/{name}` link; an
  active person keeps its checkbox.
- **Regression test (guards the contract):** an inactive person does **not**
  appear in the scheduler assignment dropdown and **does** appear in the trophy
  case / leaderboards. This locks in "feed from Odoo / nowhere else / history
  visible."

### Local verification note

Local Python is 3.9 and the full suite cannot run locally; production is Railway
auto-deploy on push to `main`. Verify changed modules with `py_compile` and a
targeted `ast`-based exec of the route/template-context logic before pushing, in
addition to writing the tests above for CI.
