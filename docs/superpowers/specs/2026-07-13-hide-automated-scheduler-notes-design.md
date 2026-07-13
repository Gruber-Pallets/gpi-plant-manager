# Hide Automated Scheduler Notes Design

## Purpose

Remove every per-person explanatory badge produced by the automated scheduler
from the Staffing page. Human-entered day notes and work-center notes remain
unchanged, and actionable schedule-level warnings and errors remain visible.

This decision supersedes only the visible per-assignment explanation portions
of the approved global minimum-coverage scheduler design and plan. The solver
may continue producing structured reason codes and display text internally for
diagnostics, API compatibility, and testing, but the Staffing page must not
render them beside employee names.

## Current Behavior

The route exposes automated assignment explanations as `rotation_reasons`.
The Staffing template renders each explanation as a `.rotation-reason` badge,
and `staffing.js` recreates the same badge when it rebuilds an assignment
summary in the browser. The forthcoming global solver also specifies visible
texts such as “Assigned to meet minimum coverage.”

These badges are automated annotations rather than supervisor-authored notes,
but their placement makes the schedule crowded and makes them read like notes.

## Design

Remove the server-rendered `.rotation-reason` badge from assigned-person rows.
Remove the JavaScript badge-injection path used when an assignment summary is
updated without a page reload. Do not replace either badge with a tooltip,
icon, hidden accessibility text, or alternate per-person annotation.

Keep structured assignment reasons in the scheduling engine and route payloads.
Removing that data is outside this change, which affects presentation rather
than solver behavior or diagnostic capability.

Keep schedule-level warning and error UI unchanged. Those messages tell the
planner about unresolved coverage, invalid configuration, training needs, or a
failed rebuild and remain actionable. Human-entered schedule notes and
work-center notes continue to save and render exactly as they do now.

Update the global minimum-coverage scheduler design and implementation plan so
future solver work does not restore visible per-person badges. Those documents
may still require structured reason codes in solver results and tests.

## Data Flow

1. The scheduler may calculate and return assignment reasons.
2. Route code may retain those reasons in internal context or JSON responses.
3. The Staffing template and live summary JavaScript ignore per-assignment
   reasons.
4. Schedule-level warnings and errors continue through their existing render
   paths.

No schema, persistence, migration, or API version change is required.

## Testing

Regression tests will assert that:

- the Staffing template has no server-rendered automated reason badge;
- the Staffing JavaScript does not inject a reason badge during live summary
  updates;
- the global scheduler documents no longer instruct implementation to render
  per-person reasons; and
- existing schedule-level warnings and human-note controls remain present.

Focused tests and the relevant Staffing test suite will run after the change.

## Acceptance Criteria

- No automated scheduler explanation appears beside any assigned employee,
  including after an automatic rebuild or a client-side assignment update.
- Messages such as “Assigned to meet minimum coverage.” are never rendered as
  per-person notes.
- Schedule-level warnings and errors remain visible.
- Human-entered day notes and work-center notes remain unchanged.
- Structured reason data can remain available internally without appearing in
  the Staffing UI.
