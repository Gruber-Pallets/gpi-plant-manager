"""Quick-punch fixer mode: Off / Preview / Live. Singleton row (id=1), cached
in process and refreshed on save() and on every reconcile — same audited
pattern as auto_lunch_settings.

- ``off``: the fixer does not touch Odoo.
- ``preview`` (default): the fixer only records what it would fix.
- ``live``: the fixer merges quick sign-in mistakes in Odoo.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from threading import Lock

from ._singleton import CachedSingleton


_log = logging.getLogger(__name__)

MODES = ("off", "preview", "live")
_SOURCES = ("settings", "external", "baseline")


@dataclass(frozen=True)
class Settings:
    mode: str = "preview"


DEFAULT = Settings()

# Stable signed-safe 64-bit key (ASCII "QPFIXSET"); never use process-random hash().
_TRANSACTION_LOCK_KEY = 0x5150464958534554


def _row_to_settings(row: dict) -> Settings:
    mode = row.get("mode")
    # The table CHECK allows only MODES; anything else reads as the safe
    # default (Preview never changes Odoo).
    return Settings(mode=mode if mode in MODES else DEFAULT.mode)


def _load_from_db() -> Settings:
    from . import db
    rows = db.query("SELECT mode FROM quick_punch_fix_settings WHERE id = 1")
    return _row_to_settings(rows[0]) if rows else DEFAULT


_store: CachedSingleton[Settings] = CachedSingleton(_load_from_db)
_save_lock = Lock()


def current() -> Settings:
    """Return the singleton settings. Cached in process after first read;
    refreshed on save() and reconcile. Falls back to DEFAULT if the table has
    no row."""
    return _store.current()


def _lock_singleton_transaction(cur) -> None:
    cur.execute(
        "SELECT pg_advisory_xact_lock(%s)",
        (_TRANSACTION_LOCK_KEY,),
    )


def _insert_event(cur, before: Settings | None, after: Settings,
                  actor_upn: str | None, actor_name: str | None,
                  source: str) -> None:
    cur.execute(
        "INSERT INTO quick_punch_fix_setting_events "
        "(before_mode, after_mode, actor_upn, actor_name, source) "
        "VALUES (%s, %s, %s, %s, %s)",
        (before.mode if before is not None else None, after.mode,
         actor_upn, actor_name, source),
    )


def save(s: Settings, *, actor_upn: str | None = None,
         actor_name: str | None = None, source: str = "settings") -> bool:
    """Persist ``s``. Writes one audit event, and returns True, only when the
    mode actually changes."""
    from . import db
    if s.mode not in MODES:
        raise ValueError(f"invalid quick-punch fixer mode: {s.mode!r}")
    if source not in _SOURCES:
        raise ValueError(f"invalid quick-punch fixer audit source: {source}")
    with _save_lock:
        changed = False
        persisted = s
        with db.cursor() as cur:
            _lock_singleton_transaction(cur)
            cur.execute(
                "SELECT mode FROM quick_punch_fix_settings WHERE id = 1 FOR UPDATE"
            )
            row = cur.fetchone()
            before = _row_to_settings(row) if row else None
            if before != s:
                cur.execute(
                    "INSERT INTO quick_punch_fix_settings (id, mode) "
                    "VALUES (1, %s) "
                    "ON CONFLICT (id) DO UPDATE SET mode = EXCLUDED.mode",
                    (s.mode,),
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
        "SELECT id, before_mode, after_mode, actor_upn, actor_name, source, "
        "changed_at "
        "FROM quick_punch_fix_setting_events "
        "ORDER BY changed_at DESC, id DESC LIMIT %s",
        (max(1, min(int(limit), 100)),),
    )


def _event_after_settings(row: dict) -> Settings:
    return Settings(mode=row["after_mode"])


def reconcile_external_change() -> Settings:
    """Audit a mode changed outside the app (a direct DB edit) and refresh the
    cache. Logs a ``baseline`` event when there is no history yet, and an
    ``external`` event when the row differs from the latest audited mode."""
    from . import db
    with _save_lock:
        with db.cursor() as cur:
            _lock_singleton_transaction(cur)
            cur.execute(
                "SELECT mode FROM quick_punch_fix_settings "
                "WHERE id = 1 FOR UPDATE"
            )
            row = cur.fetchone()
            persisted = _row_to_settings(row) if row else DEFAULT
            cur.execute("SAVEPOINT quick_punch_fix_external_audit")
            try:
                cur.execute(
                    "SELECT after_mode FROM quick_punch_fix_setting_events "
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
                    "Quick-punch fixer external change audit failed",
                    exc_info=True,
                )
                cur.execute(
                    "ROLLBACK TO SAVEPOINT quick_punch_fix_external_audit"
                )
                cur.execute("RELEASE SAVEPOINT quick_punch_fix_external_audit")
            else:
                cur.execute("RELEASE SAVEPOINT quick_punch_fix_external_audit")
        _store.set(persisted)
        return persisted


def reload() -> Settings:
    """Force a fresh read from Postgres, bypassing the cache."""
    return _store.reload()
