# Timeclock Settings Reorganization — Design

**Date:** 2026-06-02
**Status:** Design — approved in brainstorming; not yet implemented.

## Context

The **Timeclock** panel of the Settings page
(`src/zira_dashboard/templates/settings.html`, the `#timeclock-panel`
`<section>`) has accreted into one long, disorganized scroll. It currently holds,
top to bottom: the intro + two "Open Timeclock" buttons, **Company Schedule**,
**Saturday Default**, **Rounding (default)**, **Auto-Lunch**, **Per-schedule
rounding**, **Sync status**, **Recent punches**, and **Schedule variances** —
editing controls and read-only monitoring interleaved, with three different
heading treatments and four different helper-text styles.

Two concrete problems drove this work:

1. **Inconsistent saving.** The one panel runs *two* save UXs side by side:
   - **Autosave + top-center toast + header Undo/Redo** — Company Schedule and
     Saturday Default, wired via `attachAutosaver(...)` (settings.html:981-982)
     to `/settings/schedule` and `/settings/saturday_schedule`. These persist
     fine; they simply have **no visible Save button** (autosave on a 600ms
     debounce). Confirmed during brainstorming: nothing is broken, the control is
     just invisible.
   - **Native full-page reload + green "Saved." flash + explicit button** —
     Rounding, Auto-Lunch, and Per-schedule rounding (plus Add/Remove).

   (A third AJAX-flash-beside-button variant exists on the *Time Off* panel —
   out of scope here, but evidence the page has drifted.)

2. **Weak visual hierarchy.** Dense, small helper text in several sizes;
   inconsistent heading margins (a mix of CSS and inline `margin-top:1.6rem`);
   and the default-rounding IN/OUT columns don't visually read as a single
   paired control.

The two save endpoints touched here (`/settings/rounding`, `/settings/auto_lunch`,
`/settings/work_schedule_rounding`) **already return JSON** when called with
`Accept: application/json` (settings.py:490, 537, 568), so converting them to
autosave needs **no server change**.

## Goals

1. Reorganize the Timeclock panel into **three sub-tabs**: **Schedules**,
   **Rules**, **Activity** — one area visible at a time, editing separated from
   monitoring.
2. Make saving **consistent**: every editable field autosaves the same way
   (debounce → toast → Undo/Redo). Remove the three divergent explicit Save
   buttons.
3. Within **Rules**, place **Default rounding** and **Per-schedule rounding**
   adjacent under one heading (they share the IN/OUT window UI), with Auto-Lunch
   as its own block below.
4. Tighten visual hierarchy: one helper-text style, consistent heading spacing,
   and an IN/OUT layout where the two columns clearly read as a pair.

## Non-goals

- **No behavioral change to how punches sync to Odoo `hr.attendance`.** The
  entire punch → log → sync path is untouched.
- **No change to any `name=` field attribute, POST endpoint, or server save
  logic.** This is template + CSS + JS-wiring only. (Lone exception: the
  per-schedule `/add` and `/remove` redirects gain a `#rules` fragment so the
  user lands back on the Rules tab — a redirect-target string, not logic.)
- **No change to the top-level Settings sidebar** (Work Centers, Roster Filter,
  Integrations, TVs, Timeclock, Time Off). The three new tabs live *inside* the
  Timeclock panel.
- **No new save paradigm.** We standardize on the autosave + Undo/Redo pattern
  the page (and the People Matrix / Scheduler) already use — not a sticky save
  bar, not uniform buttons. (Decided in brainstorming.)
- **No condensing or paginating the Activity tables.** Same queries, columns,
  and 50-row limits; restyled only.
- **No Time Off panel changes.**

## Decisions (from brainstorming)

- **Save model: autosave everywhere.** Field edits autosave. Genuinely
  *structural* actions stay as explicit buttons: **+ Add break**, per-schedule
  **Add a schedule** / **Remove** (Remove keeps its `confirm()`).
- **Layout: sub-tabs**, default landing tab **Schedules**.
- **Rounding layout: stacked** — Default *above* Per-schedule, unified, not
  side-by-side (the IN/OUT grid is already two columns; side-by-side gets cramped
  on a 13" laptop).

## Design

### Panel shell

```
┌ Timeclock ────────────────────────────────────────────────┐
│ intro line ("Writes to Odoo hr.attendance … name-pick")     │
│ [ Open Timeclock ↗ ]  [ Open here ]        ← panel-level     │
│                                                             │
│ ┌ Schedules │ Rules │ Activity ┐   ← role=tablist           │
│ └───────────┴───────┴──────────┘                            │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │  (active tab panel)                                      │ │
│ └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

- The heading, intro, and the two **Open Timeclock** buttons stay at the panel
  top (panel-level, not tab-specific).
- Tabs are `role=tablist` **buttons** toggling an `.active` class on three tab
  panels — deliberately **not** `<details>`, to avoid the global "only one
  `<details>` open at a time" handler (settings.html:1145).
- Active tab is reflected in the URL hash (`?section=timeclock#rules`) and
  applied on load, so the per-schedule Add/Remove round-trip returns to Rules.
  Switching tabs otherwise does no reload (safe — everything autosaves).

### Tab 1 — Schedules

**Company Schedule** + **Saturday Default**, unchanged in function (shift
bookends, work days, indexed break rows, `+ Add break`). They already autosave;
this tab only gives them matching card framing and the unified header/help
spacing. Their `attachAutosaver` wiring is preserved as-is.

### Tab 2 — Rules

- **Rounding** (one heading, two adjacent parts):
  - *Default rounding* — the IN | OUT window pair, posting to `/settings/rounding`.
  - *Per-schedule rounding* — directly beneath, each override reusing the
    **identical** IN/OUT card markup so they read as variations of one control.
    The four window fields per card autosave to `/settings/work_schedule_rounding`
    (one autosaver attached per card form, keyed by its hidden
    `resource_calendar_id`). **Add a schedule** (`/add`) and **Remove**
    (`/remove`) remain explicit buttons; their redirects gain `#rules`.
- **Auto-Lunch** — Mode radios + the flexible-schedule rule, posting to
  `/settings/auto_lunch`. Moved below the rounding block (today it sits *between*
  default and per-schedule rounding, splitting them).
- **IN/OUT pairing:** each rounding control wrapped in a bordered `.rounding-card`;
  inside, the two columns get equal width, aligned rows, and a divider between IN
  and OUT so the pair is visually unmistakable. Default and per-schedule share
  this card so they look like one family of control.

### Tab 3 — Activity (read-only)

- **Sync status (last 7 days)** as a compact summary, then **Recent punches** and
  **Schedule variances** tables. Same DB queries (settings.py:113-138), same
  columns, same `LIMIT 50`. Restyled with the page's standard `table`/`th`/`td`
  CSS instead of the current inline `style=` attributes. No interactivity added.

### Save behavior (the consistency fix)

- Attach `attachAutosaver` to the Rounding form, the Auto-Lunch form, and each
  per-schedule window-edit form. (Endpoints already speak JSON — no server
  change.)
- **Remove** the three explicit Save buttons and their `.rounding-actions` /
  `saved-flash` markup, plus the server-rendered `{% if saved %}Saved.{% endif %}`
  spans inside those forms.
- Result: every field saves identically — 600ms debounce → top-center "Saved"
  toast → page-header Undo/Redo (each form keeps its own history; the header
  buttons dispatch to the most-recently-saved form, as today).
- The existing `form[data-section]` submit interceptor (settings.html:744) is
  irrelevant to these forms (they have no submit button after the change) and is
  left alone.

### Visual hierarchy

- **One helper-text class** applied under every heading, replacing today's mix
  (`.note` 0.72rem, `.rounding-blurb` 0.85rem, `.effective-note`, inline
  `<p style="color:var(--muted)">`). Single size, line-height, and bottom margin.
- **Consistent heading spacing** via classes — drop the repeated inline
  `style="margin-top:1.6rem"` on the `<h3>`s for even vertical rhythm.
- Heading levels rationalized within the panel: `h2` Timeclock → tab strip →
  per-section headers (one class) → sub-block labels (one class).

## Files touched

- `src/zira_dashboard/templates/settings.html` — restructure the
  `#timeclock-panel` `<section>` into the tab strip + three tab panels; reorder
  Auto-Lunch below per-schedule rounding; wrap rounding controls in
  `.rounding-card`; wire the three autosavers; delete the three Save buttons and
  their flash markup; apply the unified help/heading classes.
- `src/zira_dashboard/static/settings.css` — `.timeclock-tabs` strip + active
  state; `.rounding-card` and the paired IN/OUT treatment; one helper-text class;
  consistent heading-spacing classes.
- `src/zira_dashboard/routes/settings.py` — append `#rules` to the redirect
  targets of `/settings/work_schedule_rounding/add` and `/remove` (lines 588,
  601). No other change.

## Verification

Manual, via the preview workflow (this is a layout refactor with no pure-logic
unit surface):

- **Tabs:** all three render; clicking switches the visible panel; default is
  Schedules; reload with `#rules` lands on Rules; per-schedule Add/Remove returns
  to Rules.
- **Autosave parity:** edit a default-rounding field, an auto-lunch field, and a
  per-schedule window field — each pops the "Saved" toast within ~1s, persists on
  reload, and arms header Undo; Undo reverts and re-saves. No Save buttons remain
  on those sections.
- **Structural buttons still work:** + Add break, per-schedule Add / Remove
  (with confirm) behave as before.
- **No regressions to untouched sections:** Company Schedule / Saturday Default
  still autosave; Work Centers, Roster Filter, TVs, Time Off unchanged.
- **Field/endpoint integrity:** spot-check that every `name=` and `action=` in
  the timeclock panel is byte-for-byte what it was (grep diff), proving the punch
  → Odoo path is untouched.
- **Visual:** screenshots of each tab at desktop and ~1280px confirming
  consistent spacing and the IN/OUT pair reading as one control.

## Done criteria

- ☐ Timeclock panel renders as Schedules / Rules / Activity sub-tabs; Schedules
  is the default; tab state survives the per-schedule round-trip via `#rules`.
- ☐ Rounding (default) and Per-schedule rounding are adjacent under one heading;
  Auto-Lunch is a separate block below; IN/OUT columns read as a pair.
- ☐ Rounding, Auto-Lunch, and per-schedule window fields autosave; their explicit
  Save buttons and flash markup are gone.
- ☐ Activity tab holds Sync status + Recent punches + Schedule variances,
  unchanged in data, restyled consistently.
- ☐ One helper-text style and consistent heading spacing across the panel.
- ☐ All `name=` attributes, POST endpoints, and server save logic byte-identical
  except the two `#rules` redirect targets; punch → Odoo sync untouched.
- ☐ Verified in the browser preview per the checklist above.

## Open questions

None blocking. One minor judgment call deferred to implementation: whether the
`.rounding-card` divider between IN and OUT is a vertical rule or just the
existing column gap — decided visually against a screenshot.
