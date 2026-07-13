"""Machine breakdown detection and exclusion math for the Exception Inbox.

Mirrors missing_wc.py's role for its category, but with more state: a
breakdown incident persists (machine_breakdowns), tracks per-operator
snoozes (breakdown_snoozes), and drives a per-operator time exclusion
(wc_time_attributions source='breakdown') that mirrors the existing
source='testing' mechanism -- except testing zeroes UNITS (credited to no
one) while a breakdown zeroes EXPECTED minutes (units earned before the
breakdown are kept).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

BREAKDOWN_NO_OUTPUT_MINUTES = 60
"""Default minutes of no output (while an operator is clocked in) before a
station is flagged as broken down."""


@dataclass(frozen=True)
class StationSignal:
    wc_name: str
    last_output_utc: datetime | None  # None = no output yet today
    has_operator: bool  # at least one operator currently clocked in on this WC


@dataclass(frozen=True)
class BreakdownCandidate:
    wc_name: str
    stop_utc: datetime


def detect(
    signals: list[StationSignal],
    now: datetime,
    shift_start_utc: datetime,
    shift_end_utc: datetime,
    no_output_minutes: int = BREAKDOWN_NO_OUTPUT_MINUTES,
    *,
    elapsed_minutes: Callable[[datetime, datetime], float] | None = None,
    now_is_productive: bool = True,
) -> list[BreakdownCandidate]:
    """Pure. Which stations should open a NEW breakdown incident this tick.

    A station is a candidate when it has an operator clocked in AND has
    produced nothing for >= no_output_minutes (measured from its last output,
    or from shift start if it has never produced today, using
    elapsed_minutes when provided) AND `now` is productive shift time. The
    caller is responsible for excluding stations that already have an open
    incident, an active testing window, or were recently dismissed without
    new output since -- this function only applies the no-output-while-
    staffed rule."""
    if not now_is_productive or now < shift_start_utc or now > shift_end_utc:
        return []
    out: list[BreakdownCandidate] = []
    for sig in signals:
        if not sig.has_operator:
            continue
        stop = sig.last_output_utc or shift_start_utc
        if elapsed_minutes is None:
            elapsed = (now - stop).total_seconds() / 60.0
        else:
            elapsed = elapsed_minutes(stop, now)
        if elapsed < no_output_minutes:
            continue
        out.append(BreakdownCandidate(wc_name=sig.wc_name, stop_utc=stop))
    return out


def departed_at(
    person_name: str,
    wc_name: str,
    punch_windows: dict[str, list[tuple]],
    stop_utc: datetime,
) -> datetime | None:
    """Pure. None if the person still has an open (or not-yet-closed-since-
    the-breakdown) punch on wc_name; otherwise the UTC time of their last
    closed punch window on wc_name at/after `stop_utc` -- i.e. when they left
    the broken machine (by transfer or clock-out). `punch_windows` matches
    assignment_windows.resolve_segments's punch_windows param shape:
    {person_name: [(wc_name, start_utc, end_utc|None), ...]}."""
    windows = [w for w in punch_windows.get(person_name, []) if w[0] == wc_name]
    relevant = [(s, e) for (_wc, s, e) in windows if e is None or e > stop_utc]
    if not relevant:
        return None
    if any(e is None for _, e in relevant):
        return None
    return max(e for _, e in relevant)


def excluded_minutes_for_windows(
    windows: list[tuple[datetime, datetime | None]],
    day: date,
    productive_minutes_in_window,
) -> float:
    """Pure. Sum of productive_minutes_in_window(day, start, end) over each
    CLOSED [start, end) window (end is not None and end > start); open or
    zero/negative-span windows are skipped. `productive_minutes_in_window`
    is injected (matches shift_config.productive_minutes_in_window's
    signature) so this is testable without shift config or timezones,
    mirroring routes/leaderboards.py's averages_for_wc DI style."""
    total = 0.0
    for start, end in windows:
        if end is None or end <= start:
            continue
        total += productive_minutes_in_window(day, start, end)
    return total


def excluded_minutes_overlapping(
    windows: list[tuple[datetime, datetime | None]],
    start_utc: datetime,
    end_utc: datetime,
    now_utc: datetime,
    day: date,
    productive_minutes_in_window,
) -> float:
    """Pure. Sum of productive_minutes_in_window(day, lo, hi) for the overlap
    of each breakdown window (open windows capped at now_utc) with
    [start_utc, end_utc). Used to shrink one work segment's productive
    minutes (recycling per-WC expected) to honor a breakdown exclusion,
    without needing a whole-day total."""
    clipped: list[tuple[datetime, datetime]] = []
    for w_start, w_end in windows:
        w_end = w_end if w_end is not None else now_utc
        lo = max(w_start, start_utc)
        hi = min(w_end, end_utc)
        if hi > lo:
            clipped.append((lo, hi))
    return excluded_minutes_for_windows(clipped, day, productive_minutes_in_window)


BREAKDOWN_SNOOZE_MINUTES = 15


def open_incident(wc_name: str, day, stop_utc: datetime, source: str = "auto") -> int:
    """Open a new breakdown incident. Caller must ensure no incident is
    already open for (wc_name, day) -- see get_open_incident."""
    from . import db
    rows = db.query(
        "INSERT INTO machine_breakdowns (wc_name, day, detected_stop_utc, source) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (wc_name, day, stop_utc, source),
    )
    return rows[0]["id"]


def get_open_incident(wc_name: str, day) -> dict | None:
    """The currently-open incident for (wc_name, day), or None."""
    from . import db
    rows = db.query(
        "SELECT id, wc_name, day, detected_stop_utc, source, created_at, "
        "resolved_at, resolution, resume_utc FROM machine_breakdowns "
        "WHERE wc_name = %s AND day = %s AND resolved_at IS NULL",
        (wc_name, day),
    )
    return rows[0] if rows else None


def get_incident(incident_id: int) -> dict | None:
    """One incident by id, open or resolved."""
    from . import db
    rows = db.query(
        "SELECT id, wc_name, day, detected_stop_utc, source, created_at, "
        "resolved_at, resolution, resume_utc FROM machine_breakdowns WHERE id = %s",
        (incident_id,),
    )
    return rows[0] if rows else None


def all_open_incidents(day) -> list[dict]:
    """Every currently-open incident for `day`, oldest first."""
    from . import db
    return db.query(
        "SELECT id, wc_name, day, detected_stop_utc, source, created_at, "
        "resolved_at, resolution, resume_utc FROM machine_breakdowns "
        "WHERE day = %s AND resolved_at IS NULL ORDER BY detected_stop_utc",
        (day,),
    )


def resolve_incident(incident_id: int, resolution: str, resume_utc: datetime | None = None) -> None:
    """Mark an incident resolved (resolution in 'recovered'|'handled'|'dismissed')."""
    from . import db
    db.execute(
        "UPDATE machine_breakdowns SET resolved_at = now(), resolution = %s, resume_utc = %s "
        "WHERE id = %s",
        (resolution, resume_utc, incident_id),
    )


def reopen_incident(incident_id: int) -> None:
    """Undo a resolution -- clears resolved_at/resolution/resume_utc so the
    incident is open again (dismiss-undo)."""
    from . import db
    db.execute(
        "UPDATE machine_breakdowns SET resolved_at = NULL, resolution = NULL, resume_utc = NULL "
        "WHERE id = %s",
        (incident_id,),
    )


def snooze_operator(incident_id: int, person_name: str, minutes: int = BREAKDOWN_SNOOZE_MINUTES) -> None:
    """Silence one operator's row on this incident's card for `minutes`."""
    from . import db
    until = datetime.now(UTC) + timedelta(minutes=minutes)
    db.execute(
        "INSERT INTO breakdown_snoozes (breakdown_id, person_name, until_utc) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (breakdown_id, person_name) DO UPDATE SET "
        "until_utc = EXCLUDED.until_utc, created_at = now()",
        (incident_id, person_name, until),
    )


def active_snooze_until(incident_id: int, person_name: str) -> datetime | None:
    """The until_utc timestamp if this operator's snooze on this incident
    hasn't expired yet, else None."""
    from . import db
    rows = db.query(
        "SELECT until_utc FROM breakdown_snoozes "
        "WHERE breakdown_id = %s AND person_name = %s AND until_utc > now()",
        (incident_id, person_name),
    )
    return rows[0]["until_utc"] if rows else None


def _enabled() -> bool:
    import os
    return os.environ.get("MACHINE_BREAKDOWN_ENABLED", "true").strip().lower() not in ("0", "false", "no")


def _shift_bounds(day: date) -> tuple[datetime, datetime]:
    from .shift_config import shift_start_for, shift_end_for, SITE_TZ
    start = datetime.combine(day, shift_start_for(day), tzinfo=SITE_TZ).astimezone(UTC)
    end = datetime.combine(day, shift_end_for(day), tzinfo=SITE_TZ).astimezone(UTC)
    return start, end


def _punch_windows_for_day(day: date) -> dict:
    from . import timeclock_windows
    return timeclock_windows.attendance_windows_for_day(day)


def _present_operators_on_wc(
    wc_name: str, day: date, now: datetime | None = None
) -> list[str]:
    """Names with an open attendance window at this work center at now."""
    now = now or datetime.now(UTC)
    return sorted({
        person
        for person, windows in _punch_windows_for_day(day).items()
        for punched_wc, start, end in windows
        if punched_wc == wc_name and start <= now and end is None
    })


def _station_signals(day: date, now: datetime) -> list[StationSignal]:
    """One StationSignal per metered recycling station with an operator
    currently on it."""
    from . import staffing
    from .leaderboard import cached_leaderboard
    from .stations import recycling_stations
    from .deps import client  # local import: avoid a hard dep at module load
    totals = cached_leaderboard(client, recycling_stations(), day, now_utc=now)
    meter_to_loc_name = {loc.meter_id: loc.name for loc in staffing.LOCATIONS if loc.meter_id}
    out: list[StationSignal] = []
    for total in totals:
        wc_name = meter_to_loc_name.get(total.station.meter_id, total.station.name)
        # NB: total.active_intervals[-1][1] is NOT the last real production
        # timestamp -- leaderboard._active_intervals pads the tail interval
        # forward by up to TRANSFER_GAP (60 min) so a lunch-adjacent gap
        # doesn't wrongly split a shift for uptime-display purposes. Using
        # that padded value here would silently push effective breakdown
        # detection out to ~75 min of real silence instead of the intended
        # 15. samples is the actual (event_dt_utc, units) production log --
        # samples[-1][0] is the true last-unit timestamp.
        last_output = total.samples[-1][0] if total.samples else None
        has_operator = bool(_present_operators_on_wc(wc_name, day, now))
        out.append(StationSignal(wc_name=wc_name, last_output_utc=last_output, has_operator=has_operator))
    return out


def _last_output_after(wc_name: str, day: date, stop_utc: datetime) -> datetime | None:
    """The most recent output time for wc_name strictly after `stop_utc`, or
    None if it's still silent -- used to detect recovery."""
    for sig in _station_signals(day, datetime.now(UTC)):
        if sig.wc_name == wc_name and sig.last_output_utc and sig.last_output_utc > stop_utc:
            return sig.last_output_utc
    return None


def _last_output_before(wc_name: str, day: date, now: datetime) -> datetime | None:
    """The station's last output time as of `now` (or None if it hasn't
    produced today) -- used by the manual report button."""
    for sig in _station_signals(day, now):
        if sig.wc_name == wc_name:
            return sig.last_output_utc
    return None


def run_detect_tick(day: date | None = None, now: datetime | None = None) -> None:
    """One detection pass: open new incidents, cap operators who've left a
    broken machine, and auto-resolve incidents whose machine is producing
    again. Called from the warmer; best-effort per incident so one bad
    incident never blocks the others."""
    if not _enabled():
        return
    from . import wc_attributions
    from .plant_day import today as plant_today
    day = day or plant_today()
    now = now or datetime.now(UTC)
    shift_start, shift_end = _shift_bounds(day)

    try:
        open_incidents = all_open_incidents(day)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "machine breakdown: failed to load open incidents", exc_info=True)
        open_incidents = []

    for incident in open_incidents:
        try:
            if not _present_operators_on_wc(incident["wc_name"], day, now):
                resolve_incident(incident["id"], "handled")
                continue
            _cap_departed_operators(incident, day, now)
            _maybe_auto_resolve(incident, day, now)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "machine breakdown tick failed for incident %s", incident["id"], exc_info=True)

    from . import shift_config
    candidates = detect(
        _station_signals(day, now),
        now,
        shift_start,
        shift_end,
        elapsed_minutes=lambda start, end: shift_config.productive_minutes_in_window(
            day, start, end
        ),
        now_is_productive=shift_config.in_shift_on(now.astimezone(shift_config.SITE_TZ)),
    )
    for candidate in candidates:
        if get_open_incident(candidate.wc_name, day) is not None:
            continue
        try:
            incident_id = open_incident(candidate.wc_name, day, candidate.stop_utc, source="auto")
            for person in _present_operators_on_wc(candidate.wc_name, day, now):
                wc_attributions.add_breakdown(day, candidate.wc_name, person, candidate.stop_utc, incident_id)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "machine breakdown open failed for %s", candidate.wc_name, exc_info=True)


def _cap_departed_operators(incident: dict, day: date, now: datetime) -> None:
    """Cap any operator's open breakdown row the moment they leave the
    broken machine (transfer or self-punch-out) -- detected via their punch
    windows, not via the Transfer button (which caps immediately itself;
    this is the passive/punch-out path)."""
    from . import wc_attributions
    wc_name = incident["wc_name"]
    stop = incident["detected_stop_utc"]
    punch_windows = _punch_windows_for_day(day)
    for person in punch_windows:
        dep = departed_at(person, wc_name, punch_windows, stop)
        if dep is None:
            continue
        row = wc_attributions.open_breakdown_row(day, wc_name, person)
        if row is not None:
            wc_attributions.cap_breakdown(row["id"], dep)


def _maybe_auto_resolve(incident: dict, day: date, now: datetime) -> None:
    """Resolve an incident as 'recovered' once its station has produced
    output again, capping any operator still open at the resume time."""
    from . import wc_attributions
    resume = _last_output_after(incident["wc_name"], day, incident["detected_stop_utc"])
    if resume is None:
        return
    for person in _present_operators_on_wc(incident["wc_name"], day, now):
        row = wc_attributions.open_breakdown_row(day, incident["wc_name"], person)
        if row is not None:
            wc_attributions.cap_breakdown(row["id"], resume)
    resolve_incident(incident["id"], "recovered", resume_utc=resume)


def current_rows(day: date | None = None, now: datetime | None = None) -> list[dict]:
    """Snapshot rows for every open incident today: one header row (machine
    info + dismiss) followed by one row per operator (Transfer/Snooze, or a
    muted no-action row while snoozed). Header and operator rows share the
    same item_kind ("breakdown") but differ by action/absence of action --
    see inbox_keys.breakdown and routes/exceptions.py's undo wiring."""
    from . import inbox_keys
    from .plant_day import today as plant_today
    day = day or plant_today()
    now = now or datetime.now(UTC)

    rows: list[dict] = []
    for incident in all_open_incidents(day):
        wc_name = incident["wc_name"]
        operators = _present_operators_on_wc(wc_name, day, now)
        if not operators:
            continue
        stop = incident["detected_stop_utc"]
        stop_iso = stop.isoformat()
        elapsed_min = int((now - stop).total_seconds() // 60)
        rows.append({
            "name": wc_name,
            "label": "Stopped producing",
            "detail": f"No output since {_local_time_label(stop)} ({elapsed_min} min)",
            "priority": "urgent",
            "badge": "AUTO-DETECTED" if incident["source"] == "auto" else "MANUAL",
            "row_key": f"breakdown_header:{wc_name}:{stop_iso}",
            "item_key": inbox_keys.breakdown(wc_name, stop_iso),
            "action": None,
            "dismiss_action": {
                "type": "breakdown_dismiss",
                "incident_id": incident["id"],
            },
        })
        for person in operators:
            snoozed_until = active_snooze_until(incident["id"], person)
            item_key = inbox_keys.breakdown(wc_name, stop_iso, person)
            if snoozed_until is not None:
                mins_left = max(1, int((snoozed_until - now).total_seconds() // 60))
                rows.append({
                    "name": person,
                    "label": "Snoozed",
                    "detail": f"Re-checks in {mins_left} min",
                    "priority": "muted",
                    "badge": "Follow-up",
                    "row_key": f"breakdown_snoozed:{wc_name}:{stop_iso}:{person}",
                    "item_key": item_key,
                    "action": None,
                })
                continue
            rows.append({
                "name": person,
                "label": f"Idle — {wc_name} is down",
                "detail": "",
                "priority": "urgent",
                "badge": "Needs decision",
                "row_key": f"breakdown_op:{wc_name}:{stop_iso}:{person}",
                "item_key": item_key,
                "action": {
                    "type": "breakdown",
                    "incident_id": incident["id"],
                    "person_name": person,
                    "wc_name": wc_name,
                },
            })
    return rows


def _local_time_label(dt: datetime) -> str:
    import os
    from .shift_config import SITE_TZ
    local = dt.astimezone(SITE_TZ)
    fmt = "%#I:%M %p" if os.name == "nt" else "%-I:%M %p"
    return local.strftime(fmt)


def report_manual(wc_name: str, day: date | None = None, now: datetime | None = None) -> dict:
    """Open (or find) a breakdown incident for wc_name on demand -- the
    "+ Report a breakdown" button. Returns {ok, incident_id, already_open?}."""
    from . import wc_attributions
    from .plant_day import today as plant_today
    day = day or plant_today()
    now = now or datetime.now(UTC)

    existing = get_open_incident(wc_name, day)
    if existing is not None:
        return {"ok": True, "incident_id": existing["id"], "already_open": True}

    stop = _last_output_before(wc_name, day, now) or now
    incident_id = open_incident(wc_name, day, stop, source="manual")
    operators = _present_operators_on_wc(wc_name, day, now)
    for person in operators:
        wc_attributions.add_breakdown(day, wc_name, person, stop, incident_id)
    if not operators:
        # Nothing to act on -- resolve immediately rather than leaving an
        # empty, un-actionable card in the queue (mirrors the "informational
        # only, auto-resolves" edge case in the design spec).
        resolve_incident(incident_id, "handled")
    return {"ok": True, "incident_id": incident_id}
