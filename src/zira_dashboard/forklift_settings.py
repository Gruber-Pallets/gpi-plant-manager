"""Forklift demand-advisor settings: master toggle + tunable algorithm
parameters (per-driver throughput, target utilization, coverage work centers,
history window, cold-start fallback). Singleton row (id=1), cached in process
and invalidated on save() — same pattern as auto_lunch_settings.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    calls_per_hour: float = 16.0          # raw per-driver flat-out service rate
    target_utilization: float = 0.65      # headroom; 0 < .. <= 1
    include_loading_jockeying: bool = False  # coverage: Tablets only, or +Loading/Jockeying
    history_samples: int = 8              # max same-weekday snapshots for prediction
    coldstart_calls_per_day: float = 0.0  # 0 = auto from weekly trends; >0 = manual fallback

    @property
    def effective_throughput(self) -> float:
        """Per-driver calls/hour after applying the utilization headroom.
        Default: 16 * 0.65 = 10.4. Floored at 0.1 so it never hits 0."""
        return max(0.1, self.calls_per_hour * self.target_utilization)


DEFAULT = Settings()

_lock = RLock()
_cache: Settings | None = None


def _row_to_settings(row: dict) -> Settings:
    return Settings(
        enabled=bool(row.get("enabled", True)),
        calls_per_hour=float(row.get("calls_per_hour") or 16.0),
        target_utilization=float(row.get("target_utilization") or 0.65),
        include_loading_jockeying=bool(row.get("include_loading_jockeying", False)),
        history_samples=int(row.get("history_samples") or 8),
        coldstart_calls_per_day=float(row.get("coldstart_calls_per_day") or 0.0),
    )


def _load_from_db() -> Settings:
    from . import db
    rows = db.query(
        "SELECT enabled, calls_per_hour, target_utilization, "
        "include_loading_jockeying, history_samples, coldstart_calls_per_day "
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
        "(id, enabled, calls_per_hour, target_utilization, "
        "include_loading_jockeying, history_samples, coldstart_calls_per_day) "
        "VALUES (1, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET enabled = EXCLUDED.enabled, "
        "calls_per_hour = EXCLUDED.calls_per_hour, "
        "target_utilization = EXCLUDED.target_utilization, "
        "include_loading_jockeying = EXCLUDED.include_loading_jockeying, "
        "history_samples = EXCLUDED.history_samples, "
        "coldstart_calls_per_day = EXCLUDED.coldstart_calls_per_day",
        (s.enabled, s.calls_per_hour, s.target_utilization,
         s.include_loading_jockeying, s.history_samples, s.coldstart_calls_per_day),
    )
    with _lock:
        _cache = s


def reload() -> Settings:
    """Force a fresh read from Postgres, bypassing the cache."""
    global _cache
    with _lock:
        _cache = _load_from_db()
        return _cache
