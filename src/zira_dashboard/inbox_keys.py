"""Canonical Exception Inbox item keys.

One identity per inbox item, shared by the snapshot rows (exception_inbox) and
the resolve handlers (routes/*) so a logged inbox_events row correlates to the
open item it resolved. Keep these stable: the Phase 4 reconciler joins the open
set to the event log on this key, and the Phase 2b client diffs queue rows
against archived events by it.
"""
from __future__ import annotations


def time_off(request_id) -> str:
    return f"time_off:{request_id}"


def missing_wc(attendance_id) -> str:
    return f"missing_wc:{attendance_id}"


def missed_punch_out(attendance_id) -> str:
    return f"missed_punch_out:{attendance_id}"


def late(emp_id, day) -> str:
    """`day` is an ISO date string (the plant day)."""
    return f"late:{emp_id}:{day}"


def assignment(wc_name, start_iso) -> str:
    return f"assignment:{wc_name}:{start_iso}"


def plant_schedule(day) -> str:
    """`day` is an ISO date string."""
    return f"plant_schedule:{day}"


def breakdown(wc_name, stop_iso, person_name=None) -> str:
    """The incident's own key when person_name is None (the card header /
    dismiss target); a distinct per-operator key otherwise (the Transfer /
    snooze / auto-resolve target for one operator's row)."""
    if person_name:
        return f"breakdown:{wc_name}:{stop_iso}:{person_name}"
    return f"breakdown:{wc_name}:{stop_iso}"
