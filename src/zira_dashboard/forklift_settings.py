"""Forklift demand-advisor settings. Each tunable is a NULLABLE OVERRIDE:
NULL = "auto" (follow the algorithm's own value). Singleton row (id=1), cached
in process, invalidated on save() — same pattern as auto_lunch_settings.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._singleton import CachedSingleton

if TYPE_CHECKING:
    from zira_dashboard import forklift_score as fs

# The algorithm's own default values for the policy knobs (driver throughput is
# data-derived elsewhere and passed in). These are the grey "tick" values.
DEFAULT_UTILIZATION = 0.65
DEFAULT_PLAN_FOR_PERCENTILE = 1.0   # 1.0 = busiest hour; lower = more typical
DEFAULT_HISTORY_SAMPLES = 8
DEFAULT_THROUGHPUT = 16.0           # fallback when no data-derived rate yet
DEFAULT_TARGET_CLAIM_SECONDS = 240.0   # SLA recommender target = 4 min


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    throughput_override: float | None = None
    utilization_override: float | None = None
    plan_for_percentile_override: float | None = None
    history_samples_override: int | None = None
    target_claim_seconds: float | None = None   # NULL = auto (240s / 4 min)
    include_loading_jockeying: bool = False
    coldstart_calls_per_day: float = 0.0
    # GOAT composite-score overrides (NULL = auto / forklift_score default).
    # Weights stored raw (renormalized at compute time).
    score_w_calls: float | None = None
    score_w_ontime: float | None = None
    score_w_speed: float | None = None
    score_w_util: float | None = None
    score_target_calls: float | None = None
    score_ontime_floor: float | None = None
    score_fast_secs: float | None = None
    score_slow_secs: float | None = None
    score_min_calls: int | None = None


def _f(override, default):
    """Override coerced to float when set, else the algorithm default."""
    return float(override) if override is not None else default


@dataclass(frozen=True)
class Resolved:
    throughput: float
    utilization: float
    percentile: float
    history_samples: int
    target_claim_seconds: float = DEFAULT_TARGET_CLAIM_SECONDS
    # Score-config overrides threaded from Settings (None = auto).
    score_w_calls: float | None = None
    score_w_ontime: float | None = None
    score_w_speed: float | None = None
    score_w_util: float | None = None
    score_target_calls: float | None = None
    score_ontime_floor: float | None = None
    score_fast_secs: float | None = None
    score_slow_secs: float | None = None
    score_min_calls: int | None = None

    @property
    def effective_throughput(self) -> float:
        return max(0.1, self.throughput * self.utilization)

    def score_config(self) -> "fs.ScoreConfig":
        """The composite GOAT-score config: each field is the override when set,
        else forklift_score's own DEFAULT_SCORE_CONFIG value."""
        from zira_dashboard import forklift_score as fs
        d = fs.DEFAULT_SCORE_CONFIG
        weights = {
            "calls": _f(self.score_w_calls, d.weights["calls"]),
            "ontime": _f(self.score_w_ontime, d.weights["ontime"]),
            "speed": _f(self.score_w_speed, d.weights["speed"]),
            "util": _f(self.score_w_util, d.weights["util"]),
        }
        return fs.ScoreConfig(
            weights=weights,
            target_calls=_f(self.score_target_calls, d.target_calls),
            ontime_floor=_f(self.score_ontime_floor, d.ontime_floor),
            fast_secs=_f(self.score_fast_secs, d.fast_secs),
            slow_secs=_f(self.score_slow_secs, d.slow_secs),
            min_calls=int(_f(self.score_min_calls, d.min_calls)),
        )


def resolve(s: Settings, *, algo_throughput: float) -> Resolved:
    """Effective parameters: each override if set, else the algorithm's value.
    The score overrides are threaded through verbatim (None = auto); they're
    resolved against forklift_score's defaults in Resolved.score_config()."""
    return Resolved(
        throughput=s.throughput_override if s.throughput_override is not None else algo_throughput,
        utilization=s.utilization_override if s.utilization_override is not None else DEFAULT_UTILIZATION,
        percentile=s.plan_for_percentile_override if s.plan_for_percentile_override is not None else DEFAULT_PLAN_FOR_PERCENTILE,
        history_samples=s.history_samples_override if s.history_samples_override is not None else DEFAULT_HISTORY_SAMPLES,
        target_claim_seconds=_f(s.target_claim_seconds, DEFAULT_TARGET_CLAIM_SECONDS),
        score_w_calls=s.score_w_calls,
        score_w_ontime=s.score_w_ontime,
        score_w_speed=s.score_w_speed,
        score_w_util=s.score_w_util,
        score_target_calls=s.score_target_calls,
        score_ontime_floor=s.score_ontime_floor,
        score_fast_secs=s.score_fast_secs,
        score_slow_secs=s.score_slow_secs,
        score_min_calls=s.score_min_calls,
    )


def algorithm_values(s: Settings, *, algo_throughput: float) -> Resolved:
    """The algorithm's own values, ignoring overrides (the baseline / ticks)."""
    return resolve(Settings(enabled=s.enabled), algo_throughput=algo_throughput)


DEFAULT = Settings()


def _row_to_settings(row: dict) -> Settings:
    def _f(v):
        return float(v) if v is not None else None
    def _i(v):
        return int(v) if v is not None else None
    return Settings(
        enabled=bool(row.get("enabled", True)),
        throughput_override=_f(row.get("throughput_override")),
        utilization_override=_f(row.get("utilization_override")),
        plan_for_percentile_override=_f(row.get("plan_for_percentile_override")),
        history_samples_override=_i(row.get("history_samples_override")),
        target_claim_seconds=_f(row.get("target_claim_seconds")),
        include_loading_jockeying=bool(row.get("include_loading_jockeying", False)),
        coldstart_calls_per_day=float(row.get("coldstart_calls_per_day") or 0.0),
        score_w_calls=_f(row.get("score_w_calls")),
        score_w_ontime=_f(row.get("score_w_ontime")),
        score_w_speed=_f(row.get("score_w_speed")),
        score_w_util=_f(row.get("score_w_util")),
        score_target_calls=_f(row.get("score_target_calls")),
        score_ontime_floor=_f(row.get("score_ontime_floor")),
        score_fast_secs=_f(row.get("score_fast_secs")),
        score_slow_secs=_f(row.get("score_slow_secs")),
        score_min_calls=_i(row.get("score_min_calls")),
    )


def _load_from_db() -> Settings:
    from . import db
    rows = db.query(
        "SELECT enabled, throughput_override, utilization_override, "
        "plan_for_percentile_override, history_samples_override, "
        "target_claim_seconds, "
        "include_loading_jockeying, coldstart_calls_per_day, "
        "score_w_calls, score_w_ontime, score_w_speed, score_w_util, "
        "score_target_calls, score_ontime_floor, score_fast_secs, "
        "score_slow_secs, score_min_calls "
        "FROM forklift_settings WHERE id = 1"
    )
    return _row_to_settings(rows[0]) if rows else DEFAULT


_store: CachedSingleton[Settings] = CachedSingleton(_load_from_db)


def current() -> Settings:
    """Return the singleton settings. Cached in process after first read;
    invalidated on save(). Falls back to DEFAULT if the table has no row."""
    return _store.current()


def save(s: Settings) -> None:
    """Persist the settings (UPSERT id=1) and update the in-process cache so
    the next current() returns the saved value without a re-read."""
    from . import db
    db.execute(
        "INSERT INTO forklift_settings "
        "(id, enabled, throughput_override, utilization_override, "
        "plan_for_percentile_override, history_samples_override, "
        "target_claim_seconds, "
        "include_loading_jockeying, coldstart_calls_per_day, "
        "score_w_calls, score_w_ontime, score_w_speed, score_w_util, "
        "score_target_calls, score_ontime_floor, score_fast_secs, "
        "score_slow_secs, score_min_calls) "
        "VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET enabled = EXCLUDED.enabled, "
        "throughput_override = EXCLUDED.throughput_override, "
        "utilization_override = EXCLUDED.utilization_override, "
        "plan_for_percentile_override = EXCLUDED.plan_for_percentile_override, "
        "history_samples_override = EXCLUDED.history_samples_override, "
        "target_claim_seconds = EXCLUDED.target_claim_seconds, "
        "include_loading_jockeying = EXCLUDED.include_loading_jockeying, "
        "coldstart_calls_per_day = EXCLUDED.coldstart_calls_per_day, "
        "score_w_calls = EXCLUDED.score_w_calls, "
        "score_w_ontime = EXCLUDED.score_w_ontime, "
        "score_w_speed = EXCLUDED.score_w_speed, "
        "score_w_util = EXCLUDED.score_w_util, "
        "score_target_calls = EXCLUDED.score_target_calls, "
        "score_ontime_floor = EXCLUDED.score_ontime_floor, "
        "score_fast_secs = EXCLUDED.score_fast_secs, "
        "score_slow_secs = EXCLUDED.score_slow_secs, "
        "score_min_calls = EXCLUDED.score_min_calls",
        (s.enabled, s.throughput_override, s.utilization_override,
         s.plan_for_percentile_override, s.history_samples_override,
         s.target_claim_seconds,
         s.include_loading_jockeying, s.coldstart_calls_per_day,
         s.score_w_calls, s.score_w_ontime, s.score_w_speed, s.score_w_util,
         s.score_target_calls, s.score_ontime_floor, s.score_fast_secs,
         s.score_slow_secs, s.score_min_calls),
    )
    _store.set(s)


def reload() -> Settings:
    """Force a fresh read from Postgres, bypassing the cache."""
    return _store.reload()
