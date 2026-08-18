# Repair 4 Zira Meter Design

## Goal

Connect Repair 4 to its new Zira meter, `44483`, in exactly the same way as
Repairs 1–3 so its production appears everywhere the app consumes metered
repair data.

## Existing behavior

Repair 4 already exists as a Recycled work center in Bay 5. It has no meter ID
in `staffing.LOCATIONS` and is absent from `stations.STATIONS`. Those two gaps
exclude it from work-center production attribution and from the Recycling
dashboard's station bundle.

## Design

- Add `Station(meter_id="44483", name="Repair 4", category="Repair",
  cell="Recycling")` to the shared station registry beside Repairs 1–3.
- Set Repair 4's `staffing.Location.meter_id` to `"44483"`.
- Keep all fetch, cache, attribution, dashboard, goal, and recognition logic
  unchanged. Those systems already discover metered work centers from these
  registries.
- Add a focused invariant test proving both registries map Repair 4 to the same
  meter and that it is part of the Recycling station bundle.
- Add a short user-facing changelog note.

## Error handling

No new runtime error path is needed. Zira fetch failures continue through the
same handling used by every existing station.

## Validation

Before implementation, confirm that Zira accepts meter `44483` and returns
readings. Then use a red-green test for the registry mapping, run focused
production-data tests, run the full test suite, and run Ruff.

