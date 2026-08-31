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
    sample_times_utc: tuple[datetime, ...] = ()


@dataclass(frozen=True)
class BreakdownCandidate:
    wc_name: str
    stop_utc: datetime


@dataclass(frozen=True)
class OperatorPresence:
    person_name: str
    wc_name: str
    arrival_utc: datetime
    employee_odoo_id: int | None = None


@dataclass(frozen=True)
class OperatorDeparture:
    person_name: str
    wc_name: str
    arrival_utc: datetime
    departure_utc: datetime
    employee_odoo_id: int | None = None


@dataclass(frozen=True)
class OperatorSourceSnapshot:
    presences: tuple[OperatorPresence, ...]
    departures: tuple[OperatorDeparture, ...]
    available: bool
    mirror_owned: bool
    complete: bool = True


class BreakdownRows(list):
    """Breakdown rows plus whether worker identity was fully enumerated."""

    def __init__(self, values=(), *, complete: bool):
        super().__init__(values)
        self.complete = bool(complete)


def personal_breakdown_start(
    *, station_stop_utc: datetime, arrival_utc: datetime
) -> datetime:
    """The worker clock cannot begin before the worker reached the station."""
    return max(station_stop_utc, arrival_utc)


def first_output_after(
    sample_times_utc: tuple[datetime, ...] | list[datetime],
    stop_utc: datetime,
) -> datetime | None:
    """The first real production sample after a station stopped."""
    return min((sample for sample in sample_times_utc if sample > stop_utc), default=None)


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


def dismiss_incident(incident_id: int) -> list[dict] | None:
    """Dismiss one still-open incident and return its exact undo snapshot.

    The incident row lock is shared with adoption and recovery. Only the first
    terminal action is allowed to mutate the incident or its exclusions.
    """
    from . import db, wc_attributions

    with db.cursor() as cur:
        cur.execute(
            "SELECT id, day FROM machine_breakdowns "
            "WHERE id = %s AND resolved_at IS NULL FOR UPDATE",
            (incident_id,),
        )
        incident = cur.fetchone()
        if incident is None:
            return None
        cur.execute(
            "SELECT id, wc_name, person_name, employee_odoo_id, start_utc, "
            "end_utc, source, breakdown_id FROM wc_time_attributions "
            "WHERE breakdown_id = %s AND source = %s ORDER BY id FOR UPDATE",
            (incident_id, wc_attributions.BREAKDOWN_SOURCE),
        )
        snapshot = [
            {**dict(row), "day": incident["day"]} for row in cur.fetchall()
        ]
        cur.execute(
            "DELETE FROM wc_time_attributions "
            "WHERE breakdown_id = %s AND source = %s",
            (incident_id, wc_attributions.BREAKDOWN_SOURCE),
        )
        cur.execute(
            "UPDATE machine_breakdowns SET resolved_at = now(), "
            "resolution = 'dismissed', resume_utc = NULL WHERE id = %s",
            (incident_id,),
        )
        return snapshot


def finalize_recovered_incident(
    incident_id: int, resume_utc: datetime
) -> bool:
    """Atomically cap exclusions and resolve one still-open incident.

    Dismissal, undo/adoption, and recovery all take the incident lock first.
    Rows beginning at or after the first resumed output are invalid zero/late
    exclusions and are removed; earlier rows keep their earliest proven end.
    """
    from . import db, wc_attributions

    with db.cursor() as cur:
        cur.execute(
            "SELECT id FROM machine_breakdowns "
            "WHERE id = %s AND resolved_at IS NULL FOR UPDATE",
            (incident_id,),
        )
        if cur.fetchone() is None:
            return False
        cur.execute(
            "DELETE FROM wc_time_attributions "
            "WHERE breakdown_id = %s AND source = %s AND start_utc >= %s",
            (incident_id, wc_attributions.BREAKDOWN_SOURCE, resume_utc),
        )
        cur.execute(
            "UPDATE wc_time_attributions "
            "SET end_utc = LEAST(COALESCE(end_utc, %s), %s) "
            "WHERE breakdown_id = %s AND source = %s AND start_utc < %s",
            (
                resume_utc,
                resume_utc,
                incident_id,
                wc_attributions.BREAKDOWN_SOURCE,
                resume_utc,
            ),
        )
        cur.execute(
            "UPDATE machine_breakdowns SET resolved_at = now(), "
            "resolution = 'recovered', resume_utc = %s WHERE id = %s",
            (resume_utc, incident_id),
        )
        return True


def reopen_incident(incident_id: int) -> None:
    """Undo a resolution -- clears resolved_at/resolution/resume_utc so the
    incident is open again (dismiss-undo)."""
    from . import db
    db.execute(
        "UPDATE machine_breakdowns SET resolved_at = NULL, resolution = NULL, resume_utc = NULL "
        "WHERE id = %s",
        (incident_id,),
    )


def resolve_incident_operator_identity(
    incident: dict,
    person_name: str,
    employee_odoo_id: int | None,
) -> tuple[str, int | None]:
    """Resolve a transfer target from durable server-owned worker identity."""
    from . import wc_attributions

    durable = wc_attributions.breakdown_operator_rows_for_incident(incident["id"])

    def choose(rows) -> tuple[str, int | None] | None:
        matches = []
        for row in rows:
            row_name = str(row.get("person_name") or "").strip()
            row_id = row.get("employee_odoo_id")
            if employee_odoo_id is not None:
                if row_id == employee_odoo_id and row_name == person_name:
                    matches.append((row_name, int(row_id)))
            elif row_name == person_name:
                matches.append(
                    (row_name, int(row_id) if row_id is not None else None)
                )
        unique = list(dict.fromkeys(matches))
        return unique[0] if len(unique) == 1 else None

    resolved = choose(durable)
    if resolved is not None:
        return resolved
    durable_same_id = [
        row
        for row in durable
        if employee_odoo_id is not None
        and row.get("employee_odoo_id") == employee_odoo_id
    ]
    durable_same_name_ids = {
        row.get("employee_odoo_id")
        for row in durable
        if str(row.get("person_name") or "").strip() == person_name
        and row.get("employee_odoo_id") is not None
    }
    if durable_same_id or (
        durable_same_name_ids
        and employee_odoo_id not in durable_same_name_ids
    ):
        raise ValueError("worker identity does not match this breakdown")

    now = datetime.now(UTC)
    source = _operator_source_snapshot(incident["day"], now)
    if not source.available or not source.complete:
        raise ValueError("worker identity is not available")
    canonical = [
        {
            "person_name": presence.person_name,
            "employee_odoo_id": presence.employee_odoo_id,
        }
        for presence in source.presences
        if presence.wc_name == incident["wc_name"]
    ]
    resolved = choose(canonical)
    if resolved is None:
        raise ValueError("worker identity does not match this breakdown")
    return resolved


def snooze_operator(
    incident_id: int,
    person_name: str,
    minutes: int = BREAKDOWN_SNOOZE_MINUTES,
    *,
    employee_odoo_id: int | None = None,
) -> None:
    """Silence one operator's row on this incident's card for `minutes`."""
    from . import db
    until = datetime.now(UTC) + timedelta(minutes=minutes)
    db.execute(
        "INSERT INTO breakdown_snoozes "
        "(breakdown_id, person_name, employee_odoo_id, until_utc) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT ("
        "breakdown_id, "
        "(COALESCE('odoo:' || employee_odoo_id::text, 'name:' || person_name))"
        ") DO UPDATE SET "
        "until_utc = EXCLUDED.until_utc, person_name = EXCLUDED.person_name, "
        "created_at = now()",
        (incident_id, person_name, employee_odoo_id, until),
    )


def active_snooze_until(
    incident_id: int,
    person_name: str,
    *,
    employee_odoo_id: int | None = None,
) -> datetime | None:
    """The until_utc timestamp if this operator's snooze on this incident
    hasn't expired yet, else None."""
    from . import db
    if employee_odoo_id is None:
        identity_sql = "employee_odoo_id IS NULL AND person_name = %s"
        identity_params = (person_name,)
    else:
        identity_sql = "employee_odoo_id = %s"
        identity_params = (int(employee_odoo_id),)
    rows = db.query(
        "SELECT until_utc FROM breakdown_snoozes WHERE breakdown_id = %s "
        f"AND {identity_sql} AND until_utc > now()",
        (incident_id, *identity_params),
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


def _punch_windows_with_availability(day: date) -> tuple[dict, bool]:
    """Attendance windows plus whether their Odoo source was readable."""
    from . import timeclock_windows
    return timeclock_windows.attendance_windows_for_day_with_availability(day)


def _operator_source_from_legacy_windows(
    punch_windows: dict, now: datetime, *, available: bool
) -> OperatorSourceSnapshot:
    """Project the rollback attendance-window shape without changing it."""
    if not available:
        return OperatorSourceSnapshot((), (), False, False, False)
    presences: list[OperatorPresence] = []
    departures: list[OperatorDeparture] = []
    for person_name in sorted(punch_windows):
        for wc_name, start_utc, end_utc in punch_windows[person_name]:
            if start_utc > now:
                continue
            if end_utc is None:
                presences.append(
                    OperatorPresence(person_name, wc_name, start_utc, None)
                )
            elif end_utc <= now:
                departures.append(
                    OperatorDeparture(
                        person_name, wc_name, start_utc, end_utc, None
                    )
                )
    return OperatorSourceSnapshot(tuple(presences), tuple(departures), True, False, True)


def _operator_source_from_staffing_snapshot(snapshot) -> OperatorSourceSnapshot:
    """Use Task 11's atomic policy/timeline generation for breakdown reads."""
    policy = snapshot.policy
    if not policy.mirror_owned:
        return OperatorSourceSnapshot((), (), True, False, True)
    if not policy.available or policy.stale:
        return OperatorSourceSnapshot((), (), False, True, False)

    verified_cap = snapshot.verified_cap_utc
    current_attendance_ids = snapshot.current_attendance_ids
    presences: list[OperatorPresence] = []
    departures: list[OperatorDeparture] = []
    complete = True
    for span in snapshot.spans:
        is_current = bool(
            span.start_utc <= verified_cap < span.end_utc
            or (
                span.start_utc <= verified_cap == span.end_utc
                and current_attendance_ids.intersection(span.attendance_ids)
            )
        )
        if span.status == "exempt_no_location":
            # This worker's department intentionally has no station location,
            # so they are outside breakdown operator scope rather than an
            # incomplete source row.
            continue
        if span.status != "valid" or not span.app_work_center_name:
            if is_current:
                complete = False
            continue
        if is_current:
            presences.append(
                OperatorPresence(
                    span.employee_name,
                    span.app_work_center_name,
                    span.start_utc,
                    span.employee_odoo_id,
                )
            )
        elif span.start_utc < span.end_utc <= verified_cap:
            departures.append(
                OperatorDeparture(
                    span.employee_name,
                    span.app_work_center_name,
                    span.start_utc,
                    span.end_utc,
                    span.employee_odoo_id,
                )
            )
    def identity_order(value):
        return (
            value.employee_odoo_id is None,
            value.employee_odoo_id or 0,
            value.person_name,
            value.wc_name,
            value.arrival_utc,
        )
    return OperatorSourceSnapshot(
        tuple(sorted(presences, key=identity_order)),
        tuple(sorted(departures, key=identity_order)),
        True,
        True,
        complete,
    )


def _operator_source_snapshot(day: date, now: datetime) -> OperatorSourceSnapshot:
    """Freeze one canonical operator source for a complete breakdown read."""
    from .routes import staffing as staffing_routes

    staffing_snapshot = staffing_routes._read_staffing_response_snapshot(
        day, as_of_utc=now
    )
    if staffing_snapshot.policy.mirror_owned:
        return _operator_source_from_staffing_snapshot(staffing_snapshot)
    punch_windows, available = _punch_windows_with_availability(day)
    return _operator_source_from_legacy_windows(
        punch_windows, now, available=available
    )


def _present_operators_in_windows(
    wc_name: str, punch_windows: dict, now: datetime
) -> list[OperatorPresence]:
    """Rollback helper retained for callers that already froze legacy windows."""
    snapshot = _operator_source_from_legacy_windows(
        punch_windows, now, available=True
    )
    return [operator for operator in snapshot.presences if operator.wc_name == wc_name]


def _present_operators_on_wc(
    wc_name: str,
    day: date,
    now: datetime | None = None,
    operator_source: OperatorSourceSnapshot | None = None,
) -> list[OperatorPresence]:
    """Canonical current operators at this work center."""
    now = now or datetime.now(UTC)
    source = operator_source or _operator_source_snapshot(day, now)
    return [operator for operator in source.presences if operator.wc_name == wc_name]


def _station_signals(
    day: date,
    now: datetime,
    operator_source: OperatorSourceSnapshot | None = None,
) -> list[StationSignal]:
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
        has_operator = bool(
            _present_operators_on_wc(wc_name, day, now, operator_source)
        )
        out.append(
            StationSignal(
                wc_name=wc_name,
                last_output_utc=last_output,
                has_operator=has_operator,
                sample_times_utc=tuple(sample[0] for sample in total.samples),
            )
        )
    return out


def _last_output_after(
    wc_name: str,
    day: date,
    stop_utc: datetime,
    operator_source: OperatorSourceSnapshot | None = None,
) -> datetime | None:
    """The first output time for wc_name strictly after ``stop_utc``."""
    for sig in _station_signals(day, datetime.now(UTC), operator_source):
        if sig.wc_name != wc_name:
            continue
        resume = first_output_after(sig.sample_times_utc, stop_utc)
        if resume is not None:
            return resume
        if not sig.sample_times_utc and sig.last_output_utc and sig.last_output_utc > stop_utc:
            return sig.last_output_utc
    return None


def _last_output_before(
    wc_name: str,
    day: date,
    now: datetime,
    operator_source: OperatorSourceSnapshot | None = None,
) -> datetime | None:
    """The station's last output time as of `now` (or None if it hasn't
    produced today) -- used by the manual report button."""
    for sig in _station_signals(day, now, operator_source):
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
    from .plant_day import today as plant_today
    day = day or plant_today()
    now = now or datetime.now(UTC)
    shift_start, shift_end = _shift_bounds(day)
    operator_source = _operator_source_snapshot(day, now)
    operator_source_complete = operator_source.available and operator_source.complete

    try:
        open_incidents = all_open_incidents(day)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "machine breakdown: failed to load open incidents", exc_info=True)
        open_incidents = []

    for incident in open_incidents:
        try:
            if _maybe_auto_resolve(incident, day, now, operator_source):
                continue
            if operator_source_complete:
                _cap_departed_operators(incident, day, now, operator_source)
                _ensure_operator_breakdowns(incident, day, now, operator_source)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "machine breakdown tick failed for incident %s", incident["id"], exc_info=True)

    from . import shift_config
    candidates = detect(
        _station_signals(day, now, operator_source),
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
            if operator_source_complete:
                _ensure_operator_breakdowns(
                    {
                        "id": incident_id,
                        "wc_name": candidate.wc_name,
                        "day": day,
                        "detected_stop_utc": candidate.stop_utc,
                    },
                    day,
                    now,
                    operator_source,
                )
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "machine breakdown open failed for %s", candidate.wc_name, exc_info=True)


def _worker_reached_breakdown_threshold(
    day: date,
    station_stop_utc: datetime,
    operator: OperatorPresence,
    now: datetime,
) -> bool:
    from . import shift_config

    personal_start = personal_breakdown_start(
        station_stop_utc=station_stop_utc,
        arrival_utc=operator.arrival_utc,
    )
    if now <= personal_start:
        return False
    elapsed = shift_config.productive_minutes_in_window(day, personal_start, now)
    return elapsed >= BREAKDOWN_NO_OUTPUT_MINUTES


def _eligible_operator_presences(
    incident: dict,
    day: date,
    now: datetime,
    operator_source: OperatorSourceSnapshot,
) -> list[OperatorPresence]:
    return [
        operator
        for operator in operator_source.presences
        if operator.wc_name == incident["wc_name"]
        and _worker_reached_breakdown_threshold(
            day, incident["detected_stop_utc"], operator, now
        )
    ]


def _ensure_operator_breakdowns(
    incident: dict,
    day: date,
    now: datetime,
    operator_source: OperatorSourceSnapshot,
    *,
    end_cap: datetime | None = None,
) -> None:
    """Idempotently adopt current and fully-completed worker intervals."""
    from . import wc_attributions

    from . import shift_config

    identities_by_name: dict[str, set[int]] = {}
    for visit in (*operator_source.presences, *operator_source.departures):
        if visit.wc_name == incident["wc_name"] and visit.employee_odoo_id is not None:
            identities_by_name.setdefault(visit.person_name, set()).add(
                visit.employee_odoo_id
            )

    for departure in operator_source.departures:
        if departure.wc_name != incident["wc_name"]:
            continue
        personal_start = personal_breakdown_start(
            station_stop_utc=incident["detected_stop_utc"],
            arrival_utc=departure.arrival_utc,
        )
        personal_end = min(departure.departure_utc, end_cap or departure.departure_utc)
        if personal_end <= personal_start:
            continue
        elapsed = shift_config.productive_minutes_in_window(
            day, personal_start, personal_end
        )
        if elapsed < BREAKDOWN_NO_OUTPUT_MINUTES:
            continue
        wc_attributions.adopt_breakdown(
            day,
            incident["wc_name"],
            departure.person_name,
            personal_start,
            incident["id"],
            employee_odoo_id=departure.employee_odoo_id,
            end_utc=personal_end,
            allow_legacy_fallback=(
                departure.employee_odoo_id is not None
                and len(identities_by_name.get(departure.person_name, ())) == 1
            ),
        )

    for operator in _eligible_operator_presences(
        incident, day, now, operator_source
    ):
        existing = wc_attributions.open_breakdown_row(
            day,
            incident["wc_name"],
            operator.person_name,
            employee_odoo_id=operator.employee_odoo_id,
            breakdown_id=incident["id"],
            allow_legacy_fallback=(
                operator.employee_odoo_id is not None
                and len(identities_by_name.get(operator.person_name, ())) == 1
            ),
        )
        if existing is not None:
            continue
        personal_start = personal_breakdown_start(
            station_stop_utc=incident["detected_stop_utc"],
            arrival_utc=operator.arrival_utc,
        )
        wc_attributions.adopt_breakdown(
            day,
            incident["wc_name"],
            operator.person_name,
            personal_start,
            incident["id"],
            employee_odoo_id=operator.employee_odoo_id,
            allow_legacy_fallback=(
                operator.employee_odoo_id is not None
                and len(identities_by_name.get(operator.person_name, ())) == 1
            ),
        )


def _cap_departed_operators(
    incident: dict,
    day: date,
    now: datetime,
    operator_source: OperatorSourceSnapshot | None = None,
    *,
    cap_utc: datetime | None = None,
) -> None:
    """Cap any operator's open breakdown row the moment they leave the
    broken machine (transfer or self-punch-out) -- detected via their punch
    windows, not via the Transfer button (which caps immediately itself;
    this is the passive/punch-out path)."""
    from . import wc_attributions
    wc_name = incident["wc_name"]
    source = operator_source or _operator_source_snapshot(day, now)
    if not source.available:
        return
    identities_by_name: dict[str, set[int]] = {}
    for visit in (*source.presences, *source.departures):
        if visit.wc_name == wc_name and visit.employee_odoo_id is not None:
            identities_by_name.setdefault(visit.person_name, set()).add(
                visit.employee_odoo_id
            )
    for departure in source.departures:
        if departure.wc_name != wc_name:
            continue
        row = wc_attributions.open_breakdown_row(
            day,
            wc_name,
            departure.person_name,
            employee_odoo_id=departure.employee_odoo_id,
            breakdown_id=incident["id"],
            allow_legacy_fallback=(
                departure.employee_odoo_id is not None
                and len(identities_by_name.get(departure.person_name, ())) == 1
            ),
        )
        if row is None:
            continue
        departure_end = min(departure.departure_utc, cap_utc or departure.departure_utc)
        row_start = row.get("start_utc")
        if row_start is not None and departure_end <= row_start:
            continue
        wc_attributions.cap_breakdown(row["id"], departure_end)


def _maybe_auto_resolve(
    incident: dict,
    day: date,
    now: datetime,
    operator_source: OperatorSourceSnapshot | None = None,
) -> bool:
    """Resolve an incident as 'recovered' once its station has produced
    output again, capping any operator still open at the resume time."""
    source = operator_source or _operator_source_snapshot(day, now)
    resume = _last_output_after(
        incident["wc_name"], day, incident["detected_stop_utc"], source
    )
    if resume is None:
        return False
    if source.available and source.complete:
        _ensure_operator_breakdowns(
            incident,
            day,
            resume,
            source,
            end_cap=resume,
        )
    return finalize_recovered_incident(incident["id"], resume)


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
    incidents = all_open_incidents(day)
    if not incidents:
        return BreakdownRows([], complete=True)
    operator_source = _operator_source_snapshot(day, now)

    rows: list[dict] = []
    for incident in incidents:
        wc_name = incident["wc_name"]
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
        if not operator_source.available:
            continue
        operators = _eligible_operator_presences(
            incident, day, now, operator_source
        )
        for operator in operators:
            person = operator.person_name
            identity = operator.employee_odoo_id or person
            snoozed_until = active_snooze_until(
                incident["id"],
                person,
                employee_odoo_id=operator.employee_odoo_id,
            )
            item_key = inbox_keys.breakdown(
                wc_name, stop_iso, person, operator.employee_odoo_id
            )
            if snoozed_until is not None:
                mins_left = max(1, int((snoozed_until - now).total_seconds() // 60))
                rows.append({
                    "name": person,
                    "label": "Snoozed",
                    "detail": f"Re-checks in {mins_left} min",
                    "priority": "muted",
                    "badge": "Follow-up",
                    "row_key": f"breakdown_snoozed:{wc_name}:{stop_iso}:{identity}",
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
                "row_key": f"breakdown_op:{wc_name}:{stop_iso}:{identity}",
                "item_key": item_key,
                "action": {
                    "type": "breakdown",
                    "incident_id": incident["id"],
                    "person_name": person,
                    "wc_name": wc_name,
                    "employee_odoo_id": operator.employee_odoo_id,
                },
            })
    return BreakdownRows(
        rows, complete=operator_source.available and operator_source.complete
    )


def _local_time_label(dt: datetime) -> str:
    import os
    from .shift_config import SITE_TZ
    local = dt.astimezone(SITE_TZ)
    fmt = "%#I:%M %p" if os.name == "nt" else "%-I:%M %p"
    return local.strftime(fmt)


def report_manual(wc_name: str, day: date | None = None, now: datetime | None = None) -> dict:
    """Open (or find) a breakdown incident for wc_name on demand -- the
    "+ Report a breakdown" button. Returns {ok, incident_id, already_open?}."""
    from .plant_day import today as plant_today
    day = day or plant_today()
    now = now or datetime.now(UTC)

    existing = get_open_incident(wc_name, day)
    if existing is not None:
        return {"ok": True, "incident_id": existing["id"], "already_open": True}

    operator_source = _operator_source_snapshot(day, now)
    stop = _last_output_before(wc_name, day, now, operator_source) or now
    incident_id = open_incident(wc_name, day, stop, source="manual")
    if operator_source.available and operator_source.complete:
        _ensure_operator_breakdowns(
            {
                "id": incident_id,
                "wc_name": wc_name,
                "day": day,
                "detected_stop_utc": stop,
            },
            day,
            now,
            operator_source,
        )
    return {"ok": True, "incident_id": incident_id}
