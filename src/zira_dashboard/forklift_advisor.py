"""Assemble the forklift advisor render model for the scheduler card.

Reads accumulated same-weekday snapshots, predicts demand, sizes drivers, and
assesses coverage against how many people are scheduled on the Tablets work
center (the queue drivers) — passed in by the caller. Returns a dict with
available=False when there is no signal so the template degrades quietly.
"""
from __future__ import annotations

from datetime import date

from . import app_settings, forklift_demand, forklift_store


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


def build_advisor(target_day: date, scheduled: int, backups: int) -> dict:
    weekday = target_day.weekday()  # Mon=0
    snaps = []
    try:
        snaps = forklift_store.calls_daily_for_weekday(weekday, limit=8)
    except Exception:
        snaps = []

    forecast = forklift_demand.predict_from_history(snaps)
    if forecast.basis == "none":
        trends = _weekly_trends_or_none()
        if trends:
            base = forklift_demand.bootstrap_from_trends(trends)
            if base.total_calls > 0:
                shape = _today_hourly_shape_or_none()
                forecast = forklift_demand.forecast_from_total_and_shape(
                    base.total_calls, shape or [])

    if forecast.basis == "none" or forecast.total_calls <= 0:
        return {"available": False}

    if forecast.peak_calls > 0:
        recommended = forklift_demand.recommend_drivers(forecast.peak_calls)
        coverage = forklift_demand.assess_coverage(recommended, scheduled, backups)
    else:
        recommended = None
        coverage = None
    backup_names = app_settings.get_setting("forklift_overload_responders") or []

    # sparkline data: list of (hour, fraction-of-peak) sorted by hour
    peak = forecast.peak_calls or 1.0
    hours = [(h, round(c / peak, 3)) for h, c in sorted(forecast.by_hour.items())]
    peak_label = (
        f"{forecast.peak_hour}:00–{forecast.peak_hour + 1}:00"
        if forecast.peak_hour is not None else "—"
    )

    return {
        "available": True,
        "day_label": target_day.strftime("%a %b %-d"),
        "total_calls": int(round(forecast.total_calls)),
        "peak_label": peak_label,
        "hours": hours,
        "recommended": recommended,
        "coverage": coverage,
        "basis": forecast.basis,
        "n_days": forecast.n_days,
        "backup_names": backup_names,
    }
