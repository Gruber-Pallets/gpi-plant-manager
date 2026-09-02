# People Actionable Status Controls Design

## Goal

Turn the People page totals and warning pills into useful controls instead of
static labels. A manager should be able to narrow the people list from the
totals, understand each warning without leaving the page, and take the next
safe action when Plant Manager can help resolve it.

The design keeps the existing compact two-band manager strip. Count controls
remain in the first band and source warnings remain in the wrapping band below
it. This change adds interaction and clearer information without returning to
a large dashboard header.

## Approved direction

Use two related interaction patterns:

- Count pills become toggle filters for the people rows.
- Warning pills become buttons that open a small anchored action panel next to
  the selected warning.

The anchored panel is the primary warning experience because it keeps the
manager's place, provides more room than a tooltip, and avoids the weight of a
full drawer for one issue. Hover and focus may show a short explanation, but
all information and actions must also be available by clicking, tapping, or
using the keyboard.

Safe actions may run from the panel. Configuration and identity changes must
open a dedicated Settings workflow where the manager can review context and
make an explicit, reversible choice.

## Count filters

The three day-total pills retain their current numbers and labels, but use real
button semantics and visible selected states.

- **Working now** and **Worked earlier** are mutually exclusive status filters.
  Selecting one clears the other.
- **Need attention** is an independent filter and can be combined with either
  status filter.
- Selecting an active filter clears it.
- With no filters selected, all people for the chosen day are shown.
- A zero-count control stays visible to preserve the day summary, but is
  disabled. Its accessible description explains that there are no matching
  people to show.

The separate existing Needs attention checkbox is removed so the page has one
clear control for that state.

The URL is the source of truth for filters. It keeps the selected `day`, uses
one optional status value such as `status=working` or `status=earlier`, and
continues to use the attention flag for the independent attention filter. This
makes a filtered view bookmarkable and shareable, and lets polling request the
same view without rebuilding state only in JavaScript.

Counts always describe the complete selected day, not the already filtered
rows. When a filter is active, a short result summary removes ambiguity, for
example, “Showing 6 of 34 working now.” A valid filter with no matching rows
shows a specific filtered-empty message and a clear-filters action rather than
the page's general no-data state.

## Structured warning model

Warnings must no longer be passed to the template as display strings that the
browser would need to interpret. The People view receives structured warning
records with, at minimum:

- a stable opaque key;
- a warning kind;
- a short pill label;
- a manager-readable title and summary;
- the affected source, work center, or identity group;
- a reason code and safe explanation;
- the operational impact;
- the latest check time and, when known, the last successful update time; and
- a capability-based list of allowed actions.

The warning key identifies a recomputable issue for the chosen day; it must not
embed raw source payloads or sensitive identifiers in the URL. Warning labels
are presentation text only. Templates and JavaScript never parse those labels
to determine warning types or actions.

Reason codes normalize conditions the current data layer already knows, such
as missing totals, duplicate or incomplete readings, a metric mismatch, a
calculation failure, unmatched forklift identities, a stale source, a source
outage, or an identity conflict. Unexpected exceptions produce a safe generic
explanation and are logged through the existing server diagnostics; raw
exceptions, secrets, and source payloads are never exposed in the panel.

## Warning buttons and anchored panel

Each warning pill is a real button with a warning icon, the concise label, an
accessible expanded state, and a visible hover/focus treatment. Opening a
warning does not navigate immediately.

The anchored panel contains:

1. what happened;
2. what is affected or hidden on the People page;
3. the affected work center, source, or external identity group;
4. when Plant Manager last checked and, if known, last succeeded;
5. concise diagnostic facts appropriate for a manager; and
6. only the actions allowed for that warning type.

Only one warning panel is open at a time. Clicking the active warning, pressing
Escape, or clicking outside closes it and returns focus appropriately. The
panel remains within the viewport, flips above its trigger when needed, and
uses the same content on mouse, keyboard, and touch devices. Enter and Space
open the selected warning. Hover content is supplemental and never the sole way
to obtain information.

The panel has a compact loading state while details are fetched. A failed
detail request leaves the dashboard usable, explains that details could not be
loaded, and offers Retry. Failure never dismisses the warning or represents it
as resolved.

## Warning-specific content and actions

### Production metric unavailable

The panel identifies the work center, names the production values currently
hidden or unavailable, and explains the known reason when one can be determined
safely. Supported reasons include missing totals, incomplete or duplicate data,
a metric mismatch, and a calculation failure.

Allowed actions are:

- **Check again**, which refreshes the warning and dashboard data;
- **Open work center dashboard**, for source context; and
- **Review settings**, only when the diagnostic reason indicates a
  configuration or work-center mapping problem.

The panel does not offer a generic Settings link when settings cannot repair
the issue.

### Unmatched forklift calls

The pill keeps the total unmatched-call count. Its panel adds the number of
distinct external driver identities, the available display names or source
IDs, the first and last affected call times, and the impact on People page
forklift attribution. It summarizes identities rather than rendering every raw
call.

The primary action is **Review identities**. It opens a focused Settings view
that lists the unresolved external identities and the evidence available for
each one. Managers can map an identity to an active employee, review existing
mappings, or remove a wrong mapping. The warning panel itself never makes an
identity assignment.

### Stale, unavailable, or conflicting sources

The panel names the source, its last successful update when known, which People
sections or values are frozen or hidden, and the safe category of failure. It
offers **Check again** and a relevant diagnostics destination. A conflict may
also link to its dedicated resolution view when one exists.

## Safe refresh behavior

**Check again** is a refresh, not a dismiss or force-clear control. It requests
fresh source and People data, prevents duplicate requests while one is running,
and reports one of three honest results:

- the issue cleared;
- the issue remains, with refreshed details; or
- the check could not finish, with a retry option.

If the issue clears, the warning is removed from the strip after the rows are
refreshed. The open panel briefly explains that the issue has cleared before it
can be dismissed. A resolved or stale warning key requested directly receives
the same harmless cleared state rather than an error or reconstructed old
data.

## Forklift identity resolution

External forklift identities require a dedicated persistent mapping store and
a focused Settings view. Each current mapping records the external driver
identity, the chosen active Odoo employee, the authenticated actor, and created
or changed timestamps. Create, change, and remove actions append an audit entry
so a previous choice can be understood and reversed.

The manager must choose an active employee explicitly. Similar names may be
shown as evidence but are never saved automatically when certainty is missing.
The server validates both sides of the mapping, rejects conflicting active
mappings, and re-runs forklift identity resolution after a successful save.
The Settings view makes current mappings and unresolved identities visible in
one place.

This mapping workflow is the only material expansion beyond the current People
strip. It is included because “Unmatched forklift calls” otherwise has no
action a manager can take. It does not redesign other Settings sections or
change forklift scoring.

## Server and browser flow

The server-rendered rows endpoint remains responsible for counts, filters,
warnings, and people rows. It accepts the selected date, optional mutually
exclusive status filter, and independent attention flag. The dashboard context
filters rows but calculates the three displayed totals from the complete day.

Warning buttons carry only their stable key and summary attributes. An
authenticated warning-detail endpoint accepts the key and selected day,
re-evaluates that warning against current data, and returns a server-rendered
panel partial. Returning HTML keeps warning wording, authorization, and action
capabilities on the server and follows the dashboard's existing Jinja pattern.

A warning-panel controller handles trigger state, positioning, loading,
closing, focus, and safe actions. It may reuse the proven interaction rules of
the existing interval popover, but remains a separate component because it
loads data and exposes actions.

Live polling preserves URL filters and the selected date. If the open warning
still exists after a row refresh, the controller restores its trigger and
panel. If it no longer exists, the controller displays the cleared state.
Polling must not unexpectedly close a panel, reset filters, move keyboard
focus, or duplicate a pending manual check.

## Accessibility and responsive behavior

- Count and warning controls expose button, pressed or expanded, disabled, and
  controlled-panel states to assistive technology.
- Selected filters and warnings are identifiable by text or icon as well as
  color.
- Focus rings remain visible against the existing manager strip.
- Status changes from refreshes and filter results use a restrained live
  region without repeatedly announcing normal polling.
- The anchored panel has a labelled heading and a predictable focus order, but
  is not a modal dialog.
- Touch targets remain usable and the panel stays inside the viewport at all
  supported widths.
- The existing two-band strip continues to wrap without overlap or horizontal
  page scrolling.

## Error and safety rules

- Details and actions are authorized on the server; hiding an action in the
  browser is not an authorization boundary.
- A warning cannot be dismissed permanently merely to make the page look
  healthy.
- Refresh failures preserve the last truthful dashboard state and its warning.
- Identity changes require an explicit choice, server validation, and an audit
  record; uncertain name matches are not guessed.
- Warning details expose aggregates and safe explanations, not raw operational
  payloads or exception text.
- Concurrent mapping changes return a clear conflict and reload the current
  mapping rather than silently overwriting it.

## Verification

Automated coverage will verify:

- working and earlier are mutually exclusive, attention combines with either,
  and selecting an active filter clears it;
- filter query parameters preserve the selected day, survive polling, and can
  be loaded directly;
- zero-count controls are disabled and filtered-empty states are clear;
- totals stay based on the complete day and the result summary reports the
  visible subset accurately;
- raw warning strings are replaced with structured records and capability-based
  actions;
- production, unmatched-forklift, stale-source, unavailable-source, conflict,
  unknown-error, and resolved-warning panels render safe accurate content;
- detail and manual-refresh loading, success, still-failing, failure, retry,
  and duplicate-request states behave correctly;
- mapping creates, changes, removals, inactive employees, conflicts, concurrent
  writes, audit entries, and resolution refreshes are safe;
- polling preserves active filters, keyboard focus, and an open panel;
- mouse, touch, Enter, Space, Escape, outside-click, and focus behavior work;
- panel placement stays in the viewport and the manager strip does not overlap
  or overflow at the existing five tested viewport widths; and
- the existing People calculations, section ordering, and scoring tests remain
  unchanged and pass.

Manual review should use a busy day containing multiple production warnings
and many unmatched forklift calls. A manager must be able to filter the people
list, explain why each warning matters, check it again, and reach the correct
resolution screen without losing the selected day or scroll position.

## Scope boundaries

This design changes the People manager-strip controls, warning detail flow,
and the focused forklift identity-resolution workflow. It does not change
attention rules, attendance truth, production calculations, forklift scoring,
person-row content, timeline behavior, section ordering, or unrelated Settings
pages.

## Rejected alternatives

### Tooltips only

Tooltips are poor on touch devices, cannot safely hold actions, and make
keyboard discovery harder. Hover remains a shortcut, not the product.

### Navigate immediately when a warning is clicked

Immediate navigation removes context before the manager understands the issue
and sends every warning to a destination even when a safe refresh is the best
first action.

### A full right-side issue drawer

A drawer has room for more diagnostics but is too heavy for the short,
single-warning task and obscures more of the People dashboard.

### Expand warning rows inline

Inline expansion pushes the person list down, makes a multi-warning strip jump
in height, and complicates the compact sticky layout.

### Edit identity mappings inside the warning panel

An anchored panel is too small for identity evidence, conflict handling, audit
context, and a deliberate employee choice. Keeping edits in Settings makes the
high-impact action reviewable and reversible.
