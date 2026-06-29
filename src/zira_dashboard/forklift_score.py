"""Pure composite forklift GOAT score: a weighted 0-100 blend of four
absolute-target-normalized components. No DB, no templates."""
from __future__ import annotations

from dataclasses import dataclass, field

_DEFAULT_WEIGHTS = {"calls": 40.0, "ontime": 30.0, "speed": 20.0, "util": 10.0}


@dataclass(frozen=True)
class ScoreConfig:
    weights: dict = field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    target_calls: float = 25.0
    ontime_floor: float = 80.0
    fast_secs: float = 30.0
    slow_secs: float = 180.0
    min_calls: int = 8


DEFAULT_SCORE_CONFIG = ScoreConfig()


@dataclass(frozen=True)
class ScoreBreakdown:
    score: float
    components: dict  # {key: {"sub": float, "points": float}}


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _subscores(row: dict, cfg: ScoreConfig) -> dict:
    calls = float(row.get("calls") or 0)
    on_time = float(row.get("on_time") or 0)
    late = float(row.get("late") or 0)
    avg_secs = float(row.get("avg_ms") or 0) / 1000.0
    util = float(row.get("utilization_pct") or 0)

    s_calls = _clamp(calls / cfg.target_calls * 100) if cfg.target_calls else 0.0
    denom = on_time + late
    pct = (on_time / denom * 100) if denom else 0.0
    spread = (100.0 - cfg.ontime_floor) or 1.0
    s_ontime = _clamp((pct - cfg.ontime_floor) / spread * 100)
    span = (cfg.slow_secs - cfg.fast_secs) or 1.0
    s_speed = _clamp((cfg.slow_secs - avg_secs) / span * 100)
    s_util = _clamp(util)
    return {"calls": s_calls, "ontime": s_ontime, "speed": s_speed, "util": s_util}


def daily_score(row: dict, cfg: ScoreConfig = DEFAULT_SCORE_CONFIG) -> ScoreBreakdown | None:
    if float(row.get("calls") or 0) < cfg.min_calls:
        return None
    subs = _subscores(row, cfg)
    w = {k: float(cfg.weights.get(k, 0) or 0) for k in subs}
    total_w = sum(w.values())
    if total_w <= 0:  # zero weights -> equal weighting
        w = {k: 1.0 for k in subs}
        total_w = float(len(subs))
    components, score = {}, 0.0
    for k, sub in subs.items():
        pts = w[k] / total_w * sub
        components[k] = {"sub": sub, "points": pts}
        score += pts
    return ScoreBreakdown(score=score, components=components)
