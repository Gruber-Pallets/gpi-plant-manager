"""Forklift driver awards over forklift_driver_daily, scored by forklift_score.
Mirrors awards.py: derived live, 5-minute in-process TTL cache, defensive."""
from __future__ import annotations

import datetime as dt
import logging
import time

from zira_dashboard import forklift_score as fs
from zira_dashboard import forklift_store

_log = logging.getLogger(__name__)

ALLTIME_FLOOR = dt.date(2024, 1, 1)
_CACHE: dict = {}
_TTL = 300.0


def _cache(key, fn, default=None):
    """Memoize fn() for _TTL seconds. Defensive: any failure inside fn logs
    and yields `default` (an empty award) so render paths never 500."""
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    try:
        val = fn()
    except Exception as exc:  # noqa: BLE001 - never raise into a request path
        _log.warning("forklift awards: %s failed: %s", key, exc)
        return default
    _CACHE[key] = (now, val)
    return val


def invalidate():
    _CACHE.clear()


def _cfg_fp(cfg: fs.ScoreConfig):
    return (tuple(sorted(cfg.weights.items())), cfg.target_calls, cfg.ontime_floor,
            cfg.fast_secs, cfg.slow_secs, cfg.min_calls)


def driver_days(start: dt.date, end: dt.date) -> list[dict]:
    """Per-driver per-day rows in [start, end]. Real impl reads the store;
    tests monkeypatch this seam."""
    return forklift_store.driver_days_between(start, end)


def _scored_days(start, end, cfg):
    out = []
    for r in driver_days(start, end):
        b = fs.daily_score(r, cfg)
        if b is None:
            continue
        out.append({"name": r["name"], "driver_id": r["driver_id"],
                    "day": r["day"], "score": b.score, "calls": r["calls"],
                    "breakdown": b})
    return out


def goat(cfg: fs.ScoreConfig = fs.DEFAULT_SCORE_CONFIG):
    def _f():
        days = _scored_days(ALLTIME_FLOOR, dt.date.today(), cfg)
        if not days:
            return None
        days.sort(key=lambda d: (-d["score"], d["day"], d["name"]))
        return days[0]
    return _cache(("goat", _cfg_fp(cfg)), _f)


def annual_top_days(year: int, cfg: fs.ScoreConfig = fs.DEFAULT_SCORE_CONFIG, n: int = 3):
    def _f():
        days = _scored_days(dt.date(year, 1, 1), dt.date(year, 12, 31), cfg)
        days.sort(key=lambda d: (-d["score"], d["day"], d["name"]))
        return days[:n]
    return _cache(("annual_top", year, _cfg_fp(cfg)), _f, default=[])


def monthly_badges(year: int, month: int, cfg: fs.ScoreConfig = fs.DEFAULT_SCORE_CONFIG, n: int = 3):
    def _f():
        start = dt.date(year, month, 1)
        end = (dt.date(year + (month == 12), (month % 12) + 1, 1) - dt.timedelta(days=1))
        days = _scored_days(start, end, cfg)
        days.sort(key=lambda d: (-d["score"], d["day"], d["name"]))
        return days[:n]
    return _cache(("monthly", year, month, _cfg_fp(cfg)), _f, default=[])


def _ontime_pct(r):
    denom = (r["on_time"] or 0) + (r["late"] or 0)
    return (r["on_time"] / denom * 100) if denom else 0.0


def annual_best_ontime(year: int, min_calls: int = 50):
    def _f():
        agg = _aggregate_year(year)
        elig = [a for a in agg if a["calls"] >= min_calls]
        if not elig:
            return None
        elig.sort(key=lambda a: (-a["ontime_pct"], -a["calls"], a["name"]))
        return elig[0]
    return _cache(("best_ontime", year, min_calls), _f)


def annual_fastest(year: int, min_calls: int = 50):
    def _f():
        agg = _aggregate_year(year)
        elig = [a for a in agg if a["calls"] >= min_calls]
        if not elig:
            return None
        elig.sort(key=lambda a: (a["avg_ms"], -a["calls"], a["name"]))
        return elig[0]
    return _cache(("fastest", year, min_calls), _f)


def _aggregate_year(year: int) -> list[dict]:
    rows = driver_days(dt.date(year, 1, 1), dt.date(year, 12, 31))
    by_driver: dict = {}
    for r in rows:
        a = by_driver.setdefault(r["driver_id"], {
            "name": r["name"], "driver_id": r["driver_id"], "calls": 0,
            "on_time": 0, "late": 0, "ms_weighted": 0})
        a["calls"] += r["calls"]
        a["on_time"] += r["on_time"] or 0
        a["late"] += r["late"] or 0
        a["ms_weighted"] += (r["avg_ms"] or 0) * r["calls"]
    out = []
    for a in by_driver.values():
        a["ontime_pct"] = (a["on_time"] / (a["on_time"] + a["late"]) * 100) if (a["on_time"] + a["late"]) else 0.0
        a["avg_ms"] = (a["ms_weighted"] / a["calls"]) if a["calls"] else 0
        out.append(a)
    return out


def leaderboard(start: dt.date, end: dt.date,
                cfg: fs.ScoreConfig = fs.DEFAULT_SCORE_CONFIG,
                min_calls: int = 50) -> dict:
    rows = driver_days(start, end)
    by_driver: dict = {}
    for r in rows:
        a = by_driver.setdefault(r["driver_id"], {
            "name": r["name"], "driver_id": r["driver_id"], "calls": 0,
            "on_time": 0, "late": 0, "ms_weighted": 0,
            "score_sum": 0.0, "score_days": 0})
        a["calls"] += r["calls"]
        a["on_time"] += r["on_time"] or 0
        a["late"] += r["late"] or 0
        a["ms_weighted"] += (r["avg_ms"] or 0) * r["calls"]
        b = fs.daily_score(r, cfg)
        if b is not None:
            a["score_sum"] += b.score
            a["score_days"] += 1
    drivers = list(by_driver.values())
    for a in drivers:
        a["ontime_pct"] = (a["on_time"] / (a["on_time"] + a["late"]) * 100) if (a["on_time"] + a["late"]) else 0.0
        a["avg_ms"] = (a["ms_weighted"] / a["calls"]) if a["calls"] else 0

    most_calls = sorted(drivers, key=lambda a: (-a["calls"], a["name"]))
    rate = [a for a in drivers if a["calls"] >= min_calls]
    on_time = sorted(rate, key=lambda a: (-a["ontime_pct"], -a["calls"], a["name"]))
    fastest = sorted(rate, key=lambda a: (a["avg_ms"], -a["calls"], a["name"]))
    overall = sorted(
        ({"name": a["name"], "driver_id": a["driver_id"],
          "score": a["score_sum"] / a["score_days"], "days": a["score_days"],
          "calls": a["calls"]}
         for a in drivers if a["score_days"] > 0),
        key=lambda a: (-a["score"], a["name"]))
    return {"most_calls": most_calls, "on_time": on_time,
            "fastest": fastest, "overall": overall}


FORKLIFT_SCOPES = ("forklift_goat", "forklift_top_day",
                   "forklift_best_ontime", "forklift_fastest", "forklift_badge")


def awards_earned_by_driver(name: str, today: dt.date,
                            cfg: fs.ScoreConfig = fs.DEFAULT_SCORE_CONFIG) -> list[dict]:
    """Reverse lookup for the player card: every forklift award `name`
    currently holds, parallel to awards.awards_earned_by. Defensive — never
    raises (the underlying award computations swallow data errors)."""
    earned: list[dict] = []
    g = goat(cfg)
    if g and g["name"] == name:
        earned.append({"type": "forklift_goat", "name": name,
                       "score": g["score"], "day": g["day"]})
    for yr in (today.year, today.year - 1):
        for i, t in enumerate(annual_top_days(yr, cfg)):
            if t["name"] == name:
                earned.append({"type": "forklift_top_day", "name": name,
                               "year": yr, "position": i + 1,
                               "score": t["score"], "day": t["day"]})
        bo = annual_best_ontime(yr)
        if bo and bo["name"] == name:
            earned.append({"type": "forklift_best_ontime", "name": name,
                           "year": yr, "value": bo["ontime_pct"]})
        ff = annual_fastest(yr)
        if ff and ff["name"] == name:
            earned.append({"type": "forklift_fastest", "name": name,
                           "year": yr, "value": ff["avg_ms"]})
        for m in range(1, 13):
            for i, b in enumerate(monthly_badges(yr, m, cfg)):
                if b["name"] == name:
                    earned.append({"type": "forklift_badge", "name": name,
                                   "year": yr, "month": m, "position": i + 1,
                                   "score": b["score"], "day": b["day"]})
    return _apply_overrides(earned)


def _apply_overrides(items):
    # Delegate to awards.apply_forklift_overrides so manual replace/delete/reset
    # behaves identically to production awards. Tests monkeypatch this seam.
    from zira_dashboard import awards
    return awards.apply_forklift_overrides(items)
