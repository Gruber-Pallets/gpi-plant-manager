# Unpublished Schedule Inbox Badge - Design

**Date:** 2026-06-25
**Status:** Approved for implementation planning

## Context

The Exception Inbox already aggregates operational to-dos into one page and one
top-nav count. It currently includes assignments, late/absence, missing work
center, missed punch-out, and pending time-off rows. Plant schedules are edited
and published through `/staffing`, and each saved schedule has a `published`
flag. The scheduler defaults to the next working day using the plant work-week
configuration, which is the same business-day concept this alert should use.

Dale wants the inbox to nudge supervisors to publish tomorrow's plant schedule
once it is late enough in the prior business day to matter. The cutoff is
1:30 PM plant-local time. The alert should appear when the target schedule is
not published; this includes both a saved draft and no saved schedule row.

Dale also wants the Inbox menu treatment to be more alerting: whenever the inbox
has any open to-do, the Inbox menu link and notification badge should be red.

## Decisions

| Decision | Choice |
|---|---|
| Alert placement | Add a dedicated inbox section, not a separate global badge. |
| Target day | The next plant business day after today. |
| Cutoff | Show only at or after 1:30 PM plant-local time on the previous business day. |
| Unpublished definition | Any target schedule whose `published` flag is false, including a missing row. |
| Navigation | Link directly to `/staffing?day=YYYY-MM-DD`. |
| Menu color | Any positive inbox total turns the Inbox link/count red; source-error-only remains amber. |

## Behavior

Before 1:30 PM local time, the inbox does not include this schedule reminder.

At or after 1:30 PM, the inbox checks the next business day. If that day's
schedule is not published, the inbox includes a warning section:

- Section title: `Plant Schedule`
- Count: `1`
- Row name: `Plant Schedule`
- Row label: formatted target date, such as `Friday, Jun 26`
- Row detail: `Not published`
- Row badge: `Publish`
- Row priority: `warn`
- Section action: link to `/staffing?day=YYYY-MM-DD`

If the target schedule is already published, the section count is zero and the
inbox remains clear for this source.

On Friday after the cutoff, the target is Monday when the plant work-week is the
standard Monday-Friday schedule. If Settings changes the plant work days, the
alert follows that configuration.

## Architecture

Add a small helper in `exception_inbox.py` that owns the schedule reminder:

1. Read `plant_day.now()` for a timezone-aware plant-local datetime.
2. If local time is before `13:30`, return zero rows.
3. Compute the next business day with a tiny private helper in
   `exception_inbox.py` that reads `schedule_store.current().work_weekdays`,
   matching the scheduler's existing `_next_working_day()` behavior without
   importing a route module.
4. Load `staffing.load_schedule(target_day)`.
5. Return one shaped row when `published` is false.

`build_summary()` includes the schedule count in `sections` and `total`.
`build_snapshot()` adds the new section to the page sections. Both use
`_capture()` so a schedule-source failure marks the inbox degraded without
hiding other work.

The row does not need inline actions. It uses the existing `href` section action,
which keeps the inbox simple and lands the user in the scheduler where publish
already lives.

## Menu Alerting

The current footer code adds an Inbox count badge and makes it red only for
urgent items. Change `updateInboxSummaryLink()` in `footer.js` and the related
CSS so any positive `total` marks the Inbox nav link and count badge red. The
existing degraded amber state still applies when there are source errors and no
open to-dos. If both open to-dos and degraded sources exist, red wins because
the user has work to do.

This applies globally because `_footer.html` loads the same footer script on
every page.

## Error Handling

Schedule-reminder failures are recorded as a source error named
`Plant Schedule`. The inbox still renders the remaining sections, and the nav
badge shows the degraded state if there are no open to-dos. If there are open
to-dos plus the schedule source failed, the nav stays red and the title includes
the degraded note.

## Testing

Add focused tests in `tests/test_exception_inbox.py`:

- Before 1:30 PM, no plant schedule section count is added.
- At or after 1:30 PM, an unpublished next-business-day schedule adds one inbox
  count and a row linking to `/staffing?day=YYYY-MM-DD`.
- A published target schedule adds no count.
- Friday after cutoff targets Monday under a Monday-Friday work-week.
- Summary totals include the schedule reminder.
- A schedule-source exception records `Plant Schedule` in `source_errors`.

Add a static test for `footer.js`/`footer.css` proving the menu uses red styling
for any open inbox total, not only urgent counts.

## Non-Goals

- No new database table.
- No changes to schedule publishing behavior.
- No dismiss/snooze control for the schedule reminder.
- No holiday calendar; business days follow the configured plant work-week.
