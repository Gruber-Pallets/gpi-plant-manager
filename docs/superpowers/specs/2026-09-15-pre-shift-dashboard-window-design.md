# Pre-shift Dashboard Window Fix

## Purpose

Keep live department dashboards available between plant midnight and the start
of the day's shift. During that period, the dashboard must show zero elapsed
work instead of failing with an internal server error.

## Root Cause

For a live "today" dashboard, the production window starts at the configured
shift start and ends at the earlier of the current time or shift end. Before
shift start, that creates an end earlier than the start. The canonical
attendance projection rejects this non-positive window, and the uncaught error
becomes FastAPI's plain `Internal Server Error` page.

The Recycling page reloads every 60 seconds after a lightweight liveness probe.
The probe succeeds because the server is healthy, then the full page render
hits the invalid window and replaces the working dashboard with the error
response.

## Design

Clamp the live dashboard's effective window end to the shift start. Before the
shift, the resulting zero-duration window represents zero elapsed work.

The canonical attendance projection will treat a zero-duration window as an
empty projection instead of passing it to the timeline segment converter. This
keeps the converter's strict positive-duration contract intact for callers
that genuinely provide invalid bounds.

Do not change the refresh cadence or probe behavior. The server must render the
page correctly at every valid plant time; masking the route failure in
JavaScript would leave direct visits broken and add unnecessary render traffic.

## Error Handling

- Before shift start: return no canonical work segments and render zero
  progress.
- At or after shift start: preserve the current canonical attendance behavior.
- Reversed windows outside this controlled pre-shift case remain invalid at
  the pure timeline-conversion boundary.

## Verification

1. Add a regression test that exercises the department dashboard before shift
   start with canonical attendance enabled.
2. Confirm the test fails with the current positive-duration exception.
3. Implement the minimal clamp and empty-projection handling.
4. Run focused department, current-operator, attendance-location, and dashboard
   tests.
5. Run the full test suite and lint checks before pushing.

## Acceptance Criteria

- `/recycling` and `/tv/recycling` render successfully before shift start.
- Pre-shift elapsed production and work segments are zero/empty.
- Shift-time and historical dashboard calculations are unchanged.
- The 60-second dashboard refresh can no longer replace the page with this
  pre-shift `Internal Server Error`.
