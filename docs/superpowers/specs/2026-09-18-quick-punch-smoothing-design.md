# Quick-Punch Smoothing

## Purpose

When a person signs out and back in, or taps the wrong station and corrects
it, within a few minutes, treat it as one continuous stint. The station bars
and pallet credit should read as if the mistake never happened. Odoo's
attendance records, hours, and payroll stay exactly as punched.

## Observed Case (2026-09-18)

Read-only look at production data for the screenshot Dale shared:

| Person | Odoo attendance rows (Central time) |
|---|---|
| Christian C. | Dismantler #3 07:00:00–07:02:10, Dismantler #2 07:02:10–07:04:27, Dismantler #3 07:04:27–11:00, Dismantler #3 11:30–open |
| Jose C. | Dismantler #2 07:00–11:00, Dismantler #2 11:30–open |

Station bars are one row per work center, with every stint at that station laid
end to end in time order. Each stint draws its own goal tick.

- **Dismantler 3's bar** starts with Christian's 2-minute stint (a lone
  striped tick at the far left), then one stint joined across lunch.
- **Dismantler 2's bar** shows Jose's morning stint (green), Christian's
  2-minute detour, then Jose's afternoon stint (red). Jose did nothing
  unusual. His lunch split should have been joined for display, but a bug in
  `production_segments.coalesce_display_scores` only compares a stint with the
  stint immediately before it in time at that station. Christian's detour sat
  between Jose's two stints and blocked the join.

Over the prior 30 days, stints under 10 minutes included 7 quick detours
(A → B → back to A), 2 same-station sign-out/sign-in pairs, and about 5 short
first picks followed by a move. Roughly half of all short stints were under
2 minutes. Christian's detour lasted 2 minutes 17 seconds.

## Decisions (Dale, 2026-09-18)

- **Scope:** smooth the app's own record of who was where *before* pallets are
  credited. Bars and pallet credit both see the smoothed stints. Never change
  Odoo attendance records.
- **Patterns:** same-station gap, quick detour, and wrong first pick.
- **Limit:** one fixed 5-minute limit for all rules. It is not a Settings knob.
- **Real work is never erased (added during the build):** the wrong-first-pick
  rule applies only when that station's meter proves no pallets were made
  during the short stint. A short first stint with real production is real
  work. Found when an existing test's 5-minute Repair 2 stint with 34 pallets
  would otherwise have been folded away, leaving those pallets unassigned.

## Rules

Rules apply per person (keyed by Odoo employee ID) to that person's stints in
time order. They use one constant, `QUICK_PUNCH_LIMIT = timedelta(minutes=5)`.
A boundary at exactly 5:00 counts as quick (`<=`).

1. **Came-back rule.** Covers both the same-station gap and the quick detour.
   Suppose a stint at station A ends, and a later stint for the same person at
   station A starts no more than 5 minutes after that end. Then everything
   between them becomes part of one stint at A: a sign-out gap, stints at other
   stations, or both. Apply it left to right until nothing changes, measuring
   from the end of the current merged stint.
2. **Wrong-first-pick rule.** A stint qualifies when:
   - it starts a presence block, meaning no stint for this person ends within
     5 minutes before its start (start of day, or back from being away longer
     than 5 minutes);
   - it lasts 5 minutes or less;
   - the person's next stint is at a different station and starts no more than
     5 minutes after it ends; and
   - the station's meter data is known and shows no pallets during the stint
     (`start <= sample time < end`, the same window pallet credit uses). With
     no meter data for the station, the rule does not fire.

   The short stint joins the next stint, which now starts at the short stint's
   start, so any small gap is filled too. Repeat until stable, measuring the
   absorbed stint as it stands, so a chain of blips before settling is handled
   when their total stays within 5 minutes.
3. **Order:** run the came-back rule to a fixed point first, then the
   wrong-first-pick rule. The order matters. Run in the other order,
   Christian's first 2 minutes would be moved to Dismantler 2 before his return
   to Dismantler 3 was seen.
4. **Left alone:**
   - A stint that is still running. It cannot be judged yet and is smoothed
     after the fact once the person comes back.
   - A short mid-day stop between two different stations (A → B → C).
   - Anything longer than 5 minutes.
   - Other people's stints. Smoothing never moves one person's time onto
     another person.
   - Any stretch that overlaps a `conflicting_location`, `unmapped_location`,
     or `stale_open_location` span for that person. This keeps the existing
     promise that pallet credit is never invented while Odoo's location data
     conflicts or is unknown. Moments when the person was clocked in without a
     station tagged (`pending_first_location`, `missing_required_location`,
     `exempt_no_location`) do not block smoothing. Those are the brief
     no-station moments a transfer produces.
   - A detour whose in-between stint starts before the person left, or runs
     past their return. That only happens with overlapping, bad source data,
     and it is left unsmoothed rather than absorbing a long stint.

At the live edge the picture can change retroactively. For example, while
Christian was on Dismantler 2 at 07:03, the wrong-first-pick rule showed him at
Dismantler 2 from 07:00. Once he returned to Dismantler 3 at 07:04:27, the
came-back rule redrew him as Dismantler 3 from 07:00 onward. This is expected.

With these rules, today's data becomes: Christian at Dismantler 3 07:00–11:00
and 11:30–now; Jose at Dismantler 2 07:00–11:00 and 11:30–now. Lunch stays a
real gap, and the existing display join covers it.

## Architecture

- **New pure module** `src/zira_dashboard/quick_punch_smoothing.py`:
  `smooth_quick_punches(segments, *, blocked_windows=None, limit=QUICK_PUNCH_LIMIT)`
  plus `person_key(segment)`, which defines the grouping key: the Odoo ID, or
  the name when there is no ID. `blocked_windows` must use that same key. It
  takes and returns `WorkSegment`s, with no DB, network, or clock access.
  Smoothed segments keep `source="odoo"`, the person's name and ID, and the
  absorbing station's name.
- **Single call site:** `assignment_windows.work_segments_from_timeline`.
  Build unclipped segments from valid spans and collect each person's blocking
  windows from the non-valid spans listed above. Smooth per person, then clip to
  the requested window and drop non-positive results. Smoothing sees as much
  punch history as the caller's spans carry. Timeline spans arrive already
  clipped to the range the caller read, so a stint cut at that boundary is
  judged by its visible length.
- **Meter data:** `work_segments_from_timeline` takes an optional
  `production_times_by_wc` (from `quick_punch_smoothing.production_times_from_samples`).
  All three callers pass the same samples they credit, so dashboards, People
  Performance, and leaderboards agree. The department route already loads
  meters before building stints. `_strict_inputs_for_day` builds stints after
  its samples are validated. `production_scores_for_timeline` rebuilds them
  once its samples are parsed.
- **Order preserved:** people and stints that smoothing does not touch keep
  their original order in the output. A merged stint takes the position of its
  earliest input stint.
- Every pallet-credit consumer already goes through
  `work_segments_from_timeline`, so they all receive smoothed stints with no
  other wiring:
  - department dashboards (`routes/departments.py`: `/recycling`,
    `/tv/recycling`, `/new`, `/tv/new`);
  - People Performance scores (`production_history.production_scores_for_timeline`);
  - strict daily inputs (`production_history._strict_inputs_for_day`), which
    feed attribution, leaderboards, player cards, awards, GOAT Watch, and the
    Exception Inbox's unassigned-pallet runs
    (`wc_attributions.shadow_unassigned_runs_for_day`).

### Unchanged

- Odoo attendance records, the kiosk punch log, hours, auto-lunch, and payroll
  sync.
- `LocationSpan`s and everything that reads them directly: live "who's at this
  station now" (staffing, `/wc` dashboards, machine breakdowns), attendance
  exception checks, readiness, and People Performance location ribbons. These
  keep showing real punches.
- The legacy rollback path (`assignment_windows.resolve_segments`), which is not
  used in production today.
- Already-saved past results, such as awarded trophies, are not rewritten.
  Anything recalculated from now on uses smoothed stints.

## Display Join Bug Fix

`coalesce_display_scores` will join a score with the same person's most recent
merged score at that station, not merely the immediately preceding score.
Other people's stints in between do not block the join. All existing join
conditions stay the same: same name and Odoo ID, same station, and a gap that
is zero or falls entirely inside a scheduled break. Output stays sorted by
start time, and pallet credit is unchanged because this step is display-only.

## Error Handling

- Segments without a person ID fall back to the person's name as the grouping
  key.
- If one person's stints overlap (bad source data), smoothing treats the
  overlap as a zero gap and never lengthens a stint past the latest end it
  absorbs.
- Smoothing never creates time outside the span of the stints it merges. A
  filled gap lies strictly between two of that person's stints.

## Testing

Pure unit tests in `tests/test_quick_punch_smoothing.py`:

- Christian's real 2026-09-18 sequence becomes Dismantler 3 07:00–11:00 plus
  11:30–open.
- Same-station gap under the limit is bridged; exactly 5:00 is bridged; 5:01 is
  not.
- A detour with multiple blips inside 5 minutes is absorbed.
- Wrong first pick at the start of the day and after a break longer than
  5 minutes is absorbed into the next station, but only when that station's
  meter shows no pallets during it. A pallet at the stint start keeps it; one
  at the stint end does not. Missing meter data keeps it.
- A mid-day A → B(short) → C is left alone.
- The rule order is proven: a sequence where the wrong-first-pick rule alone
  gives the wrong answer.
- A still-running detour is not smoothed; closing it by returning is.
- Other people's segments are never changed.
- Lunch (a 30-minute gap) is not bridged.

Integration tests:

- `work_segments_from_timeline`: smoothing runs before clipping. It never
  bridges a conflicting location, and it does bridge a no-station moment.
- `credit_work_segments`: meter samples inside a filled gap are credited to the
  person instead of going unassigned.
- `coalesce_display_scores` regression: Jose's two lunch-split stints join even
  with another person's stint between them.
- Each caller passes its meter data to smoothing. The department transfer test,
  where a 5-minute Repair 2 stint made 34 pallets, keeps that stint.

Validation: focused tests, then the full suite
(`ZIRA_API_KEY=test .venv/bin/python -m pytest -q`) and lint, before pushing.

## Release Notes

Add a plain-language What's New entry to `CHANGELOG.md`, written so a
10-year-old can follow it. Example: "If someone taps the wrong station or signs
out and back in by mistake within 5 minutes, the dashboard now shows it as one
steady stretch of work, and their pallets still count for them."

## Acceptance Criteria

- The Dismantler 2 and Dismantler 3 bars for 2026-09-18 each show one stint per
  person per work period, with no stray goal ticks from the 07:00–07:04
  mistake.
- Pallets made during a smoothed gap or detour are credited to the person who
  came back.
- Odoo attendance rows are never written by this feature.
- Live who's-here-now views and attendance exception checks are unchanged.
