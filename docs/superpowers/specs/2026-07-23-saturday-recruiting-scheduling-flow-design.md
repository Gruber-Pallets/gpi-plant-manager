# Saturday Recruiting Scheduling Flow

**Date:** 2026-07-23

## Goal

Make Saturday planning an explicit manager-driven sequence:

1. Open a blank Saturday.
2. Turn on the work centers that should run.
3. Start recruiting for those centers.
4. After recruiting closes, use the Inbox action to prepare the schedule.
5. Finish the committed crew manually or with the existing Auto control.

Recruiting and automatic scheduling must never start merely because time passes
or a background job runs.

## Current Problem

New future days now load configured default people. That behavior also applies
to Saturday, so the Saturday recruiting lifecycle guard sees existing
assignments and rejects activation with:

> Clear existing Saturday assignments before activating recruiting.

The seeded Saturday assignments are also conceptually wrong. The manager has
not yet selected which work centers will run, and the committed Saturday crew
is not known until recruiting finishes.

## Manager Workflow

### 1. Blank Saturday

A newly opened future Saturday persists a blank draft:

- no assigned people;
- no Auto work centers enabled;
- normal Saturday shift hours and other day metadata remain available.

The manager can turn on the work centers that should operate. Changing these
selections does not schedule people.

### 2. Start recruiting

Once at least one eligible work center is enabled, the Scheduler shows a blue
button labeled `Recruit N`, where `N` is the number of enabled recruiting work
centers.

The button uses the Publish button's size, weight, border radius, and general
interaction style, with a blue background. Pressing it is the only action that
starts recruiting.

Recruiting demand is derived from the enabled centers and their configured
minimum crew, as it is today.

While recruiting is open, the schedule stays blank. The manager can review
responses, but default preparation and the optional Auto rebuild are not
available until recruiting is closed.

For Saturdays created before this change, activation may remove existing
assignments only when every assignment is recorded as a seeded default.
Unknown, generated, or manual assignments continue to block activation so
manager work is never silently erased.

### 3. Recruiting closes

The existing deadline process may change the recruiting round from
`recruiting` to `closed`. That transition only stops employee responses. It
does not assign people, run Auto, or otherwise change the schedule.

A closed recruitment that has not yet been prepared appears in the Exception
Inbox as a warning-level action:

- title: `Saturday recruitment`;
- label: the Saturday date;
- detail: committed-person count and a prompt to schedule the crew;
- action: `Schedule Saturday`;
- destination: `/staffing?day=YYYY-MM-DD`.

The Inbox summary count includes this action. The action remains open until
the recruitment has been prepared.

### 4. Prepare the closed recruitment

Opening the Scheduler for a closed, unprepared Saturday performs one
idempotent preparation step:

1. Read the committed recruits and manager availability overrides.
2. Exclude anyone who is no longer effectively available.
3. Consider only the work centers enabled for that Saturday.
4. Place available recruits into their configured exact or group default
   centers when a valid enabled default spot exists.
5. Leave all other available recruits unassigned.
6. Persist the placements with `default` assignment sources.
7. Mark the recruitment as prepared.

This is default placement, not an Auto rebuild. It does not fill minimum crew,
move people without defaults, or schedule non-recruits.

The prepared marker is persisted on the recruiting round. It prevents a later
page load from reapplying defaults after a manager manually moves or removes
someone. If preparation fails, the marker is not written and the Inbox action
remains available.

Opening the same Saturday directly, rather than through the Inbox link,
performs the same one-time preparation after recruiting is closed.

### 5. Finish the schedule

After preparation, the manager can:

- place remaining committed recruits manually; or
- press the existing Auto control.

On a closed, prepared Saturday recruiting round, Auto's candidate roster is
restricted to effectively available committed recruits. Existing default
placements and manager placements are preserved as locks. Auto fills the
remaining committed people into enabled, qualified work centers according to
the existing rotation and capacity rules. Auto is unavailable while recruiting
is still open.

Auto runs only when the manager presses its button. No deadline worker, page
load, recruiting action, or Inbox process invokes Auto.

## Data Model

Add a nullable preparation timestamp to `saturday_recruitments`:

```text
staffing_prepared_at timestamptz null
```

`NULL` means a closed recruitment still needs schedule preparation. A value
means default placement has already been attempted successfully and must not
run again.

Cancellation and a new recruiting round for another date do not reuse this
marker. Publishing remains represented by the existing recruiting status and
schedule publication fields.

## Components

### Future-day seeding

The future-draft seeding path treats Saturday separately:

- `assignments = {}`;
- `assignment_sources = {}`;
- `auto_enabled_work_centers = []`.

Weekday seeding and Reset-to-defaults behavior remain unchanged.

### Recruiting activation

The recruiting store keeps its published-schedule guard and manual-assignment
guard. It gains source-aware cleanup for legacy drafts containing only seeded
defaults, performed in the same transaction as activation.

### Closed-Saturday preparation

A focused preparation helper owns the one-time transition from closed
recruiting to a default-seeded committed crew. It returns the persisted
schedule and is safe to call repeatedly.

### Inbox

The Inbox reads closed, unprepared recruiting rounds from local Postgres and
adds them as their own queue section. A source read failure is surfaced through
the Inbox's existing degraded-source behavior instead of hiding other items.

### Optional Auto rebuild

The rebuild route derives the effective committed-name set for recruiting
Saturdays and filters the roster before invoking the existing scheduler. The
normal weekday Auto path is unchanged.

## Error Handling and Safety

- No selected work centers: Recruit remains unavailable and the API continues
  to reject activation.
- Published Saturday: recruiting activation remains blocked.
- Legacy default-only assignments: cleared transactionally during activation.
- Any manual, generated, or unknown-source assignment: activation is blocked
  with the existing clear-assignments guidance.
- Recruiting lookup failure: no preparation or rebuild is saved.
- Default preparation failure: no prepared marker is recorded; the Inbox item
  remains open for retry.
- Zero committed recruits: preparation succeeds with a blank schedule and
  closes the Inbox action; the manager may adjust centers or leave the
  Saturday unpublished.
- Repeated page loads: do not reapply defaults after preparation.
- Auto on a closed, prepared recruiting Saturday: never schedules a
  non-committed person.

## Testing

Add regression coverage for:

- a new future Saturday seeding with no assignments and no enabled centers;
- weekday future seeding remaining unchanged;
- Recruit activating from manager-selected centers on a blank Saturday;
- legacy default-only assignments being cleared during activation;
- manual, generated, and unknown-source assignments still blocking activation;
- the button rendering as `Recruit N` with Publish-compatible classes and blue
  styling;
- the close process changing status without scheduling anyone;
- closed, unprepared recruiting appearing in the Inbox and summary count;
- the Inbox action linking to the correct Saturday;
- first closed-Saturday page load placing committed recruits into valid enabled
  defaults and marking preparation complete;
- non-committed people never entering prepared assignments;
- repeated page loads not reapplying defaults;
- preparation failure retaining the Inbox action;
- zero-commitment preparation completing cleanly;
- Saturday Auto receiving only effectively available committed recruits;
- Auto preserving prepared defaults and manual locks;
- weekday Auto behavior remaining unchanged.

## Non-Goals

- Automatically starting recruiting.
- Automatically running Auto at the recruiting deadline.
- Automatically running Auto when the Scheduler or Inbox opens.
- Changing weekday seeding or Reset-to-defaults behavior.
- Replacing the manager's ability to schedule Saturday manually.
