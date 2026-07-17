# Show Saturday Availability Only When It Is Partial

## Goal

Keep the Saturday Scheduler focused by showing an availability badge only when
a committed person cannot work the full configured Saturday shift.

## Behavior

The staffing view compares each committed person's availability start and end
times with the Saturday recruitment shift start and end times.

- If both bounds match the recruitment shift, the person is fully available and
  no availability badge is rendered.
- If either bound differs, the person has partial availability and receives a
  yellow warning badge containing their available time range.

This applies wherever Saturday committed people are displayed in the staffing
view, including Unassigned and assigned work-center rows. The existing
commitment, scheduling, and publishing behavior is unchanged.

## Data flow

`build_staffing_bays` receives the Saturday commitment windows and the active
Saturday recruitment shift. It produces availability display data only for
partial commitments. The template and client-side roster rendering continue to
render a badge only when display data is present; the badge style changes from
the current informational blue treatment to the warning yellow treatment.

## Testing

Cover a full-shift commitment and a partial commitment in the staffing view:

- Full-shift availability is omitted from the display map.
- A shorter availability interval remains present with its formatted range.
- The staffing template and dynamically rendered roster retain the conditional
  badge behavior, with the warning styling applied to partial availability.

## Acceptance criteria

- A person available for all Saturday shift hours has no hours bubble.
- A person available for fewer than the configured Saturday shift hours has a
  yellow bubble showing their available hours.
- The same rule works for both unassigned and assigned people.
