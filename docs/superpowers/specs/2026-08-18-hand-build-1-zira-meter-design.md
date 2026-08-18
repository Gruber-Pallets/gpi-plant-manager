# Hand Build #1 Zira Meter Design

## Goal

Connect Hand Build #1 to Zira meter `44484` and give the full two-person crew
a 400-pallet daily goal. Reuse the existing New production pipeline so Hand
Build output appears throughout the app without a Hand-Build-specific fetch or
scoring path.

## Verified source

The live Zira API accepts meter `44484`. Its readings use the same fields the
current production pipeline expects, including `event_date`, `status`, `units`,
and `duration`. A live sample contained both `Working` and `Stop` rows and 467
positive rows totaling 505 units.

Zira also exposes `product`, `length`, and `width`, but the current readings
identify every unit only as `product="Pallet"`; length and width are blank.
Size-aware targets are therefore outside this change. The initial goal is 400
pallets per full workday regardless of size.

## Existing application support

The app already contains the larger Hand Build behavior:

- Hand Build #1 is a two-person `Hand Build` work center in Bay 6.
- Its persisted department is `New` and its group is `Hand Builds`.
- The New value-stream dashboard discovers every metered New location.
- Production history discovers every location with a meter and attributes its
  output to the saved schedule.
- The New leaderboard already defines a Hand Build family containing Hand
  Build #1, Hand Build #2, and Big Build #1.
- GOAT recognition and post-shift notifications already become eligible when
  a Hand Build location receives a meter.
- The missing-work-center detector uses the shared station registry.

The missing pieces are the meter mappings and a usable goal.

## Design

### Static meter mapping

Set Hand Build #1's `staffing.Location.meter_id` to `"44484"`. Add a matching
`Station` to `stations.STATIONS` with:

- name: `Hand Build #1`
- category: `Hand Build`
- cell: `New`

The two registries must agree on the meter ID. This follows the same invariant
used by other metered work centers while retaining the station's real
production family instead of labeling it as Repair, Dismantler, or Other.

### Safe database backfill

On schema bootstrap, update the existing Hand Build #1 `work_centers` row:

- Fill `meter_id` with `44484` only when the saved value is blank.
- Change `goal_per_day_override` to `400` only when the saved value is null or
  zero.

This upgrades the current production row while preserving any later deliberate
meter replacement or nonzero goal change.

### Automatic downstream behavior

No downstream special cases are added. Once both mappings exist:

1. The New value-stream station resolver includes Hand Build #1.
2. The normal Zira cache and daily persistence fetch its readings.
3. Production attribution assigns the work center's units across its scheduled
   operators using the existing attribution rules.
4. The New leaderboard activates the Hand Build family when qualifying
   production history exists.
5. Hand Build becomes eligible for existing GOAT, ribbon, and Slack record
   recognition.
6. Unscheduled production at Hand Build #1 can appear in the existing
   missing-work-center workflow.
7. The 400-pallet full-day goal is prorated by existing pace and progress
   calculations.

### TV behavior

Do not seed a new physical TV display. Hand Build data will appear on the
existing New dashboard and New leaderboard. The existing work-center dashboard
can resolve Hand Build #1 by name; a dedicated TV registry entry can be added
later if the plant installs or assigns a screen for it.

## Size-aware goals deferred

When Zira begins populating product dimensions, follow-up work will:

1. define per-size Hand Build rates,
2. persist the daily size mix instead of only the final station total, and
3. compute an expected goal from the actual mix and time spent on each size.

This change does not create an unused rate table or infer sizes from blank
fields. The 400-pallet goal remains the fallback until that follow-up ships.

## Error handling

Hand Build #1 uses the same Zira request, pagination, cache, and failure
handling as existing meters. A Zira outage remains isolated by the existing
dashboard and warmer error paths. The schema backfill is idempotent and cannot
overwrite later nonblank/nonzero configuration.

## Testing and validation

- Use a failing test to prove Hand Build #1 is initially absent from the shared
  station registry and has no staffing meter.
- Assert both registries resolve meter `44484` and the station participates in
  the New/metered paths.
- Assert the schema contains guarded meter and 400-goal backfills.
- Cover New station discovery, production-history station discovery, Hand Build
  category activation, and missing-work-center station membership.
- Run focused tests, Ruff, and the broad local suite with database URLs disabled
  so tests cannot touch production.
- Retain the live read-only Zira evidence that meter `44484` returns compatible,
  positive production data.

