# Current Staffing Minimum Warning Design

## Goal

Make the staffing warning banner describe the schedule currently displayed on
the page. An enabled work center that has its minimum number of present,
qualified operators must not be labeled below minimum merely because a
hypothetical Auto rebuild would choose a different arrangement.

## Current problem

The staffing page runs the Auto scheduler while rendering. Before that run, it
removes assignments from enabled work centers so the solver can construct a
fresh proposal. The page then displays `center_minimum_unmet` placement issues
from that proposal beside the unchanged saved schedule. This mixes two
different states: the grid shows the saved schedule, while the warning banner
describes an unrequested Auto proposal.

This is why Loading/Jockeying can visibly contain one person and Tablets can
visibly contain five people while both are reported below their configured
minimums.

## Approaches considered

### 1. Validate the displayed schedule for page-load warnings — selected

Calculate minimum coverage from the saved assignments shown in the grid. Keep
Auto proposal diagnostics in the responses to explicit Schedule Goal and
work-center On/Off actions. This makes every message describe the state the
user is currently interacting with.

### 2. Protect every displayed assignment during the page's Auto preview

Treat all current assignments as solver locks. This would usually remove the
contradiction, but it would also stop the preview from representing what an
Auto rebuild would actually do. It leaves page rendering coupled to a
hypothetical schedule that the user did not request.

### 3. Hide all minimum warnings during page rendering

This is the smallest change, but it would also hide genuine current shortages,
such as a scheduled operator being absent or unqualified. It removes useful
information along with the misleading information.

## Selected behavior

For each enabled work center, the page will count an assignment toward the
minimum only when the assigned person:

- exists in the active roster;
- is active and is not a reserve;
- is not on full-day time off; and
- has level 1 or higher in every effective required skill for that center.

If the safe current count is at least the configured minimum, the page shows no
minimum warning for that center. Otherwise it shows a current-schedule warning
with the safe count and required minimum.

For the reported example, Loading/Jockeying at 1 of 1 and Tablets at 5 of 4
produce no banner warning when those operators are present and qualified.

## Data flow

1. The staffing route loads the roster, saved schedule, time off, enabled work
   centers, and effective work-center settings as it does today.
2. A pure helper evaluates the saved assignments for each enabled center and
   returns current minimum issues.
3. The page context uses those issues for minimum coverage instead of the Auto
   suggestion's placement issues.
4. The Auto suggestion may still supply assignment reasons and protected
   training/qualification diagnostics that describe real saved locks.
5. Explicit Auto API actions continue returning the full proposal placement
   issues, including centers an Auto rebuild cannot fill.
6. A manual picker change continues clearing obsolete Auto-result warnings;
   the existing picker-close understaffing check remains the immediate feedback
   for a newly reduced crew, and the authoritative qualified check runs on the
   next server render.

## Error handling

The current-schedule check follows the staffing page's existing safe fallback
rules for work-center settings. A missing roster person, inactive person,
reserve, full-day absence, or missing required skill does not count toward the
minimum. A transient settings read must not make the page unavailable.

## Testing

- Add a regression matching the reported layout: one qualified operator at a
  minimum-one center and five qualified operators at a minimum-four center
  yield no current minimum issues.
- Verify that absent, reserve, inactive, unknown, and unqualified assignments do
  not count.
- Verify that a genuinely short enabled center produces one current warning.
- Verify that page context excludes hypothetical Auto placement shortages when
  the displayed schedule is safely staffed.
- Verify that explicit Auto rebuild responses still report genuine proposal
  placement failures.
- Run the focused staffing rotation and suggestion suites, followed by the
  repository's normal test command if one is documented.

## Scope

This change does not alter saved assignments, assignment-source metadata,
minimum or maximum settings, skill data, Auto scheduling decisions, or publish
validation. It only corrects which state supplies minimum warnings on the
staffing page.
