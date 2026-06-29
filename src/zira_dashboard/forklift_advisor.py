"""Assemble the forklift advisor render model for the scheduler card.

Reads accumulated same-weekday snapshots, predicts demand, sizes drivers, and
assesses coverage against how many people are scheduled on the Tablets work
center (the queue drivers) — passed in by the caller. Returns a dict with
available=False when there is no signal so the template degrades quietly.
"""
from __future__ import annotations

from datetime import date

from . import (
    app_settings,
    forklift_demand,
    forklift_queue,
    forklift_settings,
    forklift_store,
)

# Window (days) over which handling time + calibration samples are read.
_CALIB_WINDOW_DAYS = 90


def _cfg() -> "forklift_settings.Settings":
    """Current forklift settings, falling back to DEFAULT if the store can't be
    read (no DB in unit tests, transient failure, etc.). Never raises."""
    try:
        return forklift_settings.current()
    except Exception:
        return forklift_settings.DEFAULT


def _weekly_trends_or_none() -> dict | None:
    """Best-effort cold-start source; never raises into the request path."""
    try:
        from . import forklift_client
        return forklift_client.fetch_weekly_trends()
    except Exception:
        return None


def _today_hourly_shape_or_none() -> list | None:
    """Today's dashboard hourly shape (hourlyClaimAvgs); None on any error."""
    try:
        from . import forklift_client
        dash = forklift_client.fetch_dashboard()
        return (dash or {}).get("hourlyClaimAvgs") or None
    except Exception:
        return None


# Slider ranges surfaced to the settings page (min/max/step per knob). The JS
# live preview and the POST parser clamp to these same bounds.
SLIDER_RANGES = {
    "throughput": {"min": 5, "max": 30, "step": 1},
    "utilization_pct": {"min": 40, "max": 100, "step": 1},
    "plan_for": {"min": 0.5, "max": 1.0, "step": 0.05},
    "history_samples": {"min": 2, "max": 20, "step": 1},
    # Target time-to-claim slider works in MINUTES (1-10, half-min steps).
    "target_minutes": {"min": 1, "max": 10, "step": 0.5},
}


def _algo_throughput() -> float:
    """Data-derived per-driver throughput, falling back to the default when
    there's no usable data (or the read fails). Never raises."""
    try:
        rate = forklift_store.recent_driver_throughput()
    except Exception:
        rate = None
    return rate or forklift_settings.DEFAULT_THROUGHPUT


def _forecast(target_day: date, history_samples: int,
              coldstart_calls_per_day: float) -> "forklift_demand.DemandForecast":
    """Build the demand forecast for `target_day`: same-weekday history first,
    then a cold-start fallback (manual daily volume if configured, else weekly
    trends), distributed across today's hourly shape. All I/O is wrapped so this
    never raises into the request path."""
    weekday = target_day.weekday()  # Mon=0
    snaps = []
    try:
        snaps = forklift_store.calls_daily_for_weekday(weekday, limit=history_samples)
    except Exception:
        snaps = []

    forecast = forklift_demand.predict_from_history(snaps)
    if forecast.basis == "none":
        shape = _today_hourly_shape_or_none()
        if coldstart_calls_per_day > 0:
            # Manual cold-start: assume this daily volume, shaped by today's hours.
            forecast = forklift_demand.forecast_from_total_and_shape(
                coldstart_calls_per_day, shape or [])
        else:
            trends = _weekly_trends_or_none()
            if trends:
                base = forklift_demand.bootstrap_from_trends(trends)
                if base.total_calls > 0:
                    forecast = forklift_demand.forecast_from_total_and_shape(
                        base.total_calls, shape or [])
    return forecast


def _mean_handle_or_none() -> float | None:
    """History-derived mean handling time (seconds); None on no data / failure."""
    try:
        return forklift_store.mean_handle_seconds(_CALIB_WINDOW_DAYS)
    except Exception:
        return None


def _fit_calibration(mean_handle: float) -> "forklift_queue.CalibResult":
    """Calibrate the queue model against actual recorded waits. Defensive: a
    store-read failure yields an uncalibrated (k=1.0) result, never raises."""
    try:
        samples = forklift_store.calibration_samples(_CALIB_WINDOW_DAYS)
    except Exception:
        samples = []
    return forklift_queue.fit_calibration(samples, mean_handle)


def _recommend_for_target(forecast: "forklift_demand.DemandForecast",
                          params: "forklift_settings.Resolved",
                          mean_handle: float, k: float,
                          target_seconds: float) -> "forklift_queue.RecResult":
    """SLA recommendation: smallest crew whose calibrated predicted time-to-claim
    stays under `target_seconds`, sized to the chosen percentile's hour."""
    _, lam = forklift_demand.demand_at_percentile(forecast.by_hour, params.percentile)
    return forklift_queue.recommend_for_target(lam, mean_handle, target_seconds, k)


def build_advisor(target_day: date, scheduled: int, backups: int) -> dict:
    cfg = _cfg()
    if not cfg.enabled:
        return {"available": False}

    algo_throughput = _algo_throughput()
    resolved = forklift_settings.resolve(cfg, algo_throughput=algo_throughput)

    # v1 simplification: build a single forecast from the *resolved* history
    # window and size both recommendations from it. The window only affects
    # demand smoothing (second-order for the baseline display), so reusing the
    # same forecast is acceptable and keeps this cheap.
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

    base = {
        "available": True,
        "day_label": target_day.strftime("%a %b %-d"),
        "total_calls": int(round(forecast.total_calls)),
        "peak_label": peak_label,
        "hours": hours,
        "coverage": None,
        "basis": forecast.basis,
        "n_days": forecast.n_days,
        "backup_names": backup_names,
        # SLA fields (default to the no-data / cold-start shape; filled below).
        "recommended": None,
        "algo_recommended": None,
        "overloaded": False,
        "predicted_claim_seconds": None,
        "target_seconds": resolved.target_claim_seconds,
        "backtest": None,
    }

    # SLA recommendation: needs handling time + a forecast with hourly shape.
    mean_handle = _mean_handle_or_none()
    if mean_handle is None or not forecast.by_hour:
        # No signal yet for the queue model: degrade quietly (recommendation
        # "builds as history accrues"), but keep the demand summary above.
        return base

    calib = _fit_calibration(mean_handle)
    rec = _recommend_for_target(forecast, resolved, mean_handle, calib.k,
                                resolved.target_claim_seconds)
    # Algorithm baseline = same calc at the DEFAULT target (the discreet tick).
    algo_rec = _recommend_for_target(
        forecast, resolved, mean_handle, calib.k,
        forklift_settings.DEFAULT_TARGET_CLAIM_SECONDS)

    coverage = (forklift_demand.assess_coverage(rec.drivers, scheduled, backups)
                if rec.drivers else None)

    base.update({
        "recommended": rec.drivers,
        "algo_recommended": algo_rec.drivers,
        "overloaded": rec.overloaded,
        "predicted_claim_seconds": rec.predicted_seconds,
        "target_seconds": resolved.target_claim_seconds,
        "coverage": coverage,
        "backtest": {
            "n_samples": calib.n_samples,
            "mean_actual_seconds": calib.mean_actual_seconds,
            "mean_pred_seconds": calib.mean_pred_seconds,
            "uncalibrated": calib.uncalibrated,
        },
    })
    return base


def _resolved_dict(r: "forklift_settings.Resolved") -> dict:
    return {
        "throughput": r.throughput,
        "utilization": r.utilization,
        "percentile": r.percentile,
        "history_samples": r.history_samples,
        "effective_throughput": round(r.effective_throughput, 2),
    }


def demand_summary(target_day: date) -> dict:
    """Read-only forecast + SLA summary for the Forklift settings page. Reuses the
    same _forecast + SLA recommender the scheduler card uses, so the settings page
    and the card never disagree. Never raises into the request path — returns a
    safe summary if anything fails.

    Carries the SLA recommendation (smallest crew under the time-to-claim target):
    `recommended` / `target_seconds` / `predicted_claim_seconds` / `overloaded`
    plus the `backtest`, the algorithm baseline (the same calc at the DEFAULT
    target), the surviving knobs' algorithm ticks + overrides (plan-for, history;
    None = auto), the sorted per-hour call counts (JS live preview), and the
    slider ranges. The OLD user-facing capacity number is intentionally gone."""
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

    # SLA recommendation (same model as the scheduler card). Needs handling time
    # + a forecast with hourly shape; otherwise the recommendation degrades to
    # None ("builds as history accrues") while the demand summary still renders.
    recommended = algo_recommended = predicted = None
    overloaded = False
    backtest = None
    target_seconds = resolved.target_claim_seconds
    mean_handle = _mean_handle_or_none()
    if mean_handle is not None and forecast.by_hour:
        calib = _fit_calibration(mean_handle)
        rec = _recommend_for_target(forecast, resolved, mean_handle, calib.k,
                                    resolved.target_claim_seconds)
        algo_rec = _recommend_for_target(
            forecast, resolved, mean_handle, calib.k,
            forklift_settings.DEFAULT_TARGET_CLAIM_SECONDS)
        recommended = rec.drivers
        predicted = rec.predicted_seconds
        overloaded = rec.overloaded
        algo_recommended = algo_rec.drivers
        backtest = {
            "n_samples": calib.n_samples,
            "mean_actual_seconds": calib.mean_actual_seconds,
            "mean_pred_seconds": calib.mean_pred_seconds,
            "uncalibrated": calib.uncalibrated,
        }

    return {
        "total_calls": int(round(forecast.total_calls)),
        "peak_calls": round(peak, 1),
        "peak_hour": forecast.peak_hour,
        "peak_label": peak_label,
        "basis": forecast.basis,
        "n_days": forecast.n_days,
        "recommended": recommended,
        "algo_recommended": algo_recommended,
        "overloaded": overloaded,
        "target_seconds": target_seconds,
        "predicted_claim_seconds": predicted,
        "backtest": backtest,
        "algo_values": _resolved_dict(algo),
        "resolved_values": _resolved_dict(resolved),
        "overrides": {
            "throughput": cfg.throughput_override,
            "utilization": cfg.utilization_override,
            "plan_for": cfg.plan_for_percentile_override,
            "history_samples": cfg.history_samples_override,
            # Target time-to-claim override (None = auto / the 240s default). The
            # settings slider works in MINUTES, so carry both the override flag
            # and the resolved value in minutes.
            "target": cfg.target_claim_seconds,
        },
        "target_minutes": round(target_seconds / 60.0, 2),
        "hour_values": hour_values,
        "ranges": SLIDER_RANGES,
        "enabled": cfg.enabled,
    }
