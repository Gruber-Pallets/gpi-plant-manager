"""Auto-lunch settings: master toggle, observe-only mode, and the global flex
rule. Singleton row (id=1), cached in process and invalidated on save() —
same pattern as schedule_store.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from threading import Lock

from ._singleton import CachedSingleton


_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    observe_only: bool = True
    flex_after_hours: float = 5.0
    flex_minutes: int = 30


DEFAULT = Settings()

_FIELDS = "enabled, observe_only, flex_after_hours, flex_minutes"


def _row_to_settings(row: dict) -> Settings:
    flex_after_hours = row.get("flex_after_hours")
    flex_minutes = row.get("flex_minutes")
    return Settings(
        enabled=bool(row.get("enabled", False)),
        observe_only=bool(row.get("observe_only", True)),
        flex_after_hours=float(
            DEFAULT.flex_after_hours if flex_after_hours is None
            else flex_after_hours
        ),
        flex_minutes=int(
            DEFAULT.flex_minutes if flex_minutes is None else flex_minutes
        ),
    )


def _load_from_db() -> Settings:
    from . import db
    rows = db.query(
        f"SELECT {_FIELDS} FROM auto_lunch_settings WHERE id = 1"
    )
    return _row_to_settings(rows[0]) if rows else DEFAULT


_store: CachedSingleton[Settings] = CachedSingleton(_load_from_db)
_save_lock = Lock()


def current() -> Settings:
    """Return the singleton settings. Cached in process after first read;
    invalidated on save(). Falls back to DEFAULT if the table has no row."""
    return _store.current()


def _insert_event(cur, before: Settings | None, after: Settings,
                  actor_upn: str | None, actor_name: str | None,
                  source: str) -> None:
    before_values = (
        (before.enabled, before.observe_only, before.flex_after_hours,
         before.flex_minutes) if before is not None else (None, None, None, None)
    )
    cur.execute(
        "INSERT INTO auto_lunch_setting_events "
        "(before_enabled, before_observe_only, before_flex_after_hours, "
        " before_flex_minutes, after_enabled, after_observe_only, "
        " after_flex_after_hours, after_flex_minutes, actor_upn, actor_name, source) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (*before_values, after.enabled, after.observe_only,
         after.flex_after_hours, after.flex_minutes,
         actor_upn, actor_name, source),
    )


def save(s: Settings, *, actor_upn: str | None = None,
         actor_name: str | None = None, source: str = "settings") -> bool:
    from . import db
    if source not in {"settings", "external", "baseline"}:
        raise ValueError(f"invalid Auto-Lunch audit source: {source}")
    with _save_lock:
        changed = False
        persisted = s
        with db.cursor() as cur:
            cur.execute(
                f"SELECT {_FIELDS} FROM auto_lunch_settings WHERE id = 1 FOR UPDATE"
            )
            row = cur.fetchone()
            before = _row_to_settings(row) if row else None
            if before != s:
                cur.execute(
                    "INSERT INTO auto_lunch_settings "
                    "(id, enabled, observe_only, flex_after_hours, flex_minutes) "
                    "VALUES (1, %s, %s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET enabled = EXCLUDED.enabled, "
                    "observe_only = EXCLUDED.observe_only, "
                    "flex_after_hours = EXCLUDED.flex_after_hours, "
                    "flex_minutes = EXCLUDED.flex_minutes",
                    (s.enabled, s.observe_only, s.flex_after_hours, s.flex_minutes),
                )
                _insert_event(cur, before, s, actor_upn, actor_name, source)
                changed = True
            elif before is not None:
                persisted = before
        _store.set(persisted)
        return changed


def recent_events(limit: int = 20) -> list[dict]:
    from . import db
    return db.query(
        "SELECT id, before_enabled, before_observe_only, "
        "before_flex_after_hours, before_flex_minutes, after_enabled, "
        "after_observe_only, after_flex_after_hours, after_flex_minutes, "
        "actor_upn, actor_name, source, changed_at "
        "FROM auto_lunch_setting_events ORDER BY changed_at DESC, id DESC LIMIT %s",
        (max(1, min(int(limit), 100)),),
    )


def _event_after_settings(row: dict) -> Settings:
    return Settings(
        enabled=bool(row["after_enabled"]),
        observe_only=bool(row["after_observe_only"]),
        flex_after_hours=float(row["after_flex_after_hours"]),
        flex_minutes=int(row["after_flex_minutes"]),
    )


def reconcile_external_change() -> Settings:
    from . import db
    with _save_lock:
        with db.cursor() as cur:
            cur.execute(
                f"SELECT {_FIELDS} FROM auto_lunch_settings "
                "WHERE id = 1 FOR UPDATE"
            )
            row = cur.fetchone()
            persisted = _row_to_settings(row) if row else DEFAULT
            cur.execute("SAVEPOINT auto_lunch_external_audit")
            try:
                cur.execute(
                    "SELECT after_enabled, after_observe_only, "
                    "after_flex_after_hours, after_flex_minutes "
                    "FROM auto_lunch_setting_events "
                    "ORDER BY changed_at DESC, id DESC LIMIT 1"
                )
                latest = cur.fetchone()
                if latest is None:
                    _insert_event(
                        cur, None, persisted, None, None, "baseline"
                    )
                else:
                    audited = _event_after_settings(latest)
                    if audited != persisted:
                        _insert_event(
                            cur, audited, persisted, None, None, "external"
                        )
            except Exception:
                _log.warning(
                    "Auto-Lunch external change audit failed", exc_info=True
                )
                cur.execute("ROLLBACK TO SAVEPOINT auto_lunch_external_audit")
                cur.execute("RELEASE SAVEPOINT auto_lunch_external_audit")
            else:
                cur.execute("RELEASE SAVEPOINT auto_lunch_external_audit")
        _store.set(persisted)
        return persisted


def reload() -> Settings:
    """Force a fresh read from Postgres, bypassing the cache."""
    return _store.reload()
