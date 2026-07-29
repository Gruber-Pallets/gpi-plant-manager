# Forklift Throughput History Repair Design

**Date:** 2026-07-29

## Goal

Keep forklift staffing suggestions realistic when historical per-driver rows
contain on-call time but do not contain their completed-call counts.

## Root cause

The recommendation divides forecast demand by measured driver throughput.
Throughput currently sums `calls` and `on_call_ms` from
`forklift_driver_daily`.

The historical on-time reconstruction created many driver-day rows with valid
on-call time and `calls = 0`. The separate `forklift_calls_daily` table still
has complete daily call totals. Combining one recent day of driver calls with
many days of reconstructed on-call time reduced measured throughput from about
19 calls per hour to about 0.72, which inflated a normal recommendation to 160
drivers.

## Design

`forklift_store.recent_driver_throughput()` will calculate fleet throughput
from paired daily sources:

- Sum on-call time by day from `forklift_driver_daily`.
- Join those days to `forklift_calls_daily`.
- Keep only days with positive call totals and positive on-call time.
- Divide the paired daily call total by the paired on-call hours.

The calculation will continue using the existing lookback window. It will not
use incomplete per-driver `calls` values and will not rewrite historical data.

If the paired history contains fewer than two total on-call hours or no calls,
the function will return `None`. The existing advisor behavior will then use
the 16-calls-per-hour fallback.

The forecast, utilization allowance, planned-hour percentile, schedule
coverage counting, and displayed recent claim time will remain unchanged.

## Data flow

1. The advisor requests recent driver throughput.
2. The store pairs daily demand totals with daily on-call totals.
3. The advisor applies its utilization allowance to that measured rate.
4. The recommendation remains
   `ceil(planned hourly calls / effective driver throughput)`.

With the current 23 paired production days, the measured rate is about 19.02
calls per hour. An 85-call planned peak therefore recommends about six drivers
instead of 160.

## Failure handling

- Missing or unpaired days are ignored.
- Insufficient paired history returns `None`.
- Database read failures keep the advisor's existing safe fallback behavior.
- No migration or destructive data cleanup is required.

## Testing

Add a regression test that models the production shape: complete daily call
totals, reconstructed on-call time, and missing per-driver call counts. The
test must fail under the current single-table calculation and pass when the
paired-history query is used.

Run the focused forklift store and advisor tests, then the full relevant test
files and lint checks before committing the implementation.
