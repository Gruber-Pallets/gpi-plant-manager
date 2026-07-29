# Live Schedule Validation Design

## Goal

Show Staffing warnings only when they apply to the schedule currently visible
in the grid. A warning appears as soon as a current selection violates a
staffing rule and disappears as soon as the visible selections satisfy it.

## Problem

The Staffing page currently renders some warnings from an Auto-scheduler
recommendation. That recommendation can differ from the assignments displayed
in the grid. After a manager manually changes the grid, the browser only
clears a small subset of warnings, so a resolved safe-crew warning can remain
visible.

## Scope

This change covers every Staffing warning whose status depends on the selected
schedule: safe complete crews, minimum coverage, duplicate assignments,
capacity, qualifications, full-day time off, active training protections, and
default conflicts. It does not save or rebuild the schedule as a side effect
of validation.

The server remains authoritative for all staffing rules. The browser never
decides whether a person is qualified, available, or safe to schedule.

## Architecture

Create one shared server-side current-view validation path. It accepts a day,
the enabled Auto work centers, and the assignment map currently represented in
the browser. It loads the current roster, work-center settings, time off,
defaults, and active training state, then returns the complete warning payload
for that exact view.

The initial Staffing page render uses the same validation path for its
displayed schedule. It must not use an Auto-scheduler proposal to populate the
warning banner.

Expose the validator through a read-only JSON endpoint for subsequent edits.
The endpoint performs no schedule writes, does not change assignment sources,
and does not start or reconcile training blocks.

## Browser behavior

When a manager changes a Scheduled picker, clears a picker, changes enabled
Auto work centers, applies a reset, or receives a successful Auto rebuild, the
browser collects the assignments and enabled centers currently shown. It sends
that snapshot to the validation endpoint after a short debounce.

Only the response for the newest snapshot may update the warning panel. The
browser aborts or ignores older outstanding requests so a slow response cannot
replace newer validation results.

Each successful validation response replaces the whole warning list; it never
appends to or filters a prior list. This lets warnings both appear and clear as
the manager edits the grid. Auto rebuild responses can still update the grid,
but the following validation response is the authoritative source for its
warning panel.

If validation cannot complete, the browser removes stale schedule warnings and
shows a distinct, temporary validation-unavailable message. It must not imply
that an older staffing warning still applies to the current selections.

The posted view remains display-only and uses the server-rendered validation
result for the posted assignments.

## Validation rules

The shared validator reports the same user-facing warning types used by the
Scheduler, evaluated against the supplied assignments:

- a person selected at more than one work center;
- a work center above configured capacity;
- work centers below their safe configured minimum;
- people without the skills required by their selected center;
- people selected while inactive, in Reserves, or on full-day time off;
- unsafe coupled crews, including Trim Saw's required two-person safe pair and
  training partners;
- unresolved exact and group default requirements; and
- any other current-view rule already surfaced by Staffing.

The validation response contains structured issues, with the existing message
and reason/rejection details where available. The browser continues to render
the existing accessible warning panel from that structured response.

## Error handling and safety

Requests reject malformed days, unknown work centers, invalid assignment map
shapes, and duplicate names in a single center. The server limits validation
to known work centers and names from the active scheduling context. It treats
all client data as a proposed view only; it never persists it.

Because all rules are evaluated server-side from fresh staffing data, a client
cannot clear a warning by changing only presentation state. Conversely, a
stale Auto recommendation cannot leave a warning after the current view is
safe.

## Regression coverage

Tests will prove that:

- the initial page's warning data comes from its displayed assignments rather
  than a hypothetical Auto recommendation;
- an unsafe Trim Saw pair produces a warning and replacing it with a safe pair
  removes that warning;
- current-view validation returns coverage, duplicate, capacity,
  qualification, time-off, training, and default issues when applicable;
- the endpoint has no schedule-write side effects;
- a later validation response wins over an older one in the browser; and
- validation failures remove stale staffing warnings and clearly report that
  current validation is unavailable.

## Review

The design is intentionally limited to warning truthfulness. It does not
change Auto scheduling decisions, manually move anyone, or alter the rules
that determine a safe crew.
