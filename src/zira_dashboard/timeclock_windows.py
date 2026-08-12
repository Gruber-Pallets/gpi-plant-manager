"""Per-person work-center windows.

The dashboard and production records use Odoo's tagged attendance intervals
as the source of truth. This module also retains the local kiosk-log helper
for legacy callers; workers without a tagged Odoo interval fall back to the
published schedule and manual attributions.
"""
from __future__ import annotations

import time as _time
from collections import OrderedDict
from datetime import date, datetime, UTC
from threading import RLock

# In-module cache for attendance_windows_for_day. Past days are immutable
# (Odoo attendance edits to history are rare and a redeploy/restart clears
# this), so they cache indefinitely with simple LRU bounding. Today (and any
# future day) gets a short TTL matching the live-cache warmer cadence, so a
# range render doesn't fire one XML-RPC call per day per request.
_PAST_CACHE_MAX = 400
_TODAY_TTL_SECONDS = 45.0
_past_cache: OrderedDict[date, dict] = OrderedDict()
_today_cache: dict[date, tuple[float, dict]] = {}
_cache_lock = RLock()


def _segments_from_rows(rows: list[dict]) -> list[tuple[str, datetime, datetime | None]]:
    """rows: ONE person's punch rows, ordered by time. Each {action, wc_name, at}.
    Returns [(wc_name, start_utc, end_utc|None)]. Pure + testable."""
    out: list[tuple[str, datetime, datetime | None]] = []
    open_wc: str | None = None
    open_start: datetime | None = None
    for r in rows:
        action = r["action"]
        at = r["at"]
        if action in ("clock_in", "transfer_in"):
            if open_wc is not None and open_start is not None and at > open_start:
                out.append((open_wc, open_start, at))
            open_wc = r.get("wc_name")
            open_start = at
        elif action in ("clock_out", "transfer_out"):
            if open_wc is not None and open_start is not None and at > open_start:
                out.append((open_wc, open_start, at))
            open_wc = None
            open_start = None
    if open_wc is not None and open_start is not None:
        out.append((open_wc, open_start, None))
    return [(wc, s, e) for (wc, s, e) in out if wc]


def punch_windows_for_day(day: date) -> dict[str, list[tuple[str, datetime, datetime | None]]]:
    """{roster_name: [(wc_name, start_utc, end_utc|None), ...]} from the punch
    log for `day` (site-local day bounds). Never raises -- returns {} on error."""
    try:
        from . import db, attendance, shift_config
        from datetime import datetime as _dt, time as _time, timedelta as _td
        site = shift_config.SITE_TZ
        start_local = _dt.combine(day, _time(0, 0), tzinfo=site)   # local midnight
        end_local = start_local + _td(days=1)                      # next local midnight
        start_utc = start_local.astimezone(UTC)
        end_utc = end_local.astimezone(UTC)
        id_to_name = attendance.person_id_to_name()
        rows = db.query(
            "SELECT person_odoo_id, action, wc_name, "
            "       COALESCE(rounded_at, occurred_at) AS at "
            "FROM timeclock_punches_log "
            "WHERE COALESCE(rounded_at, occurred_at) >= %s "
            "  AND COALESCE(rounded_at, occurred_at) < %s "
            "ORDER BY person_odoo_id, COALESCE(rounded_at, occurred_at), id",
            (start_utc, end_utc),
        )
    except Exception:
        return {}
    by_person: dict[str, list[dict]] = {}
    for r in rows:
        name = id_to_name.get(str(r["person_odoo_id"]))
        if not name:
            continue
        by_person.setdefault(name, []).append(r)
    out: dict[str, list[tuple[str, datetime, datetime | None]]] = {}
    for name, rs in by_person.items():
        segs = _segments_from_rows(rs)
        if segs:
            out[name] = segs
    return out


def with_current_attendance_overrides(
    attendance_windows: dict[str, list[tuple[str, datetime, datetime | None]]],
    current_windows: dict[str, list[tuple[str, datetime, datetime | None]]],
) -> dict[str, list[tuple[str, datetime, datetime | None]]]:
    """Layer Odoo's current attendance record over that day's history.

    ``attendance_windows`` holds the day's Odoo attendance intervals.  The
    open-attendance snapshot is Odoo's live source of truth: if it names a
    work center, append that current interval for the person.  The assignment
    resolver closes an earlier open interval at this new check-in time, which
    preserves any real prior work while moving the active operator to the
    latest tablet work center.
    """
    merged = {
        name: list(windows)
        for name, windows in (attendance_windows or {}).items()
    }
    for name, windows in (current_windows or {}).items():
        if windows:
            existing = merged.setdefault(name, [])
            for window in windows:
                if window not in existing:
                    existing.append(window)
    return merged


def current_attendance_windows() -> tuple[
    dict[str, list[tuple[str, datetime, datetime | None]]], datetime | None
]:
    """Return Odoo's current, work-center-tagged attendance by roster name.

    The normal day intervals retain history; this small live overlay makes
    tablet sign-ins visible as soon as the Odoo open-attendance snapshot is
    refreshed.  Missing work centers are intentionally ignored: they provide
    no reliable station to display or credit.
    """
    try:
        from . import attendance, live_cache

        snapshot, refreshed_at = live_cache.read_open_attendance()
        if snapshot is None or live_cache.is_stale(refreshed_at):
            live_cache.refresh_odoo_open_attendance()
            snapshot, refreshed_at = live_cache.read_open_attendance()
        if not snapshot:
            return {}, refreshed_at
        id_to_name = attendance.person_id_to_name()
    except Exception:
        return {}, None

    out: dict[str, list[tuple[str, datetime, datetime | None]]] = {}
    for person_id, record in snapshot.items():
        name = id_to_name.get(str(person_id))
        wc_name = record.get("wc_name") if isinstance(record, dict) else None
        check_in = record.get("check_in") if isinstance(record, dict) else None
        if not name or not wc_name or not check_in:
            continue
        try:
            started_at = datetime.fromisoformat(check_in)
        except (TypeError, ValueError):
            continue
        out[name] = [(wc_name, started_at, None)]
    return out, refreshed_at


def _windows_from_intervals(intervals: list[dict]) -> list[tuple[str, datetime, datetime | None]]:
    """ONE person's attendance records -> [(wc_name, start_utc, end_utc|None)],
    sorted by start. Each record is {wc_name, start, end(None=still open)}.

    A record with NO wc_name inherits the previous record's WC -- the person
    didn't transfer (a transfer would tag the new WC), so they're still at the
    same WC (this is what stitches auto-lunch's untagged afternoon record onto
    the morning WC). A leading WC-less record (no prior WC) is skipped. Pure.
    """
    out: list[tuple[str, datetime, datetime | None]] = []
    last_wc: str | None = None
    for r in sorted(intervals, key=lambda x: x["start"]):
        wc = r.get("wc_name") or last_wc
        if not wc:
            continue
        last_wc = wc
        out.append((wc, r["start"], r.get("end")))
    return out


def attendance_windows_for_day_with_availability(
    day: date,
) -> tuple[dict[str, list[tuple[str, datetime, datetime | None]]], bool]:
    """{roster_name: [(wc_name, start_utc, end_utc|None), ...]} built from the
    COMPLETE set of Odoo hr.attendance records for `day` -- the source of truth
    for where each operator was clocked in.

    Unlike punch_windows_for_day (which reads the local kiosk punch mirror and
    can miss records that auto-lunch / sync write straight to Odoo), this reads
    every Odoo attendance record: the morning record, auto-lunch's afternoon
    record, and any mid-shift transfers -- so a scheduled operator's goal spans
    their whole clocked-in day instead of truncating at the auto-lunch split.

    Returns ``(windows, available)``. A successful empty read is ``({}, True)``;
    a source/read failure is ``({}, False)``. Errors are NOT cached, so a
    transient Odoo outage can't poison a past day's entry.
    """
    try:
        from . import shift_config
        today = datetime.now(shift_config.SITE_TZ).date()
    except Exception:
        return {}, False
    is_past = day < today
    with _cache_lock:
        if is_past:
            cached = _past_cache.get(day)
            if cached is not None:
                _past_cache.move_to_end(day)
                return cached, True
        else:
            hit = _today_cache.get(day)
            if hit is not None and (_time.monotonic() - hit[0]) < _TODAY_TTL_SECONDS:
                return hit[1], True
    try:
        from . import odoo_client, attendance
        from datetime import datetime as _dt
        intervals = odoo_client.fetch_attendance_intervals_for_day(day)
        id_to_name = attendance.person_id_to_name()
    except Exception:
        return {}, False
    by_person: dict[str, list[dict]] = {}
    for it in intervals:
        name = id_to_name.get(str(it.get("employee_odoo_id")))
        if not name:
            continue
        ci = it.get("check_in")
        if not ci:
            continue
        try:
            start = _dt.fromisoformat(ci)
            end = _dt.fromisoformat(it["check_out"]) if it.get("check_out") else None
        except (ValueError, TypeError):
            continue
        by_person.setdefault(name, []).append(
            {"wc_name": it.get("wc_name"), "start": start, "end": end})
    out: dict[str, list[tuple[str, datetime, datetime | None]]] = {}
    for name, recs in by_person.items():
        wins = _windows_from_intervals(recs)
        if wins:
            out[name] = wins
    with _cache_lock:
        if is_past:
            _past_cache[day] = out
            while len(_past_cache) > _PAST_CACHE_MAX:
                _past_cache.popitem(last=False)
        else:
            now_mono = _time.monotonic()
            stale = [k for k, (ts, _v) in _today_cache.items()
                     if now_mono - ts >= _TODAY_TTL_SECONDS]
            for k in stale:
                del _today_cache[k]
            _today_cache[day] = (now_mono, out)
    return out, True


def attendance_windows_for_day(day: date) -> dict[str, list[tuple[str, datetime, datetime | None]]]:
    """Preserve the legacy fail-soft dictionary API for existing callers."""
    windows, _available = attendance_windows_for_day_with_availability(day)
    return windows
