"""Reconcile the open-inbox mirror and log self-clearing items.

Runs on the warmer tick. Diffs the live open set (from build_snapshot's queue)
against the inbox_open_items mirror. An item that LEFT the queue is logged
'auto_resolved' ONLY when its category was *fully enumerated* this tick (source
neither errored nor truncated by a display cap), it has no human inbox_events
row since it was first seen, and it departed more than a grace period ago. Those
guards mean a transient source failure, a display cap, or a resolve/log race can
never mass-log false resolutions.
"""
from __future__ import annotations

import logging
from typing import Any

from . import db, exception_inbox, inbox_log, plant_day

_log = logging.getLogger(__name__)

# Only auto-resolve a departure once it has been gone at least this long, so the
# resolving handler's (non-transactional, two-step) human-event write is sure to
# have landed first. Must exceed the reconcile tick interval.
_AUTO_RESOLVE_GRACE_SECONDS = 90

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
# used to tell whether a category's source errored this tick.
_KIND_SOURCE = {
    "assignment": "Assignments To Do",
    "plant_schedule": "Plant Schedule",
    "late": "Late / Absence",
    "missing_wc": "Missing Work Center",
    "missed_punch_out": "Missed Punch Out",
    "time_off": "Pending Time Off",
}


def plan_reconcile(open_now: dict, prev: dict, complete_kinds: set) -> dict:
    """Pure diff. ``open_now``/``prev`` are {item_key: {item_kind, ...}}.

    Returns {arrivals: [keys], still_open: [keys], departed: [keys]}. A key that
    left the queue is reported as departed ONLY when its category is in
    ``complete_kinds`` (fully enumerated this tick). A category that errored or
    was truncated is absent from ``complete_kinds``, so its vanished items are
    kept (we can't tell "resolved" from "source down / capped out")."""
    arrivals = [k for k in open_now if k not in prev]
    still_open = [k for k in open_now if k in prev]
    departed = [
        key for key, row in prev.items()
        if key not in open_now and row.get("item_kind") in complete_kinds
    ]
    return {"arrivals": arrivals, "still_open": still_open, "departed": departed}


def _complete_kinds(snapshot: dict) -> set:
    """Item kinds fully enumerated this tick: the source did not error AND no
    display cap hid any rows. Only these are safe to auto-resolve departures from.

    A display cap can only ever HIDE items, so a truncated section shows FEWER
    rows than its true ``count`` -- the test is ``len(rows) < count``, not
    inequality. Some sections legitimately carry MORE rows than ``count``: the
    late queue appends ``snoozed`` rows (excluded from the actionable ``count``)
    and reuses one ``item_key`` across buckets, so ``len(rows) > count`` there.
    That direction is never truncation, so it must not drop the kind -- otherwise
    a legitimate late self-clear would wait for a snooze-free tick to auto-resolve."""
    errored = {e.get("source") for e in (snapshot.get("source_errors") or [])}
    complete: set[str] = set()
    for section in snapshot.get("sections") or []:
        kind = _SECTION_KIND.get(section.get("id"))
        if kind is None:
            continue
        if _KIND_SOURCE.get(kind) in errored:
            continue
        if len(section.get("rows") or []) < int(section.get("count") or 0):
            continue  # fewer rows than count -> truncated by a display cap
        complete.add(kind)
    return complete


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
        "SELECT item_key, item_kind, person_name, category_label, priority, "
        "first_seen, last_seen FROM inbox_open_items"
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
    open_now = _open_now_from_snapshot(snapshot)
    prev = _read_mirror()
    complete = _complete_kinds(snapshot)
    actions = plan_reconcile(open_now, prev, complete)

    for key in actions["arrivals"]:
        _upsert(key, open_now[key])
    for key in actions["still_open"]:
        _upsert(key, open_now[key])

    now = plant_day.now()
    for key in actions["departed"]:
        row = prev[key]
        last_seen = row.get("last_seen")
        if last_seen is not None and (now - last_seen).total_seconds() < _AUTO_RESOLVE_GRACE_SECONDS:
            continue  # departed too recently; re-check next tick (resolve/log race guard)
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
