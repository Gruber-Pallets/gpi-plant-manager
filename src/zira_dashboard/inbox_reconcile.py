"""Reconcile the open-inbox mirror and log self-clearing items.

Runs on the warmer tick. Diffs the live open set (from build_snapshot's queue)
against the inbox_open_items mirror. An item that LEFT the queue, whose
category's source did NOT error this tick, and that has no human inbox_events
row since it was first seen, is logged 'auto_resolved'. A source error this
tick is treated as "unknown" (the item is kept), so a transient Odoo failure
can never mass-log false resolutions.
"""
from __future__ import annotations

import logging
from typing import Any

from . import db, exception_inbox, inbox_log

_log = logging.getLogger(__name__)

# Snapshot section id -> canonical item_kind.
_SECTION_KIND = {
    "assignments": "assignment",
    "plant_schedule": "plant_schedule",
    "late": "late",
    "missing_wc": "missing_wc",
    "missed_punch_out": "missed_punch_out",
    "time_off": "time_off",
}

# item_kind -> the build_snapshot source label (matches _capture(...) names),
# so a departed item can be checked against this tick's source_errors.
_KIND_SOURCE = {
    "assignment": "Assignments To Do",
    "plant_schedule": "Plant Schedule",
    "late": "Late / Absence",
    "missing_wc": "Missing Work Center",
    "missed_punch_out": "Missed Punch Out",
    "time_off": "Pending Time Off",
}


def plan_reconcile(open_now: dict, prev: dict, errored_sources: set) -> dict:
    """Pure diff. ``open_now``/``prev`` are {item_key: {item_kind, ...}}.

    Returns {arrivals: [keys], still_open: [keys], departed: [keys]}. A key that
    left but whose category source errored this tick is NOT reported as departed
    (we can't distinguish "resolved" from "source down")."""
    arrivals = [k for k in open_now if k not in prev]
    still_open = [k for k in open_now if k in prev]
    departed = []
    for key, row in prev.items():
        if key in open_now:
            continue
        if _KIND_SOURCE.get(row.get("item_kind")) in errored_sources:
            continue
        departed.append(key)
    return {"arrivals": arrivals, "still_open": still_open, "departed": departed}


def _open_now_from_snapshot(snapshot: dict) -> dict:
    out: dict[str, dict[str, Any]] = {}
    for row in snapshot.get("queue") or []:
        key = row.get("item_key")
        if not key:
            continue
        out[key] = {
            "item_kind": _SECTION_KIND.get(row.get("section_id"), row.get("section_id")),
            "person_name": row.get("name"),
            "category_label": row.get("category_label"),
            "priority": row.get("priority"),
        }
    return out


def _read_mirror() -> dict:
    rows = db.query(
        "SELECT item_key, item_kind, person_name, category_label, priority, first_seen "
        "FROM inbox_open_items"
    )
    return {r["item_key"]: r for r in rows}


def _upsert(key: str, info: dict) -> None:
    db.execute(
        "INSERT INTO inbox_open_items "
        "(item_key, item_kind, person_name, category_label, priority) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (item_key) DO UPDATE SET last_seen = now(), "
        "person_name = EXCLUDED.person_name, "
        "category_label = EXCLUDED.category_label, priority = EXCLUDED.priority",
        (key, info["item_kind"], info.get("person_name"),
         info.get("category_label"), info.get("priority")),
    )


def _delete(key: str) -> None:
    db.execute("DELETE FROM inbox_open_items WHERE item_key = %s", (key,))


def run_once() -> None:
    """One reconcile pass. Best-effort: one bad item never aborts the sweep."""
    snapshot = exception_inbox.build_snapshot()
    errored = {e.get("source") for e in (snapshot.get("source_errors") or [])}
    open_now = _open_now_from_snapshot(snapshot)
    prev = _read_mirror()
    actions = plan_reconcile(open_now, prev, errored)

    for key in actions["arrivals"]:
        _upsert(key, open_now[key])
    for key in actions["still_open"]:
        _upsert(key, open_now[key])

    for key in actions["departed"]:
        row = prev[key]
        try:
            if not inbox_log.has_human_event_since(key, row["first_seen"]):
                inbox_log.log_event_safe(
                    item_kind=row["item_kind"],
                    item_key=key,
                    person_name=row.get("person_name"),
                    category_label=row.get("category_label"),
                    action="auto_resolved",
                    outcome="Auto-resolved",
                    actor_upn=None,
                    actor_name=None,
                    source="auto",
                )
            _delete(key)
        except Exception as e:  # noqa: BLE001 -- one bad item never aborts the sweep
            _log.warning("inbox reconcile failed for %s: %s", key, e)
