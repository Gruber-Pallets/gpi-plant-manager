"""Pure forklift demand prediction + driver recommendation + coverage check.

v1 model (calibrated as history accumulates):
  - predict next working day from recent same-weekday snapshots (median total,
    mean per-hour shape); fall back to bootstrapping from the app's weekly
    trends when there is no same-weekday history yet.
  - recommend = ceil(busiest-hour calls / per-driver hourly throughput), min 1.
  - coverage compares the recommendation to drivers scheduled on Tablets.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median

# Effective per-driver throughput (calls/hour) used to size dedicated drivers.
# Measured flat-out rate is ~16-17 calls/hr/driver, but at that pace the queue
# backs up (avg ~7 min wait to claim, overload hours) -- overload sets in around
# ~70% sustained utilization, and demand arrives in sub-hour bursts. So the
# EFFECTIVE rate that keeps the busiest hour out of overload is lower; calibrated
# to ~10/hr against live data + operational reality (4 dedicated being the
# realistic floor). TODO: derive per-driver from forklift_driver_daily and
# expose as a setting.
DEFAULT_THROUGHPUT_PER_HOUR = 10.0


@dataclass
class DemandForecast:
    total_calls: float = 0.0
    by_hour: dict[int, float] = field(default_factory=dict)
    peak_hour: int | None = None
    peak_calls: float = 0.0
    basis: str = "none"          # 'history' | 'bootstrap' | 'none'
    n_days: int = 0


@dataclass
class Coverage:
    status: str                   # 'ok' | 'short'
    recommended: int
    scheduled: int                # people scheduled on the Tablets work center
    backups: int
    gap: int


def predict_from_history(snapshots: list[dict]) -> DemandForecast:
    if not snapshots:
        return DemandForecast()
    totals = [float(s.get("total_calls") or 0) for s in snapshots]
    # mean calls per hour-slot across snapshots
    sums: dict[int, float] = {}
    for s in snapshots:
        for slot, payload in (s.get("by_hour") or {}).items():
            hour = int(slot)
            calls = float((payload or {}).get("calls") or 0)
            sums[hour] = sums.get(hour, 0.0) + calls
    by_hour = {h: round(v / len(snapshots), 1) for h, v in sums.items()}
    peak_hour = max(by_hour, key=by_hour.get) if by_hour else None
    peak_calls = by_hour[peak_hour] if peak_hour is not None else 0.0
    return DemandForecast(
        total_calls=median(totals), by_hour=by_hour,
        peak_hour=peak_hour, peak_calls=peak_calls,
        basis="history", n_days=len(snapshots),
    )


def demand_at_percentile(by_hour: dict[int, float], pct: float) -> tuple[int | None, float]:
    """Per-hour demand at percentile `pct` of the day's hourly call counts.
    pct=1.0 -> busiest hour; 0.5 -> median hour; 0.0 -> quietest. Nearest-rank.
    Returns (hour, calls); (None, 0.0) when there's no data."""
    items = sorted(by_hour.items(), key=lambda kv: (kv[1], kv[0]))
    if not items:
        return (None, 0.0)
    pct = max(0.0, min(1.0, pct))
    idx = round(pct * (len(items) - 1))
    hour, calls = items[idx]
    return (hour, float(calls))


def bootstrap_from_trends(weekly_trends: dict, operating_days: int = 5) -> DemandForecast:
    weeks = (weekly_trends or {}).get("weeks") or []
    claimed = [float(w.get("claimedCalls") or 0) for w in weeks if w.get("claimedCalls")]
    if not claimed or operating_days <= 0:
        return DemandForecast()
    per_day = (sum(claimed) / len(claimed)) / operating_days
    return DemandForecast(total_calls=round(per_day, 1), basis="bootstrap", n_days=0)


def forecast_from_total_and_shape(total_calls: float, hourly_slots: list[dict]) -> DemandForecast:
    """Cold-start forecast: daily VOLUME from weekly trends, hourly SHAPE from
    today's dashboard hourlyClaimAvgs (each {slot, calls}). Distributes the
    volume across the observed shape so peak_hour/peak_calls are meaningful on
    day one. Returns a shapeless bootstrap forecast (peak_calls=0) when there is
    no usable shape, so callers can suppress the recommendation."""
    slots = []
    for s in hourly_slots or []:
        if s.get("slot") is None:
            continue
        slots.append((int(s["slot"]), float(s.get("calls") or 0)))
    shape_total = sum(c for _, c in slots)
    if total_calls <= 0 or shape_total <= 0:
        return DemandForecast(total_calls=round(float(total_calls), 1), basis="bootstrap")
    by_hour = {h: round(total_calls * (c / shape_total), 1) for h, c in slots}
    peak_hour = max(by_hour, key=by_hour.get)
    return DemandForecast(
        total_calls=round(float(total_calls), 1), by_hour=by_hour,
        peak_hour=peak_hour, peak_calls=by_hour[peak_hour],
        basis="bootstrap", n_days=0,
    )


def recommend_drivers(peak_calls: float, throughput_per_hour: float = DEFAULT_THROUGHPUT_PER_HOUR) -> int:
    if throughput_per_hour <= 0:
        return 1
    return max(1, math.ceil(peak_calls / throughput_per_hour))


def assess_coverage(recommended: int, scheduled: int, backups: int) -> Coverage:
    """Compare the recommended driver count to how many people are actually
    scheduled on the Tablets work center (NOT how many are merely certified)."""
    gap = max(0, recommended - scheduled)
    return Coverage(
        status="ok" if gap == 0 else "short",
        recommended=recommended, scheduled=scheduled,
        backups=backups, gap=gap,
    )
