"""Singleton rounding settings, cached in-process.

Mirrors schedule_store: settings get read on every kiosk punch, so an
in-process cache + RLock avoids hammering the DB. save() invalidates
the cache so the next current() reflects the new values.
"""

from __future__ import annotations

from threading import RLock

from .rounding import RoundingSettings

DEFAULT_SETTINGS = RoundingSettings(0, 0, 0, 0)

_lock = RLock()
_cache: RoundingSettings | None = None


def _load_from_db() -> RoundingSettings:
    from . import db
    rows = db.query(
        "SELECT in_before_min, in_after_min, out_before_min, out_after_min "
        "FROM rounding_settings WHERE id = 1"
    )
    if not rows:
        return DEFAULT_SETTINGS
    r = rows[0]
    return RoundingSettings(
        in_before_min=int(r["in_before_min"]),
        in_after_min=int(r["in_after_min"]),
        out_before_min=int(r["out_before_min"]),
        out_after_min=int(r["out_after_min"]),
    )


def current() -> RoundingSettings:
    """Return the cached singleton. Loads from DB on first call; subsequent
    calls hit the cache until save() or reload()."""
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load_from_db()
        return _cache


def save(settings: RoundingSettings) -> None:
    """Persist + update the cache so the next current() returns the new values."""
    global _cache
    from . import db
    db.execute(
        "INSERT INTO rounding_settings "
        "(id, in_before_min, in_after_min, out_before_min, out_after_min, updated_at) "
        "VALUES (1, %s, %s, %s, %s, now()) "
        "ON CONFLICT (id) DO UPDATE SET "
        "in_before_min = EXCLUDED.in_before_min, "
        "in_after_min = EXCLUDED.in_after_min, "
        "out_before_min = EXCLUDED.out_before_min, "
        "out_after_min = EXCLUDED.out_after_min, "
        "updated_at = now()",
        (
            settings.in_before_min,
            settings.in_after_min,
            settings.out_before_min,
            settings.out_after_min,
        ),
    )
    with _lock:
        _cache = settings


def reload() -> RoundingSettings:
    """Force a fresh read from Postgres, bypassing the cache."""
    global _cache
    with _lock:
        _cache = _load_from_db()
        return _cache
