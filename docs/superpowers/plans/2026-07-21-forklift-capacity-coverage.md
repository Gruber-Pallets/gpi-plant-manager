# Forklift Capacity-Coverage Advisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the forklift advisor's Erlang-C time-to-claim SLA recommender with a capacity-coverage recommendation, and surface real claim time as a measured outcome instead of a prediction.

**Architecture:** The recommendation becomes `ceil(planned_λ / (throughput × utilization))` using demand already forecast per clock-hour. Claim time is read directly from history and shown as context, decoupled from the driver count. The entire Erlang/calibration/SLA subsystem (`forklift_queue.py`, the target slider, "overloaded" badge, predicted-TTC) is deleted.

**Tech Stack:** Python 3.12, FastAPI + Jinja2 templates, vanilla JS, Postgres, pytest. Run tests with `ZIRA_API_KEY=test .venv/bin/python -m pytest`.

---

## Reference: design spec

`docs/superpowers/specs/2026-07-21-forklift-capacity-coverage-design.md`

## File structure

- `src/zira_dashboard/forklift_store.py` — add `recent_claim_seconds`; remove `mean_handle_seconds`, `calibration_samples`.
- `src/zira_dashboard/forklift_advisor.py` — rewrite `build_advisor` + `demand_summary` to capacity model; remove SLA helpers; drop `target_minutes` from `SLIDER_RANGES`.
- `src/zira_dashboard/forklift_queue.py` — **delete** (nothing imports it after the rewrite).
- `src/zira_dashboard/forklift_settings.py` — remove `target_claim_seconds` field / `Resolved` field / `DEFAULT_TARGET_CLAIM_SECONDS` / resolve + DB read/write of it. Leave the DB column in place (ignored).
- `src/zira_dashboard/routes/settings.py` — drop `target_claim_seconds` from `_parse_forklift_overrides`.
- `src/zira_dashboard/routes/staffing.py` — trim `forklift_live_model` to the capacity fields.
- `src/zira_dashboard/templates/staffing.html` — rewrite the Forklift bay badge.
- `src/zira_dashboard/templates/settings.html` — remove target slider + predicted/overload/back-test copy; add utilization slider.
- `src/zira_dashboard/static/staffing.js` — remove client Erlang-C + live TTC recalc; keep coverage-only.
- `scripts/diagnose_forklift_overload.py` — retarget to the capacity model.
- Tests: `test_forklift_store.py` (+ `test_forklift_store_pure.py`), `test_forklift_advisor.py`, `test_forklift_demand.py`, `test_settings_forklift.py`, `test_staffing_forklift_card.py`; **delete** `test_forklift_queue.py`.

---

## Task 1: Observed claim-time store helper

**Files:**
- Modify: `src/zira_dashboard/forklift_store.py`
- Test: `tests/test_forklift_store.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_forklift_store.py`:

```python
def test_recent_claim_seconds_calls_weighted_mean(monkeypatch):
    from zira_dashboard import forklift_store as st
    rows = [{"wms": 3_000_000, "calls": 20}]  # 3,000,000 ms over 20 calls -> 150 ms? no: /calls/1000
    monkeypatch.setattr(st, "db", type("D", (), {"query": staticmethod(lambda *a, **k: rows)}))
    # 3_000_000 ms / 20 calls / 1000 = 150.0 s
    assert st.recent_claim_seconds(90) == 150.0


def test_recent_claim_seconds_none_on_no_calls(monkeypatch):
    from zira_dashboard import forklift_store as st
    monkeypatch.setattr(st, "db", type("D", (), {"query": staticmethod(lambda *a, **k: [{"wms": 0, "calls": 0}])}))
    assert st.recent_claim_seconds(90) is None
```

Note: `forklift_store` imports `db` lazily inside each function (`from . import db`). Match that — see Step 3; the monkeypatch above replaces the module attribute, so the helper must reference `db` via a module-level name. Simplest: keep the lazy import but assign to a local; the test instead patches `zira_dashboard.db.query`. Replace the two tests' monkeypatch with:

```python
def test_recent_claim_seconds_calls_weighted_mean(monkeypatch):
    from zira_dashboard import forklift_store as st, db
    monkeypatch.setattr(db, "query", lambda *a, **k: [{"wms": 3_000_000, "calls": 20}])
    assert st.recent_claim_seconds(90) == 150.0


def test_recent_claim_seconds_none_on_no_calls(monkeypatch):
    from zira_dashboard import forklift_store as st, db
    monkeypatch.setattr(db, "query", lambda *a, **k: [{"wms": 0, "calls": 0}])
    assert st.recent_claim_seconds(90) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_store.py -k recent_claim_seconds -v`
Expected: FAIL — `AttributeError: module 'zira_dashboard.forklift_store' has no attribute 'recent_claim_seconds'`.

- [ ] **Step 3: Implement the helper**

Add to `src/zira_dashboard/forklift_store.py` (next to `recent_driver_throughput`):

```python
def recent_claim_seconds(window_days: int = 90) -> float | None:
    """Observed mean time-to-claim (seconds) over the window: the calls-weighted
    mean of forklift_driver_daily.avg_ms. None when there are no calls. This is a
    MEASURED outcome, not a prediction."""
    from . import db
    rows = db.query(
        "SELECT COALESCE(SUM(avg_ms * calls),0) AS wms, COALESCE(SUM(calls),0) AS calls "
        "FROM forklift_driver_daily WHERE day >= (CURRENT_DATE - %s::int)",
        (window_days,),
    )
    if not rows or not rows[0]["calls"]:
        return None
    return float(rows[0]["wms"]) / float(rows[0]["calls"]) / 1000.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_store.py -k recent_claim_seconds -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/forklift_store.py tests/test_forklift_store.py
git commit -m "feat(forklift): observed recent claim-time store helper"
```

---

## Task 2: Rewrite `build_advisor` to the capacity model

**Files:**
- Modify: `src/zira_dashboard/forklift_advisor.py:180-279` (`build_advisor`)
- Modify: `src/zira_dashboard/forklift_advisor.py` (add `_observed_claim_or_none`, `_CLAIM_WINDOW_DAYS`)
- Test: `tests/test_forklift_advisor.py`

- [ ] **Step 1: Rewrite the SLA tests to capacity expectations**

Replace these tests in `tests/test_forklift_advisor.py` with the versions below:
`test_build_advisor_uses_sla_recommender`, `test_build_advisor_predicts_time_to_claim_from_scheduled_drivers`, `test_build_advisor_marks_scheduled_prediction_overloaded`, `test_build_advisor_overloaded_when_target_unreachable`, `test_build_advisor_suppresses_overloaded_when_calibration_maxed`, `test_build_advisor_reports_algo_and_user_recommendations`.

```python
def test_build_advisor_recommends_capacity_coverage(monkeypatch):
    """Recommendation = ceil(busiest-hour lambda / (throughput * utilization))."""
    from zira_dashboard import forklift_advisor as adv
    from zira_dashboard import forklift_demand as dem
    monkeypatch.setattr(adv, "_forecast",
                        lambda target_day, history_samples, coldstart_calls_per_day: dem.DemandForecast(
                            total_calls=500, by_hour={9: 78.0, 10: 40.0}, peak_hour=9,
                            peak_calls=78.0, basis="history", n_days=8))
    monkeypatch.setattr(adv, "_algo_throughput", lambda: 19.0)     # data-derived
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_claim_seconds",
                        lambda window_days=90: 250.0)
    monkeypatch.setattr(adv.app_settings, "get_setting", lambda k: [])
    out = adv.build_advisor(date(2026, 6, 30), scheduled=5, backups=0)
    # effective = 19 * 0.75 = 14.25 ; ceil(78/14.25) = 6
    assert out["available"] is True
    assert out["recommended"] == 6
    assert out["observed_claim_seconds"] == 250.0
    assert "overloaded" not in out
    assert "predicted_claim_seconds" not in out
    assert out["coverage"].status == "short"      # 6 needed, 5 scheduled
    assert out["live_model"]["available"] is True
    assert out["live_model"]["recommended"] == 6
    assert round(out["live_model"]["effective_throughput"], 2) == 14.25
    assert out["live_model"]["lambda_per_hr"] == 78.0


def test_build_advisor_no_observed_claim_is_none(monkeypatch):
    from zira_dashboard import forklift_advisor as adv
    from zira_dashboard import forklift_demand as dem
    monkeypatch.setattr(adv, "_forecast",
                        lambda target_day, history_samples, coldstart_calls_per_day: dem.DemandForecast(
                            total_calls=500, by_hour={9: 78.0}, peak_hour=9,
                            peak_calls=78.0, basis="history", n_days=8))
    monkeypatch.setattr(adv, "_algo_throughput", lambda: 19.0)
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_claim_seconds",
                        lambda window_days=90: None)
    monkeypatch.setattr(adv.app_settings, "get_setting", lambda k: [])
    out = adv.build_advisor(date(2026, 6, 30), scheduled=6, backups=0)
    assert out["recommended"] == 6
    assert out["observed_claim_seconds"] is None
    assert out["coverage"].status == "ok"
```

Keep the untouched tests: `test_build_advisor_falls_back_when_no_handle_time` → rename/adjust (see below), `test_build_advisor_with_history`, `test_cold_start_uses_today_shape_for_recommendation`, `test_cold_start_shape_never_produces_impossible_hours`, `test_cold_start_without_shape_suppresses_recommendation`, `test_build_advisor_no_data_returns_unavailable`, `test_build_advisor_unavailable_when_disabled`.

Update `test_build_advisor_with_history`: replace the SLA assertions

```python
    # busiest hour ~70 calls/hr @ 180s, target 240s, k=1 -> 5 drivers under target
    assert adv["recommended"] == 5
    assert adv["predicted_claim_seconds"] <= adv["target_seconds"] == 240.0
```

with (throughput default 16 in that test since `recent_driver_throughput` isn't patched → `_algo_throughput` returns `DEFAULT_THROUGHPUT` 16; effective = 16*0.75 = 12.0; ceil(70/12)=6):

```python
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_driver_throughput",
                        lambda days=28: None)   # -> DEFAULT_THROUGHPUT 16
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_claim_seconds",
                        lambda window_days=90: 250.0)
    # busiest hour 70/hr, effective 16*0.75=12 -> ceil(70/12) = 6
    assert adv["recommended"] == 6
    assert adv["observed_claim_seconds"] == 250.0
    assert adv["coverage"].status == "ok"      # 6 needed, 7 scheduled
```

Delete the obsolete `test_build_advisor_falls_back_when_no_handle_time` (there is no handle-time dependency anymore) and replace it with:

```python
def test_build_advisor_no_hourly_shape_suppresses_recommendation(monkeypatch):
    """A forecast with volume but no hourly shape yields no coverage number."""
    from zira_dashboard import forklift_advisor as adv
    from zira_dashboard import forklift_demand as dem
    monkeypatch.setattr(adv, "_forecast",
                        lambda target_day, history_samples, coldstart_calls_per_day: dem.DemandForecast(
                            total_calls=500, by_hour={}, peak_hour=None,
                            peak_calls=0.0, basis="bootstrap", n_days=0))
    monkeypatch.setattr(adv.app_settings, "get_setting", lambda k: [])
    out = adv.build_advisor(date(2026, 6, 30), scheduled=3, backups=2)
    assert out["available"] is True
    assert out["recommended"] is None
```

Also update the cold-start tests' patches: they patch `mean_handle_seconds` + `calibration_samples`, which no longer exist. Replace both patch lines in `test_cold_start_uses_today_shape_for_recommendation`, `test_cold_start_shape_never_produces_impossible_hours`, `test_build_advisor_with_history` with a single:

```python
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_claim_seconds",
                        lambda window_days=90: 250.0)
```

For the recommendation numbers in cold-start tests: throughput defaults to 16 (no `recent_driver_throughput` patch) → effective 12.0. `test_cold_start_uses_today_shape_for_recommendation` peak λ = 70 → ceil(70/12) = 6; change `assert adv["recommended"] == 5` to `== 6` and `coverage.status == "short"` stays (6 needed, 2 scheduled).

- [ ] **Step 2: Run tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_advisor.py -v`
Expected: FAIL — `build_advisor` still returns SLA keys / references removed helpers.

- [ ] **Step 3: Add the claim-time helper + window constant**

In `src/zira_dashboard/forklift_advisor.py`, near `_mean_handle_or_none` (which will be removed in Task 4), add:

```python
_CLAIM_WINDOW_DAYS = 90


def _observed_claim_or_none() -> float | None:
    """History-derived mean time-to-claim (seconds); None on no data / failure."""
    try:
        return forklift_store.recent_claim_seconds(_CLAIM_WINDOW_DAYS)
    except Exception:
        return None
```

- [ ] **Step 4: Rewrite `build_advisor`**

Replace the whole body of `build_advisor` (lines 180-279) with:

```python
def build_advisor(target_day: date, scheduled: int, backups: int) -> dict:
    cfg = _cfg()
    if not cfg.enabled:
        return {"available": False}

    algo_throughput = _algo_throughput()
    resolved = forklift_settings.resolve(cfg, algo_throughput=algo_throughput)

    forecast = _forecast(target_day, resolved.history_samples, cfg.coldstart_calls_per_day)
    if forecast.basis == "none" or forecast.total_calls <= 0:
        return {"available": False}

    backup_names = app_settings.get_setting("forklift_overload_responders") or []

    # sparkline data: list of (hour, fraction-of-peak) sorted by hour
    peak = forecast.peak_calls or 1.0
    hours = [(h, round(c / peak, 3)) for h, c in sorted(forecast.by_hour.items())]
    peak_label = (
        f"{forecast.peak_hour}:00–{forecast.peak_hour + 1}:00"
        if forecast.peak_hour is not None else "—"
    )

    # Capacity-coverage recommendation: smallest crew that can physically keep up
    # with the planned-hour call volume (throughput x utilization headroom).
    _, planned_lambda = forklift_demand.demand_at_percentile(
        forecast.by_hour, resolved.percentile)
    recommended = None
    if forecast.by_hour and planned_lambda > 0:
        recommended = forklift_demand.recommend_drivers(
            planned_lambda, resolved.effective_throughput)

    observed_claim = _observed_claim_or_none()
    coverage = (forklift_demand.assess_coverage(recommended, scheduled, backups)
                if recommended else None)

    return {
        "available": True,
        "day_label": target_day.strftime("%a %b %-d"),
        "total_calls": int(round(forecast.total_calls)),
        "peak_label": peak_label,
        "hours": hours,
        "basis": forecast.basis,
        "n_days": forecast.n_days,
        "backup_names": backup_names,
        "recommended": recommended,
        "observed_claim_seconds": observed_claim,
        "coverage": coverage,
        "live_model": {
            "available": recommended is not None,
            "recommended": recommended,
            "lambda_per_hr": planned_lambda,
            "effective_throughput": resolved.effective_throughput,
        },
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_advisor.py -k "build_advisor or cold_start" -v`
Expected: PASS. (`demand_summary` tests still fail — fixed in Task 3.)

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/forklift_advisor.py tests/test_forklift_advisor.py
git commit -m "feat(forklift): capacity-coverage recommendation in build_advisor"
```

---

## Task 3: Rewrite `demand_summary` (settings page)

**Files:**
- Modify: `src/zira_dashboard/forklift_advisor.py:292-377` (`demand_summary`)
- Modify: `src/zira_dashboard/forklift_advisor.py` `SLIDER_RANGES` (drop `target_minutes`)
- Test: `tests/test_forklift_advisor.py`

- [ ] **Step 1: Update the demand_summary tests**

In `tests/test_forklift_advisor.py`, replace the key-set assertion in `test_demand_summary_keys_and_recommendation`:

```python
    assert set(summary) == {
        "total_calls", "peak_calls", "peak_hour", "peak_label", "basis",
        "n_days", "recommended", "enabled", "overloaded",
        "target_seconds", "predicted_claim_seconds", "backtest", "target_minutes",
        "algo_recommended", "algo_values", "resolved_values", "overrides",
        "hour_values", "ranges",
    }
    # The retired capacity-ratio number must not surface.
    assert "effective_throughput" not in summary
```

with:

```python
    assert set(summary) == {
        "total_calls", "peak_calls", "peak_hour", "peak_label", "basis",
        "n_days", "recommended", "enabled", "observed_claim_seconds",
        "algo_recommended", "algo_values", "resolved_values", "overrides",
        "hour_values", "ranges",
    }
    assert "overloaded" not in summary
    assert "target_seconds" not in summary
    assert "predicted_claim_seconds" not in summary
```

Update the recommendation assertions in the same test. It patches `mean_handle_seconds`/`calibration_samples` (remove those) and asserts `recommended == 5`. Replace those lines:

```python
    monkeypatch.setattr("zira_dashboard.forklift_store.mean_handle_seconds",
                        lambda window_days=90: 180.0)
    monkeypatch.setattr("zira_dashboard.forklift_store.calibration_samples",
                        lambda window_days=90: [])
```

with:

```python
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_driver_throughput",
                        lambda days=28: None)   # -> DEFAULT_THROUGHPUT 16
    monkeypatch.setattr("zira_dashboard.forklift_store.recent_claim_seconds",
                        lambda window_days=90: 250.0)
```

and replace the block:

```python
    # SLA model: 70 calls/hr @ 180s handle, target 240s, k=1 -> 5 drivers.
    assert summary["recommended"] == 5
    assert summary["overloaded"] is False
    assert summary["target_seconds"] == 240.0
    assert summary["predicted_claim_seconds"] is not None
    assert summary["predicted_claim_seconds"] <= 240.0
    assert summary["backtest"]["uncalibrated"] is True
```

with (effective 16*0.75=12; ceil(70/12)=6):

```python
    assert summary["recommended"] == 6
    assert summary["observed_claim_seconds"] == 250.0
```

Update `test_demand_summary_carries_algo_and_overrides_and_hour_values`: it patches the two removed store fns and asserts `s["recommended"] == s["algo_recommended"] == 5`. Replace the two patch lines with the `recent_driver_throughput`/`recent_claim_seconds` pair above, and change `== 5` to `== 6`. Delete the line `assert s["target_seconds"] == 240.0`.

Update `test_demand_summary_no_signal_has_none_recommendation`: unchanged assertions still hold (`recommended is None`, `peak_calls == 0.0`, `peak_label == "—"`).

Remove `test_build_advisor_reports_algo_and_user_recommendations` (it is target-driven; superseded by capacity tests). If keeping the algo-vs-user idea, it is covered by `algo_recommended` in the demand-summary tests.

- [ ] **Step 2: Run tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_advisor.py -k demand_summary -v`
Expected: FAIL — `demand_summary` still returns the SLA key set.

- [ ] **Step 3: Rewrite `demand_summary` + trim `SLIDER_RANGES`**

In `SLIDER_RANGES`, delete the `target_minutes` entry (keep `throughput`, `utilization_pct`, `plan_for`, `history_samples`).

Replace the whole body of `demand_summary` (lines 292-377) with:

```python
def demand_summary(target_day: date) -> dict:
    """Read-only forecast + capacity summary for the Forklift settings page.
    Reuses the same _forecast + capacity recommender the scheduler card uses, so
    the settings page and the card never disagree. Never raises into the request
    path — returns a safe summary if anything fails.

    Carries the capacity recommendation (`recommended`), the algorithm baseline
    (`algo_recommended`, at the algorithm's own knob values), the observed recent
    claim time (measured outcome), the surviving knobs' algorithm ticks +
    overrides (throughput, utilization, plan-for, history; None = auto), the
    sorted per-hour call counts (JS live preview), and the slider ranges."""
    cfg = _cfg()
    algo_throughput = _algo_throughput()
    resolved = forklift_settings.resolve(cfg, algo_throughput=algo_throughput)
    algo = forklift_settings.algorithm_values(cfg, algo_throughput=algo_throughput)
    try:
        forecast = _forecast(target_day, resolved.history_samples,
                             cfg.coldstart_calls_per_day)
    except Exception:
        forecast = forklift_demand.DemandForecast()

    peak = float(forecast.peak_calls or 0.0)
    peak_label = (
        f"{forecast.peak_hour}:00–{forecast.peak_hour + 1}:00"
        if forecast.peak_hour is not None else "—"
    )
    hour_values = sorted(float(c) for c in forecast.by_hour.values())

    recommended = algo_recommended = None
    if forecast.by_hour:
        _, planned_lambda = forklift_demand.demand_at_percentile(
            forecast.by_hour, resolved.percentile)
        _, algo_lambda = forklift_demand.demand_at_percentile(
            forecast.by_hour, algo.percentile)
        if planned_lambda > 0:
            recommended = forklift_demand.recommend_drivers(
                planned_lambda, resolved.effective_throughput)
        if algo_lambda > 0:
            algo_recommended = forklift_demand.recommend_drivers(
                algo_lambda, algo.effective_throughput)

    return {
        "total_calls": int(round(forecast.total_calls)),
        "peak_calls": round(peak, 1),
        "peak_hour": forecast.peak_hour,
        "peak_label": peak_label,
        "basis": forecast.basis,
        "n_days": forecast.n_days,
        "recommended": recommended,
        "algo_recommended": algo_recommended,
        "observed_claim_seconds": _observed_claim_or_none(),
        "algo_values": _resolved_dict(algo),
        "resolved_values": _resolved_dict(resolved),
        "overrides": {
            "throughput": cfg.throughput_override,
            "utilization": cfg.utilization_override,
            "plan_for": cfg.plan_for_percentile_override,
            "history_samples": cfg.history_samples_override,
        },
        "hour_values": hour_values,
        "ranges": SLIDER_RANGES,
        "enabled": cfg.enabled,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_advisor.py -v`
Expected: PASS (all advisor tests).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/forklift_advisor.py tests/test_forklift_advisor.py
git commit -m "feat(forklift): capacity demand_summary for settings page"
```

---

## Task 4: Delete the Erlang/calibration subsystem

**Files:**
- Delete: `src/zira_dashboard/forklift_queue.py`, `tests/test_forklift_queue.py`
- Modify: `src/zira_dashboard/forklift_advisor.py` (remove dead helpers + import)
- Modify: `src/zira_dashboard/forklift_store.py` (remove `mean_handle_seconds`, `calibration_samples`, and `_MIN_DAY_CALLS` if unused)
- Test: `tests/test_forklift_store.py`, `tests/test_forklift_store_pure.py`

- [ ] **Step 1: Remove dead advisor helpers**

In `src/zira_dashboard/forklift_advisor.py` delete these now-unused functions and the `forklift_queue` import: `_mean_handle_or_none`, `_fit_calibration`, `_recommend_for_target`, `_guard_overload`, `_status_for_prediction`, `_scheduled_prediction`. Delete the `_CALIB_WINDOW_DAYS` constant. Remove `import ... forklift_queue` / `from . import forklift_queue` if present (check top of file).

- [ ] **Step 2: Delete the queue module + its tests**

```bash
git rm src/zira_dashboard/forklift_queue.py tests/test_forklift_queue.py
```

- [ ] **Step 3: Remove dead store helpers**

In `src/zira_dashboard/forklift_store.py` delete `mean_handle_seconds` and `calibration_samples`. Delete `_MIN_DAY_CALLS` and `_operating_hours` **only if** grep shows no remaining references (they are used only by `calibration_samples`). Verify:

Run: `grep -rn "mean_handle_seconds\|calibration_samples\|_operating_hours\|_MIN_DAY_CALLS" src/`
Expected: no matches after deletion.

Delete any tests in `tests/test_forklift_store.py` / `tests/test_forklift_store_pure.py` that call `mean_handle_seconds`, `calibration_samples`, or `_operating_hours`.

- [ ] **Step 4: Verify nothing imports the removed names**

Run: `grep -rn "forklift_queue\|erlang_c\|fit_calibration\|recommend_for_target" src/ tests/`
Expected: only matches inside `scripts/diagnose_forklift_overload.py` (fixed in Task 8).

- [ ] **Step 5: Run the forklift suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_advisor.py tests/test_forklift_store.py tests/test_forklift_store_pure.py tests/test_forklift_demand.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(forklift): delete Erlang-C queue + calibration subsystem"
```

---

## Task 5: Remove `target_claim_seconds` from settings

**Files:**
- Modify: `src/zira_dashboard/forklift_settings.py`
- Test: `tests/test_forklift_settings.py`

- [ ] **Step 1: Update settings tests**

In `tests/test_forklift_settings.py`, remove any assertion referencing `target_claim_seconds` or `DEFAULT_TARGET_CLAIM_SECONDS`. Add:

```python
def test_resolve_has_no_target_claim_field():
    from zira_dashboard import forklift_settings as fs
    r = fs.resolve(fs.Settings(), algo_throughput=19.0)
    assert not hasattr(r, "target_claim_seconds")
    # utilization headroom lever is present
    assert 0.0 < r.utilization <= 1.0
    assert r.effective_throughput == round(r.throughput * r.utilization, 10) or r.effective_throughput > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_settings.py -k target_claim -v`
Expected: FAIL — `Resolved` still has `target_claim_seconds`.

- [ ] **Step 3: Remove the field everywhere in `forklift_settings.py`**

- Delete `DEFAULT_TARGET_CLAIM_SECONDS = 240.0`.
- `Settings`: delete `target_claim_seconds: float | None = None`.
- `Resolved`: delete `target_claim_seconds: float = DEFAULT_TARGET_CLAIM_SECONDS`.
- `resolve()`: delete the `target_claim_seconds=_f(...)` line.
- `_row_to_settings()`: delete `target_claim_seconds=_f(row.get("target_claim_seconds"))`.
- `_load_from_db()`: remove `target_claim_seconds` from the SELECT column list.
- `save()`: remove `target_claim_seconds` from the INSERT column list, the `VALUES` placeholder count (drop one `%s`), the `ON CONFLICT DO UPDATE SET` clause, and the params tuple.

Leave the DB column in place (unused) — no migration.

- [ ] **Step 4: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_forklift_settings.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/forklift_settings.py tests/test_forklift_settings.py
git commit -m "refactor(forklift): drop target_claim_seconds setting"
```

---

## Task 6: Drop target parse from the settings route

**Files:**
- Modify: `src/zira_dashboard/routes/settings.py:764-797` (`_parse_forklift_overrides`)
- Test: `tests/test_settings_forklift.py`

- [ ] **Step 1: Update the route test**

In `tests/test_settings_forklift.py`, remove assertions that a posted `target_claim_seconds` round-trips. Add one asserting utilization round-trips:

```python
def test_parse_forklift_overrides_utilization_percent_to_fraction():
    from zira_dashboard.routes.settings import _parse_forklift_overrides
    s = _parse_forklift_overrides({"enabled": "1", "utilization_pct": "80"})
    assert s.utilization_override == 0.8
    # blank/auto -> None
    s2 = _parse_forklift_overrides({"enabled": "1", "utilization_pct": "auto"})
    assert s2.utilization_override is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_settings_forklift.py -k utilization -v`
Expected: PASS or FAIL depending on existing behavior; if the file's other tests reference `target_claim_seconds`, they FAIL to import/collect. Confirm the utilization test passes and target tests are removed.

- [ ] **Step 3: Remove the target parse**

In `_parse_forklift_overrides`, delete the two lines:

```python
    target_claim = _override("target_claim_seconds", 30.0, 1200.0,
                             integer=False, scale=60.0)
```

and the `target_claim_seconds=target_claim,` argument in the returned `Settings(...)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_settings_forklift.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/routes/settings.py tests/test_settings_forklift.py
git commit -m "refactor(forklift): drop target-claim parse from settings route"
```

---

## Task 7: Templates, JS, and route context

**Files:**
- Modify: `src/zira_dashboard/templates/staffing.html:286-307`
- Modify: `src/zira_dashboard/routes/staffing.py:1353-1363`
- Modify: `src/zira_dashboard/static/staffing.js:205-290,380`
- Modify: `src/zira_dashboard/templates/settings.html:806-985`
- Test: `tests/test_staffing_forklift_card.py`

- [ ] **Step 1: Update the staffing-card test**

In `tests/test_staffing_forklift_card.py`, replace assertions about "Overloaded" / "Predicted Time-to-Claim" with the coverage badge. Assert that for an advisor with `recommended=6, observed_claim_seconds=250.0, coverage.status="short"`, the rendered bay shows `6 suggested` and `4.2 min` (250/60) and does NOT contain `Overloaded` or `Predicted Time-to-Claim`. Follow the file's existing render harness (it constructs a `forklift_advisor` dict and renders the bay partial or the staffing template). Concretely, add/adjust:

```python
def test_forklift_bay_shows_coverage_and_observed_claim(render_bay):
    html = render_bay({
        "available": True, "recommended": 6, "observed_claim_seconds": 250.0,
        "coverage": type("C", (), {"status": "short", "gap": 2})(),
        "basis": "history", "n_days": 8, "peak_label": "12:00–13:00",
    })
    assert "6 suggested" in html.lower()
    assert "4.2 min" in html          # 250s observed
    assert "overloaded" not in html.lower()
    assert "predicted time-to-claim" not in html.lower()
```

If the existing file has no `render_bay` fixture, mirror its current rendering approach (it already renders this bay for the old assertions) and keep the same mechanism.

- [ ] **Step 2: Rewrite the staffing bay badge**

In `src/zira_dashboard/templates/staffing.html`, replace lines 289-306 (the `<div class="forklift-bay-summary ...">` block) with:

```html
                  <div class="forklift-bay-summary {{ _fk_status }}" title="{% if _fk_status == 'ok' %}Coverage satisfied{% elif forklift_advisor.coverage %}{{ forklift_advisor.coverage.gap }} driver{{ forklift_advisor.coverage.gap != 1 and 's' or '' }} short{% else %}Forklift coverage pending{% endif %}">
                    {% if forklift_advisor.recommended %}
                      <span class="forklift-bay-suggested">{{ forklift_advisor.recommended }} suggested</span>
                      {% if forklift_advisor.observed_claim_seconds is not none %}
                        <span class="forklift-bay-prediction">recent avg claim {{ '%.1f'|format(forklift_advisor.observed_claim_seconds / 60.0) }} min</span>
                      {% else %}
                        <span class="forklift-bay-prediction">claim time building</span>
                      {% endif %}
                    {% else %}
                      <span>coverage building</span>
                      <span class="forklift-bay-prediction">history accruing</span>
                    {% endif %}
                  </div>
```

Note `_fk_status` (line 287-288) still derives from `forklift_advisor.coverage.gap`; keep those two `{% set %}` lines unchanged (they no longer reference `scheduled_prediction_status`, which is gone — update line 288 to `{% set _fk_status = ('ok' if _fk_gap <= 0 else ('warn' if _fk_gap == 1 else 'danger')) %}`).

- [ ] **Step 3: Trim the live-model route context**

In `src/zira_dashboard/routes/staffing.py` (around 1356-1360), `forklift_advisor_model.get("live_model")` now yields `{available, recommended, lambda_per_hr, effective_throughput}`. Keep the `driver_wc_names` injection. No key removal needed here, but delete any downstream code referencing `mean_handle_seconds`/`calibration_k`/`target_seconds` on the live model if present (grep):

Run: `grep -n "calibration_k\|mean_handle_seconds\|target_seconds\|scheduled_prediction" src/zira_dashboard/routes/staffing.py`
Expected: no matches (remove any that appear).

- [ ] **Step 4: Simplify the JS (remove client Erlang-C)**

In `src/zira_dashboard/static/staffing.js`:
- Delete `erlangCWaitSeconds` (the `function erlangCWaitSeconds(...)` through its closing `}`, ~lines 205-234), `forkliftStatusForPrediction` (236-241).
- Replace `recalcForkliftBaySummary` (253-289) with a coverage-only version:

```javascript
  function recalcForkliftBaySummary() {
    const model = window.FORKLIFT_LIVE_MODEL || {};
    if (!model.available) return;
    const summary = document.querySelector('.forklift-bay-summary');
    if (!summary) return;
    const suggested = summary.querySelector('.forklift-bay-suggested');
    if (suggested && model.recommended) {
      suggested.textContent = model.recommended + ' suggested';
    }
    const scheduled = countScheduledForkliftDrivers(model);
    const gap = (model.recommended || 0) - scheduled;
    const status = gap <= 0 ? 'ok' : (gap === 1 ? 'warn' : 'danger');
    summary.classList.remove('ok', 'warn', 'danger');
    summary.classList.add(status);
    summary.title = gap <= 0
      ? 'Coverage satisfied'
      : gap + ' driver' + (gap === 1 ? '' : 's') + ' short';
  }
```

Keep `countScheduledForkliftDrivers` and the `recalcForkliftBaySummary()` call at line ~380. The observed-claim line is server-rendered and static — the JS no longer touches `.forklift-bay-prediction`.

- [ ] **Step 5: Rewrite the settings forklift section**

In `src/zira_dashboard/templates/settings.html`:
- Line 813: change the example copy from `"6 Suggested · Predicted Time-to-Claim 4.9 min"` to `"6 suggested (coverage) · recent avg claim 4.2 min"`.
- Lines 858-895 (the `_tmin` set, the `overloaded` block, the "Recommend N to keep time-to-claim under…" block, and the back-test block): replace with a measured-outcome line:

```html
        {% if forklift.recommended is not none %}
        <div style="color:#666;font-size:0.82rem;margin-top:0.4rem">
          Recommend <strong>{{ forklift.recommended }}</strong> for coverage of
          the busiest hour
          {% if forklift.observed_claim_seconds is not none %}
            &middot; recent avg time-to-claim ~{{ '%.1f'|format(forklift.observed_claim_seconds / 60.0) }} min (measured)
          {% endif %}
        </div>
        {% endif %}
        {% if forklift.recommended is not none %}
        <div style="color:#666;font-size:0.78rem;margin-top:0.3rem">
          Predicting ~{{ forklift.total_calls }} calls, busiest hour
          ~{{ '%.0f'|format(forklift.peak_calls) }}
          {% if forklift.basis == 'history' %}
            &middot; based on {{ forklift.n_days }} recent {{ forklift.weekday_label }}s
          {% else %}
            &middot; early estimate (history will sharpen this)
          {% endif %}
        </div>
        {% endif %}
```

- Lines 940-972 (the entire `fl-target` slider block, from the `{# Target time-to-claim slider… #}` comment through its closing `</div>`): delete it, and add a utilization slider + throughput slider before the `plan_for` slider:

```html
      {{ fl_slider('utilization_pct', 'Driver utilization',
            'Coverage headroom. Lower leaves more slack (more drivers); higher runs leaner.',
            forklift.resolved_values.utilization * 100, forklift.overrides.utilization,
            forklift.algo_values.utilization * 100, forklift.ranges.utilization_pct,
            fmt='%g', unit='%') }}

      {{ fl_slider('throughput', 'Driver throughput',
            'Calls per hour a driver handles (data-derived). Higher → fewer drivers.',
            forklift.resolved_values.throughput, forklift.overrides.throughput,
            forklift.algo_values.throughput, forklift.ranges.throughput,
            fmt='%g', unit=' /hr') }}
```

Note: the `utilization` override is a fraction; the slider works in PERCENT (range `utilization_pct` = 40-100). Passing `value = utilization * 100` and `override = cfg.utilization_override` (a fraction or None) means the readout shows the fraction when overridden. To keep it simple and correct, pass the override truthiness only for the auto state — the macro uses `override is none` for the "(auto)" readout, and shows `fmt|format(value)`; `value` is already the percent, so the readout is correct. The hidden input posts the percent, and `_parse_forklift_overrides` scales `0.01`. This round-trips.

- [ ] **Step 6: Render-check the settings + staffing pages**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_forklift_card.py tests/test_settings_forklift.py -q`
Expected: PASS.

Then verify no template references removed keys:

Run: `grep -rn "overloaded\|predicted_claim\|predicted_scheduled\|target_minutes\|target_seconds\|scheduled_prediction\|backtest\|calibration_k\|mean_handle" src/zira_dashboard/templates/ src/zira_dashboard/static/staffing.js`
Expected: no matches.

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/templates/staffing.html src/zira_dashboard/templates/settings.html src/zira_dashboard/static/staffing.js src/zira_dashboard/routes/staffing.py tests/test_staffing_forklift_card.py
git commit -m "feat(forklift): coverage badge + utilization control, drop SLA UI"
```

---

## Task 8: Diagnostic script + full verification

**Files:**
- Modify: `scripts/diagnose_forklift_overload.py`

- [ ] **Step 1: Retarget the diagnostic to the capacity model**

Rewrite `scripts/diagnose_forklift_overload.py` to drop the `forklift_queue` import and the Erlang per-crew table. Print: resolved throughput/utilization/effective, percentile, forecast (basis, peak, by_hour), `planned_lambda`, `recommend_drivers`, `observed_claim_seconds`, and the `build_advisor` result keys (`recommended`, `observed_claim_seconds`, `coverage`). Rename the file's docstring accordingly (keep the filename — it is referenced in memory).

```python
"""Read-only diagnostic: dump the inputs feeding the capacity-coverage forklift
recommendation. Run: railway run python -m scripts.diagnose_forklift_overload"""
from __future__ import annotations

from datetime import date

from zira_dashboard import (
    forklift_advisor, forklift_demand, forklift_settings,
)


def main() -> None:
    today = date.today()
    cfg = forklift_advisor._cfg()
    thr = forklift_advisor._algo_throughput()
    resolved = forklift_settings.resolve(cfg, algo_throughput=thr)
    forecast = forklift_advisor._forecast(
        today, resolved.history_samples, cfg.coldstart_calls_per_day)
    hour, lam = forklift_demand.demand_at_percentile(forecast.by_hour, resolved.percentile)

    print("throughput=%.2f  utilization=%.2f  effective=%.2f  percentile=%.2f"
          % (resolved.throughput, resolved.utilization,
             resolved.effective_throughput, resolved.percentile))
    print("forecast basis=%s n=%d peak=%s by_hour=%s"
          % (forecast.basis, forecast.n_days, forecast.peak_calls, forecast.by_hour))
    print("planned hour=%s lambda=%.1f" % (hour, lam or 0))
    if forecast.by_hour and (lam or 0) > 0:
        print("recommend_drivers = %d"
              % forklift_demand.recommend_drivers(lam, resolved.effective_throughput))
    print("observed_claim_seconds = %s" % forklift_advisor._observed_claim_or_none())

    rec = forklift_advisor.build_advisor(today, scheduled=4, backups=0)
    for k in ("available", "recommended", "observed_claim_seconds", "coverage"):
        print("  %s: %s" % (k, rec.get(k)))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: PASS except the known DB-env-gated pre-existing failures (`tests/test_inbox_event_wiring.py::test_late_declare_absent_records_inbox_event`, `tests/test_settings_api_keys.py::*`) which fail only when no test DB is configured. No forklift failures.

- [ ] **Step 3: Live read-only sanity (optional, post-merge)**

Run against the read-only proxy (DB password from `railway variables --service Postgres-rAoP --kv`):
`DATABASE_URL='<proxy-url>' ZIRA_API_KEY=test .venv/bin/python -m scripts.diagnose_forklift_overload`
Expected: `recommended` a small integer (≈6), `observed_claim_seconds` ≈ 250, no exceptions.

- [ ] **Step 4: Commit**

```bash
git add scripts/diagnose_forklift_overload.py
git commit -m "chore(forklift): retarget overload diagnostic to capacity model"
```

---

## Self-review notes (addressed)

- **Spec coverage:** §1 recommendation → Tasks 2/3; §2 observed claim → Tasks 1/2/3/7; §3 coverage badge → Task 7; §4 retire subsystem → Tasks 3/4/5/6/7; data flow → Tasks 2/3; testing → every task; rollout/diagnostic → Task 8.
- **Type consistency:** `recommend_drivers(peak_calls, throughput_per_hour)` and `assess_coverage(recommended, scheduled, backups)` match `forklift_demand`'s existing signatures; `effective_throughput` is `Resolved.effective_throughput`; `observed_claim_seconds` is the single new advisor key used identically in build_advisor, demand_summary, templates, and tests.
- **Non-destructive DB:** `target_claim_seconds` column is left in place; `save()`/`_load_from_db()` simply stop referencing it.
