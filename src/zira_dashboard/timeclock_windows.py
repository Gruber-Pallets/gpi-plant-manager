"""Per-person work-center windows derived from the local kiosk punch log
(timeclock_punches_log). A clock_in/transfer_in opens a window at its
wc_name; a transfer_out/clock_out (or the next open) closes it. Trailing
open windows (still clocked in) get end=None and are closed downstream by
assignment_windows against the shift cap.

Kiosk is still a Phase-0 pilot, so most operators have no punches yet --
punch_windows_for_day returns {} for them and the resolver falls back to
schedule + manual attributions.
"""
from __future__ import annotations

from datetime import date, datetime


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
        from datetime import datetime as _dt, time as _time, timezone as _tz, timedelta as _td
        site = shift_config.SITE_TZ
        start_local = _dt.combine(day, _time(0, 0), tzinfo=site)   # local midnight
        end_local = start_local + _td(days=1)                      # next local midnight
        start_utc = start_local.astimezone(_tz.utc)
        end_utc = end_local.astimezone(_tz.utc)
        id_to_name = {v: k for k, v in attendance.name_to_person_id().items()}
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
