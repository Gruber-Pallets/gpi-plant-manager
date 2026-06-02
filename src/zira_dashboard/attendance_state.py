"""Reconciled timeclock attendance state, shared by the kiosk route and the
auto-lunch worker.

Extracted from routes/timeclock.py so the background worker reasons about the
same Odoo-reconciled clocked-in/out state the kiosk uses, without a
routes<-service import cycle. state_from_log/trust_local are pure (unit-testable
with no DB/Odoo); current_state wires the two local reads around them.

See docs/superpowers/specs/2026-06-01-timeclock-odoo-state-reconciliation-design.md.
"""
from __future__ import annotations

from datetime import datetime

from . import db, live_cache


def latest_punch(person_odoo_id: int) -> dict | None:
    """Most-recent local punch row for this person, or None. Carries the
    sync bookkeeping (synced_to_odoo, synced_at) the reconciliation rule
    needs to decide whether the cache could have seen it yet."""
    rows = db.query(
        "SELECT action, wc_name, "
        "COALESCE(rounded_at, occurred_at) AS occurred_at, "
        "odoo_attendance_id, synced_to_odoo, synced_at "
        "FROM timeclock_punches_log WHERE person_odoo_id = %s "
        "ORDER BY occurred_at DESC, id DESC LIMIT 1",
        (person_odoo_id,),
    )
    return rows[0] if rows else None


def state_from_log(latest: dict | None) -> dict:
    """The pre-reconciliation behavior: derive state purely from the most
    recent local punch. Used as the safe fallback (cold/stale cache) and
    whenever the local log wins."""
    if latest is None or latest["action"] in ("clock_out", "transfer_out"):
        return {"is_clocked_in": False, "current_wc": None,
                "check_in_ts": None, "open_odoo_attendance_id": None}
    return {"is_clocked_in": True, "current_wc": latest["wc_name"],
            "check_in_ts": latest["occurred_at"],
            "open_odoo_attendance_id": latest["odoo_attendance_id"]}


def trust_local(latest: dict | None, refreshed_at: datetime | None) -> bool:
    """True when the local log holds a punch the Odoo cache can't have
    reflected yet — i.e. the latest punch is unsynced, or the cache was
    last refreshed before that punch finished syncing to Odoo. This is the
    race-guard that stops a lagging cache from flashing the wrong screen
    right after a kiosk punch (and, for the auto-lunch worker, ensures a
    just-written unsynced auto clock_out reads as clocked-out)."""
    if latest is None:
        return False
    if not latest.get("synced_to_odoo"):
        return True
    synced_at = latest.get("synced_at")
    if synced_at is None:
        return True
    return refreshed_at <= synced_at


def current_state(person_odoo_id: int) -> dict:
    """The kiosk's view of an employee's current attendance state, reconciled
    against Odoo. Still a fast all-local read — no XML-RPC on the hot path.

    Sources: the Odoo open-attendance snapshot (live_cache, refreshed ~30s by
    the warmer) and the latest timeclock_punches_log row. Odoo is authoritative
    EXCEPT for very-recent local punches the snapshot can't have seen yet (see
    trust_local). If the snapshot is missing or stale, we degrade to the local
    log so an Odoo/warmer outage never blanks everyone to 'clocked out'.

    See docs/superpowers/specs/2026-06-01-timeclock-odoo-state-reconciliation-design.md.
    """
    snapshot, refreshed_at = live_cache.read_open_attendance()
    latest = latest_punch(person_odoo_id)
    if snapshot is None or live_cache.is_stale(refreshed_at):
        return state_from_log(latest)
    if trust_local(latest, refreshed_at):
        return state_from_log(latest)
    entry = snapshot.get(str(person_odoo_id))
    if not entry:
        return {"is_clocked_in": False, "current_wc": None,
                "check_in_ts": None, "open_odoo_attendance_id": None}
    check_in = entry.get("check_in")
    return {"is_clocked_in": True, "current_wc": entry.get("wc_name"),
            "check_in_ts": datetime.fromisoformat(check_in) if check_in else None,
            "open_odoo_attendance_id": entry.get("att_id")}
