"""Auto-lunch settings: master toggle, observe-only mode, and the global flex
rule. Singleton row (id=1), cached in process and invalidated on save() —
same pattern as schedule_store.
"""
from __future__ import annotations

from dataclasses import dataclass

from ._singleton import CachedSingleton


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    observe_only: bool = True
    flex_after_hours: float = 5.0
    flex_minutes: int = 30


DEFAULT = Settings()


def _row_to_settings(row: dict) -> Settings:
    return Settings(
        enabled=bool(row.get("enabled", False)),
        observe_only=bool(row.get("observe_only", True)),
        flex_after_hours=float(row.get("flex_after_hours") or 5.0),
        flex_minutes=int(row.get("flex_minutes") or 30),
    )


def _load_from_db() -> Settings:
    from . import db
    rows = db.query(
        "SELECT enabled, observe_only, flex_after_hours, flex_minutes "
        "FROM auto_lunch_settings WHERE id = 1"
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
        "INSERT INTO auto_lunch_settings "
        "(id, enabled, observe_only, flex_after_hours, flex_minutes) "
        "VALUES (1, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET enabled = EXCLUDED.enabled, "
        "observe_only = EXCLUDED.observe_only, "
        "flex_after_hours = EXCLUDED.flex_after_hours, "
        "flex_minutes = EXCLUDED.flex_minutes",
        (s.enabled, s.observe_only, s.flex_after_hours, s.flex_minutes),
    )
    _store.set(s)


def reload() -> Settings:
    """Force a fresh read from Postgres, bypassing the cache."""
    return _store.reload()
