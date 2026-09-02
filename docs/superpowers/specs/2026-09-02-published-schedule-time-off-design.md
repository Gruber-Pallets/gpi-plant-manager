# Published Schedule Time Off Design

## Goal

Make employee time off visible on both printed schedules and schedules posted
to Slack. Full-day absences must be easy to notice, while partial-day absences
must stay next to the affected employee so the timing remains clear.

## Current Behavior

The staffing page already receives normalized time-off entries and shows
partial-day timing beside assigned employees. Browser printing and Slack PDF
publishing use the same print stylesheet. That stylesheet currently hides the
entire Time Off rail, including all full-day names, and also hides the Saturday
Off rail.

## Design

Add a compact print-only `Off` summary immediately below the printed schedule
header and before the schedule table. The summary contains each employee who is
off for the full day, rendered once as a simple comma-separated list. It includes
both full-day time-off entries and employees explicitly placed in Saturday Off.
Duplicate names are removed and names use a stable, case-insensitive sort.

Partial-day entries do not appear in the `Off` summary. Their existing timing
label remains inline beside the employee wherever the employee is assigned in
the schedule, such as `arrives 11:30am`, `leaves 2:00pm`, or a bounded time
range. Print styling will make that inline label legible and non-interactive.

The interactive browser scheduler remains unchanged. The new summary is hidden
on screen and shown only by print media. Slack requires no separate formatter:
its existing publishing route renders the posted schedule through the same
print/PDF path, so it receives the identical summary and inline partial labels.

## Data Flow

1. The staffing route loads normalized time-off entries and builds the existing
   staffing render model.
2. The render model derives a dedicated list of full-day off names from
   full-day time-off entries and optional-day Off assignments.
3. The staffing template renders that list in a print-only summary.
4. Browser printing applies the print stylesheet directly.
5. Slack publishing renders the posted view to PDF with that same stylesheet
   and uploads the resulting file.

## Empty and Edge States

- Omit the full-day summary when no one is off for the full day.
- Never duplicate a person who appears in both full-day time off and Saturday
  Off data.
- Do not promote a partial-day entry into the full-day summary.
- Preserve the posted-snapshot behavior used by Slack so a later draft cannot
  alter the already-posted schedule PDF.
- Keep the existing safe behavior when time-off data cannot be loaded; the
  schedule still renders without an Off summary.

## Testing

- Render-model tests prove full-day entries are included, partial-day entries
  are excluded, Saturday Off names are included, and duplicates are removed.
- Template/static tests prove the summary is print-only and is omitted when
  empty.
- Print tests prove partial timing remains inline beside assigned employees and
  interactive affordances are hidden.
- Share-route coverage proves Slack still uses the posted schedule's shared
  print/PDF rendering path.

## Out of Scope

- Changing how time off is requested, approved, synchronized, or classified.
- Changing the interactive Time Off or Saturday Off rails.
- Adding separate Slack message text for absences outside the attached schedule
  PDF.
