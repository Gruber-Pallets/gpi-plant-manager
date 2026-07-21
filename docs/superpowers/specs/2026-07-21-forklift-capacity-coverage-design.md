# Forklift advisor: capacity-coverage recommendation

**Date:** 2026-07-21
**Status:** Approved (design)

## Problem

The staffing-page Forklift advisor recommends a driver crew as *"the smallest
crew whose predicted time-to-claim stays under a target"* using an Erlang-C
queue model calibrated against recorded claim times. Investigation against 104
days of production history showed the premise is invalid:

- Claim time does **not** fall as crew grows over the range actually staffed
  (4-7 drivers). Mean claim time is flat ~207-269s across those crew sizes;
  correlation between claim time and per-driver load is **-0.13** (none).
- The Erlang model under-predicts real claim time by 20-300x, so its calibration
  factor `k` is permanently pinned at its 5.0 clamp and its verdicts (including
  the alarming "Overloaded / Target missed" badge) are not credible.

In this operation the **number of forklift drivers is not the lever that
controls time-to-claim** — travel, call clustering, and dispatch dominate. So no
recalibration can honestly recommend a crew size to hit a claim-time target.

## Goal

Make the "Suggested N" number represent something the data supports: **capacity
coverage** — the smallest crew that can physically keep up with the call volume.
Present the actual claim time as a **measured outcome**, decoupled from the
driver count. Remove the Erlang/SLA subsystem that produced the misleading
recommendation and badge.

Non-goals: predicting claim time; per-driver or hour-by-hour staffing; changing
the forklift ingest, leaderboards, GOAT score, or per-day performance card.

## Design

### 1. Recommendation = capacity coverage

```
planned_λ   = demand_at_percentile(forecast.by_hour, percentile)   # busiest hour by default
effective   = throughput × utilization
recommended = max(1, ceil(planned_λ / effective))
```

- `throughput` — data-derived per-driver calls/hr (`recent_driver_throughput`,
  currently ~19), overridable in Settings; falls back to `DEFAULT_THROUGHPUT`.
- `utilization` — the coverage headroom lever (fraction). **Default 0.75**
  (validated: tracks the ~5.1 mean crew actually staffed; 0.65 recommends ~6.3,
  1.0 recommends ~4.2).
- `percentile` — the existing "Plan for: typical ↔ busiest hour" knob.

This is `forklift_demand.recommend_drivers(planned_λ, effective_throughput)`,
which already exists. Cold-start uses the same formula on the bootstrap
forecast (already folded to clock hours by the prior fix).

There is **no "overloaded" state** — coverage is always a finite crew count.

### 2. Claim time = observed outcome, not prediction

Add `forklift_store.recent_claim_seconds(window_days)`: calls-weighted mean of
`avg_ms / 1000` over the recent window (the same quantity the retired
calibration computed as `actual_wait_seconds`, but surfaced directly).

The advisor carries `observed_claim_seconds` (or `None` when no data). Every
place that showed *"Predicted Time-to-Claim X min"* now shows *"recent avg
time-to-claim: X min"*, clearly labeled as measured history. It is context only
— it does not change with the recommended or scheduled driver count.

### 3. Coverage vs. scheduled (kept)

`forklift_demand.assess_coverage(recommended, scheduled, backups)` is unchanged:
it compares the recommendation to drivers actually scheduled on the Tablets WC
and yields ok / short with a gap. The bay badge shows:

- **"N suggested (coverage)"**
- coverage state vs. scheduled (ok / short by G) — existing `_fk_status` classes
- **"recent avg time-to-claim: X min"** as a muted outcome line (omitted when no
  history yet)

The red "Overloaded / Target missed" branch is deleted.

### 4. Retire the Erlang / SLA subsystem

Remove:

- **`forklift_queue.py`** entirely — `erlang_c_wait_seconds`, `recommend_for_target`,
  `fit_calibration`, `RecResult`, `CalibResult`, `MAX_DRIVERS`, `CALIB_CLAMP`.
  (Nothing else imports it once the advisor is rewritten.)
- **`forklift_advisor`** helpers: `_recommend_for_target`, `_guard_overload`,
  `_fit_calibration`, `_status_for_prediction`, `_scheduled_prediction`,
  `_mean_handle_or_none` (unless `mean_handle_seconds` is used elsewhere — verify;
  if unused there too, drop `forklift_store.mean_handle_seconds` and
  `calibration_samples`). `build_advisor` and `demand_summary` are rewritten to
  the capacity model; keys `overloaded`, `predicted_claim_seconds`,
  `predicted_scheduled_claim_seconds`, `scheduled_prediction_*`,
  `target_seconds`, `backtest`, `calibration_k` are removed and
  `observed_claim_seconds` is added. `live_model` keeps `recommended`,
  `lambda_per_hr`, `effective_throughput`, `driver_wc_names`; drops the Erlang
  fields. `SLIDER_RANGES` drops `target_minutes`.
- **`forklift_settings`**: drop `target_claim_seconds` (dataclass field, DB column
  read/write in `_row_to_settings`/`save`/`_load_from_db`, `Resolved` field,
  `DEFAULT_TARGET_CLAIM_SECONDS`). A DB migration drops the
  `target_claim_seconds` column (additive-safe: keep column, stop reading — OR
  drop; choose keep-and-ignore to avoid a destructive migration).
- **`routes/settings.py`** `_parse_forklift_overrides`: drop the
  `target_claim_seconds` parse; keep `throughput`/`utilization_pct` (already
  parsed).
- **Templates**: `settings.html` forklift section — remove the target slider,
  predicted-TTC copy, and overload copy; re-surface the **utilization** slider
  (and keep throughput/plan-for/history). `staffing.html` forklift bay — replace
  per §3. `static/staffing.js` — remove the Erlang/`forkliftStatusForPrediction`
  live-recalc; keep only coverage display (or a static observed-claim line).

### Data flow

```
forklift_store (history: calls/hr by hour, driver throughput, avg_ms)
   → forklift_demand.forecast (predict_from_history / cold-start fold)
   → forklift_advisor.build_advisor:
        planned_λ = demand_at_percentile(by_hour, percentile)
        recommended = recommend_drivers(planned_λ, throughput × utilization)
        observed_claim_seconds = recent_claim_seconds(window)
        coverage = assess_coverage(recommended, scheduled, backups)
   → staffing.html bay badge  /  settings.html demand summary
```

## Testing

- `forklift_demand`: `recommend_drivers` / `assess_coverage` already covered;
  add a capacity-sizing case at a realistic peak λ.
- `forklift_store`: `recent_claim_seconds` — calls-weighted mean, None on no data.
- `forklift_advisor`: rewrite the SLA tests (`test_build_advisor_*`,
  `test_demand_summary_*`, cold-start tests) to assert the capacity
  recommendation, `observed_claim_seconds`, coverage, and the removed keys are
  gone. Delete `test_forklift_queue.py`.
- `test_settings_forklift.py`: drop target-claim assertions; assert utilization
  override round-trips.
- `test_staffing_forklift_card.py`: assert the coverage badge (no overloaded /
  predicted branches).
- Full suite green (`ZIRA_API_KEY=test .venv/bin/python -m pytest -q`), minus the
  known DB-env-gated skips.

## Rollout

Direct to `main` (per project convention); Railway auto-deploys. Verify the live
advisor via `scripts/diagnose_forklift_overload.py` (updated to the capacity
model) against the read-only DB proxy after deploy.

## Risks / open points

- **Utilization default (0.75)** sets how aggressive coverage is. Tunable in
  Settings; starting value tracks proven-adequate staffing.
- **`target_claim_seconds` column**: keep-and-ignore (non-destructive) rather
  than a drop migration, given the prod-DB caution.
- **`mean_handle_seconds` / handling-time** may be surfaced elsewhere (per-driver
  card, leaderboards) — verify before deleting the store helper; keep if used.
