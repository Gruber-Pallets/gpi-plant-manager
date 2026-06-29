"""Forklift demand-advisor settings. Each tunable is a NULLABLE OVERRIDE:
NULL = "auto" (follow the algorithm's own value). Singleton row (id=1), cached
in process, invalidated on save() — same pattern as auto_lunch_settings.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

# The algorithm's own default values for the policy knobs (driver throughput is
# data-derived elsewhere and passed in). These are the grey "tick" values.
DEFAULT_UTILIZATION = 0.65
DEFAULT_PLAN_FOR_PERCENTILE = 1.0   # 1.0 = busiest hour; lower = more typical
DEFAULT_HISTORY_SAMPLES = 8
DEFAULT_THROUGHPUT = 16.0           # fallback when no data-derived rate yet


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    throughput_override: float | None = None
    utilization_override: float | None = None
    plan_for_percentile_override: float | None = None
    history_samples_override: int | None = None
    include_loading_jockeying: bool = False
    coldstart_calls_per_day: float = 0.0


@dataclass(frozen=True)
class Resolved:
    throughput: float
    utilization: float
    percentile: float
    history_samples: int

    @property
    def effective_throughput(self) -> float:
        return max(0.1, self.throughput * self.utilization)


def resolve(s: Settings, *, algo_throughput: float) -> Resolved:
    """Effective parameters: each override if set, else the algorithm's value."""
    return Resolved(
        throughput=s.throughput_override if s.throughput_override is not None else algo_throughput,
        utilization=s.utilization_override if s.utilization_override is not None else DEFAULT_UTILIZATION,
        percentile=s.plan_for_percentile_override if s.plan_for_percentile_override is not None else DEFAULT_PLAN_FOR_PERCENTILE,
        history_samples=s.history_samples_override if s.history_samples_override is not None else DEFAULT_HISTORY_SAMPLES,
    )


def algorithm_values(s: Settings, *, algo_throughput: float) -> Resolved:
    """The algorithm's own values, ignoring overrides (the baseline / ticks)."""
    return resolve(Settings(enabled=s.enabled), algo_throughput=algo_throughput)


DEFAULT = Settings()

_lock = RLock()
_cache: Settings | None = None


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
        include_loading_jockeying=bool(row.get("include_loading_jockeying", False)),
        coldstart_calls_per_day=float(row.get("coldstart_calls_per_day") or 0.0),
    )


def _load_from_db() -> Settings:
    from . import db
    rows = db.query(
        "SELECT enabled, throughput_override, utilization_override, "
        "plan_for_percentile_override, history_samples_override, "
        "include_loading_jockeying, coldstart_calls_per_day "
        "FROM forklift_settings WHERE id = 1"
    )
    return _row_to_settings(rows[0]) if rows else DEFAULT


def current() -> Settings:
    """Return the singleton settings. Cached in process after first read;
    invalidated on save(). Falls back to DEFAULT if the table has no row."""
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load_from_db()
        return _cache


def save(s: Settings) -> None:
    """Persist the settings (UPSERT id=1) and update the in-process cache so
    the next current() returns the saved value without a re-read."""
    global _cache
    from . import db
    db.execute(
        "INSERT INTO forklift_settings "
        "(id, enabled, throughput_override, utilization_override, "
        "plan_for_percentile_override, history_samples_override, "
        "include_loading_jockeying, coldstart_calls_per_day) "
        "VALUES (1, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET enabled = EXCLUDED.enabled, "
        "throughput_override = EXCLUDED.throughput_override, "
        "utilization_override = EXCLUDED.utilization_override, "
        "plan_for_percentile_override = EXCLUDED.plan_for_percentile_override, "
        "history_samples_override = EXCLUDED.history_samples_override, "
        "include_loading_jockeying = EXCLUDED.include_loading_jockeying, "
        "coldstart_calls_per_day = EXCLUDED.coldstart_calls_per_day",
        (s.enabled, s.throughput_override, s.utilization_override,
         s.plan_for_percentile_override, s.history_samples_override,
         s.include_loading_jockeying, s.coldstart_calls_per_day),
    )
    with _lock:
        _cache = s


def reload() -> Settings:
    """Force a fresh read from Postgres, bypassing the cache."""
    global _cache
    with _lock:
        _cache = _load_from_db()
        return _cache
