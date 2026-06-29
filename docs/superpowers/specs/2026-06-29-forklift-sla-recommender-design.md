# Forklift SLA Recommender — size drivers to a time-to-claim target

- **Date:** 2026-06-29
- **Status:** Approved (brainstorm) — pending spec review
- **Builds on / replaces:** the Stage 1 capacity-ratio advisor (`docs/superpowers/specs/2026-06-26-forklift-demand-staffing-design.md`, `2026-06-27-forklift-settings-redesign-design.md`). Uses the on-time history shipped with `2026-06-29-forklift-recognition-design.md`.

## 1. Background & Goal

Today the advisor sizes drivers with a capacity ratio: `recommended = ceil(busiest-hour calls / effective throughput)` ([forklift_demand.py:112](src/zira_dashboard/forklift_demand.py)), where the throughput (~10 calls/hr) is a hand-tuned constant chosen *because* the flat-out rate backs the queue up. It never reads historical time-to-claim and has no service-level target.

Goal (Dale, 2026-06-29): **recommend the smallest number of forklift drivers whose predicted average time-to-claim stays under a tunable target (default 4 minutes), learned thoroughly from history.** The new model replaces the capacity recommendation.

## 2. Decisions (locked in brainstorm)

- **Model = calibrated queueing model.** An Erlang-C (M/M/c) formula predicts time-to-claim for each driver count from the historical arrival rate + handling time, then a single correction factor calibrates it to the **actual recorded** time-to-claim (real arrivals are bursty, so the raw formula is optimistic).
- **Replace, don't add a mode.** The SLA model becomes THE recommendation on the scheduler card and settings. The **target time-to-claim** slider (default 4 min) replaces the *Driver speed* and *Safety slack* sliders. **Plan for: typical↔busiest hour** and **History window** stay. A manual override of the final number stays.
- **Honesty about data granularity.** Driver activity is stored per **day**, not per hour, so calibration is day-level — enough to remove systematic bias, not hour-perfect. The settings page shows a **back-test** (predicted vs. actual avg wait over the window) so the number is auditable.

## 3. The model

For a chosen planning hour:
- **λ — arrival rate (calls/hr):** the per-hour demand at the "Plan for" percentile, from the existing forecast via `forklift_demand.demand_at_percentile(forecast.by_hour, pct)` (pct=1.0 → busiest hour, the default).
- **μ — service rate per driver (calls/hr):** `1 / mean_handle`, where `mean_handle = Σ on_call_ms / Σ calls` over the history window (from `forklift_driver_daily`; `on_call_ms` is summed handling time). Service = handling (claim→complete); the wait *before* service IS the time-to-claim we bound.
- **Offered load:** `a = λ / μ` (Erlangs). The queue is stable only when `c·μ > λ`.
- **Erlang-C expected wait:** `W_q(c) = P_wait(c, a) / (c·μ − λ)` for `c·μ > λ`, where `P_wait` is the Erlang-C probability of queueing. (∞ when unstable.)
- **Calibration factor k:** from history (§4), corrects the formula to observed reality. Calibrated wait = `k · W_q(c)`.
- **Recommendation:** the smallest integer `c`, searching from `ceil(a)+1` up to `MAX_DRIVERS` (default 12), with `k · W_q(c) ≤ target_seconds`. If none qualifies → return `None` and flag **"busiest hour is overloaded — can't hit the target even with MAX drivers."** Demand near zero → recommend 1.

## 4. Calibration (the "thoroughly" part) + its limit

`forklift_store.calibration_samples(window_days)` → one row per eligible historical day (calls ≥ `MIN_DAY_CALLS`=20, ≥1 active driver):
- `avg_lambda` = day's total calls ÷ operating hours (operating hours = count of hours with calls > 0 that day, from `forklift_calls_daily`).
- `crew` = distinct drivers with calls that day (from `forklift_driver_daily`) — the per-day proxy for crew size.
- `actual_wait_seconds` = calls-weighted mean of that day's `avg_ms` ÷ 1000.

`forklift_queue.fit_calibration(samples, mean_handle_seconds)` (pure):
- For each sample compute `pred = erlang_c_wait_seconds(crew, avg_lambda, mean_handle_seconds)`; skip samples where `pred` is ∞/0 (unstable or degenerate).
- `k = median(actual / pred)` over usable samples, **clamped to [0.5, 5.0]**.
- Fewer than `MIN_CALIB_SAMPLES`=5 usable samples → return `k = 1.0` (uncalibrated) with a flag.
- Also returns the **back-test**: `n_samples`, `mean_actual_seconds`, `mean_calibrated_pred_seconds` (= mean of `k·pred`), for display.

**Limit (stated in UI):** crew is day-level, so `k` corrects the average bias of the formula, not hour-by-hour variation. The back-test makes the residual error visible; the manual override + target slider let Dale add margin if the back-test shows the model running optimistic.

## 5. Settings — extend `forklift_settings`

Reuse the nullable-override + `Resolved` pattern.
- **Add** `target_claim_seconds` (NUMERIC NULL; default 240 = 4 min). Guarded idempotent `ALTER TABLE forklift_settings ADD COLUMN IF NOT EXISTS target_claim_seconds NUMERIC NULL`.
- **Stop reading** `throughput_override` and `utilization_override` in the recommendation path (the capacity knobs). Leave the columns in place (harmless), per the prior redesign's approach.
- **Keep** `plan_for_percentile_override` and `history_samples_override`.
- `Resolved` gains `target_claim_seconds` (override-or-default 240) and a helper to expose it; `algorithm_values()` exposes the default for the grey tick.

## 6. Modules / boundaries

- **`forklift_queue.py` (new, pure):** `erlang_c_wait_seconds(c, lambda_per_hr, mean_handle_seconds) -> float` (∞ if `c·μ ≤ λ`); `recommend_for_target(lambda_per_hr, mean_handle_seconds, target_seconds, k, max_drivers=12) -> RecResult` where `RecResult = {drivers: int|None, predicted_seconds: float|None, overloaded: bool}`; `fit_calibration(samples, mean_handle_seconds) -> CalibResult {k, n_samples, mean_actual_seconds, mean_pred_seconds, uncalibrated: bool}`. Fully unit-testable; no DB/templates.
- **`forklift_store.py`:** `mean_handle_seconds(window_days) -> float|None` (`Σ on_call_ms / Σ calls / 1000`; None if no calls); `calibration_samples(window_days) -> list[dict]` (§4). Joins `forklift_calls_daily` (hourly) + `forklift_driver_daily`.
- **`forklift_demand.py`:** unchanged except the capacity `recommend_drivers` is no longer called by the advisor (kept for now; may be removed in a later cleanup). `demand_at_percentile` reused for λ.
- **`forklift_advisor.py`:** `build_advisor` now computes λ (forecast at percentile), `mean_handle`, `k` (calibration), and calls `recommend_for_target`. Returns: `recommended` (the count or None/overloaded), `predicted_claim_seconds`, the `target_seconds`, the back-test, and the algorithm's own value (for the discreet baseline). Stays defensive — any failure yields a "can't compute yet" advisor, never raises into the request.
- **Routes/templates:** `routes/settings.py` (parse `target_claim_seconds`; build the panel context incl. back-test) + `templates/settings.html` (target slider replaces driver-speed/safety-slack; keep plan-for/history; show back-test + manual override). `templates/staffing.html` scheduler card copy: "Recommend **N** drivers to keep time-to-claim under **4 min** · predicted **~X min**" (overloaded → the flag message).

## 7. Error handling / edge cases
- Unstable (`c·μ ≤ λ`) → wait ∞; the search starts above the stability floor and caps at `MAX_DRIVERS`; no qualifying c → `overloaded` flag, not a fabricated number.
- No/thin history: `mean_handle_seconds` None or `< MIN_CALIB_SAMPLES` → `k = 1.0` uncalibrated (labeled); if even λ/μ can't be computed, the advisor shows "recommendation builds as history accrues" (today's cold-start behavior).
- Calibration ratio clamped [0.5, 5.0] so one weird day can't blow up `k`.
- All render-time computation defensive (mirrors `forklift_advisor`); a data hiccup degrades the card/panel, never 500s.

## 8. Testing
- `forklift_queue.erlang_c_wait_seconds`: known Erlang-C values (hand-computed for small c,a), ∞ when unstable, monotonic decrease in c.
- `recommend_for_target`: picks the smallest c under target; tighter target → more drivers; overload → `{drivers: None, overloaded: True}`; near-zero λ → 1; respects `max_drivers`.
- `fit_calibration`: median ratio, clamp bounds, `< MIN_CALIB_SAMPLES` → k=1 uncalibrated, back-test fields correct; skips unstable samples.
- `forklift_store.mean_handle_seconds` / `calibration_samples`: DB-gated; shape + operating-hours + crew + weighted-actual math.
- `forklift_advisor`: returns target + predicted + back-test + algo value; uses `recommend_for_target` not the capacity ratio; defensive fallback with no DB.
- `forklift_settings`: `target_claim_seconds` override resolution (auto→240; set→value), nullable round-trip (DB-gated).
- Routes: settings panel renders the target slider + back-test + override (no driver-speed/safety-slack); `POST /settings/forklift` sets/clears `target_claim_seconds`; scheduler card renders the new copy incl. the overloaded path.

## 9. Out of scope
- Removing the now-unused capacity columns/`recommend_drivers` (later cleanup).
- Per-hour crew tracking (would sharpen calibration; future).
- Increment B (people-level "dedicate X" suggestions) — still parked.
- Forklift recognition / GOAT score — already shipped; untouched here.
