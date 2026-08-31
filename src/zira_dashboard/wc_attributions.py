"""Retro time-windowed WC attribution for production that happened at
unscheduled work centers.

When a metered work center produced units on a given day but had no one
scheduled there, we let the user retroactively attribute the production to
the person who actually worked it. The attribution flows through to
leaderboards and dashboards via ``production_history.attribute_for_day``'s
``extra_assignments`` parameter.
"""

from __future__ import annotations

from datetime import date, datetime, UTC

TESTING_PERSON = "Testing"
"""Sentinel person_name for ``source='testing'`` rows. These rows mark a
window whose production is credited to no one; they are never fed into
crediting as operators."""

TESTING_SOURCE = "testing"
"""``wc_time_attributions.source`` value marking a no-credit testing segment.
Referenced by the write side (routes) and both read-side filters so the string
can't drift out of sync."""

BREAKDOWN_SOURCE = "breakdown"
"""``wc_time_attributions.source`` value marking a machine-breakdown exclusion
window for one operator. Like ``TESTING_SOURCE``, these rows are excluded from
crediting (``people_by_wc`` / ``creditable_for_day``) -- they exist only to
carry excluded-minutes math (``breakdown_windows_for_day``), the mirror of how
testing rows carry no-credit unit offsets."""


def add(
    day: date,
    wc_name: str,
    person_name: str,
    start_utc: datetime,
    end_utc: datetime | None = None,
    source: str = "manual",
    breakdown_id: int | None = None,
    employee_odoo_id: int | None = None,
) -> int:
    """Insert one attribution row. `end_utc=None` means the assignment is
    OPEN -- it stays running until the person clocks out, transfers, or is
    reassigned (resolved downstream by assignment_windows). `breakdown_id`
    links a source=BREAKDOWN_SOURCE row back to the machine_breakdowns
    incident that created it. Returns row id."""
    from . import db

    rows = db.query(
        "INSERT INTO wc_time_attributions "
        "(day, wc_name, person_name, start_utc, end_utc, source, breakdown_id, "
        "employee_odoo_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            day,
            wc_name,
            person_name,
            start_utc,
            end_utc,
            source,
            breakdown_id,
            employee_odoo_id,
        ),
    )
    return rows[0]["id"] if rows else 0


def for_day(day: date) -> list[dict]:
    """All attributions for a day. Returns list of dicts with keys
    id, wc_name, person_name, start_utc, end_utc, source."""
    from . import db

    return db.query(
        "SELECT id, wc_name, person_name, employee_odoo_id, start_utc, end_utc, "
        "source, breakdown_id "
        "FROM wc_time_attributions WHERE day = %s ORDER BY wc_name, start_utc",
        (day,),
    )


def people_by_wc(day: date, rows: list[dict] | None = None) -> dict[str, list[str]]:
    """Aggregated view: ``{wc_name: [person, ...]}`` -- convenience for joining
    into ``attribute_for_day``'s assignments dict. Excludes ``source='testing'``
    rows so a testing window never becomes a credited operator.

    Swallows DB errors (e.g. Postgres unreachable) so callers in hot paths
    like leaderboards keep working. Pass already-fetched ``rows`` (from
    ``for_day``) to skip the re-query (see ``unattributed_for_day``).
    """
    if rows is None:
        try:
            rows = for_day(day)
        except Exception:
            return {}
    out: dict[str, list[str]] = {}
    for r in rows:
        if r.get("source") in (TESTING_SOURCE, BREAKDOWN_SOURCE):
            continue
        out.setdefault(r["wc_name"], []).append(r["person_name"])
    return out


def testing_windows_for_day(day: date, rows: list[dict] | None = None) -> dict[str, list[tuple]]:
    """``{wc_name: [(start_utc, end_utc), ...]}`` for ``source='testing'``
    rows. Swallows DB errors like ``people_by_wc``; same optional ``rows``."""
    if rows is None:
        try:
            rows = for_day(day)
        except Exception:
            return {}
    out: dict[str, list[tuple]] = {}
    for r in rows:
        if r.get("source") != TESTING_SOURCE:
            continue
        out.setdefault(r["wc_name"], []).append((r["start_utc"], r["end_utc"]))
    return out


def breakdown_windows_for_day(
    day: date, rows: list[dict] | None = None
) -> dict[tuple, list[tuple]]:
    """Breakdown windows keyed by immutable identity when it is available.

    New rows use ``(employee_odoo_id, person_name, wc_name)``. Legacy null-ID
    rows keep ``(person_name, wc_name)``. Swallows DB errors like
    :func:`people_by_wc`; pass ``rows`` to skip a re-query.
    """
    if rows is None:
        try:
            rows = for_day(day)
        except Exception:
            return {}
    out: dict[tuple, list[tuple]] = {}
    for r in rows:
        if r.get("source") != BREAKDOWN_SOURCE:
            continue
        employee_odoo_id = r.get("employee_odoo_id")
        key = (
            (employee_odoo_id, r["person_name"], r["wc_name"])
            if employee_odoo_id is not None
            else (r["person_name"], r["wc_name"])
        )
        out.setdefault(key, []).append((r["start_utc"], r.get("end_utc")))
    return out


def creditable_for_day(day: date) -> list[dict]:
    """Attributions for ``day`` EXCLUDING no-credit testing rows and
    breakdown-exclusion rows (``source in (TESTING_SOURCE, BREAKDOWN_SOURCE)``).
    This is the set that should drive both credited operators and dashboard
    GOALS -- neither a testing window nor a breakdown-exclusion window should
    inflate a goal or appear as a credited operator. Mirrors the testing
    filter in ``people_by_wc``."""
    return [r for r in for_day(day) if r.get("source") not in (TESTING_SOURCE, BREAKDOWN_SOURCE)]


def delete(attribution_id: int) -> None:
    from . import db

    db.execute("DELETE FROM wc_time_attributions WHERE id = %s", (attribution_id,))


def add_breakdown(
    day: date,
    wc_name: str,
    person_name: str,
    start_utc: datetime,
    breakdown_id: int,
    *,
    employee_odoo_id: int | None = None,
) -> int:
    """Open a new breakdown exclusion window for one operator. end_utc is
    left NULL (open) until the operator leaves the machine (see
    cap_breakdown)."""
    return _add_breakdown_window(
        day,
        wc_name,
        person_name,
        start_utc,
        None,
        breakdown_id,
        employee_odoo_id=employee_odoo_id,
    )


def add_completed_breakdown(
    day: date,
    wc_name: str,
    person_name: str,
    start_utc: datetime,
    breakdown_id: int,
    *,
    end_utc: datetime,
    employee_odoo_id: int | None = None,
) -> int:
    """Idempotently persist one completed worker visit for a breakdown."""
    if end_utc <= start_utc:
        return 0
    return _add_breakdown_window(
        day,
        wc_name,
        person_name,
        start_utc,
        end_utc,
        breakdown_id,
        employee_odoo_id=employee_odoo_id,
    )


def _add_breakdown_window(
    day: date,
    wc_name: str,
    person_name: str,
    start_utc: datetime,
    end_utc: datetime | None,
    breakdown_id: int,
    *,
    employee_odoo_id: int | None,
) -> int:
    """Insert only while the incident is still open.

    Locking the incident row serializes this insert with dismissal's resolve
    transaction.  The exact-visit indexes make delayed/retried completed
    spans converge on one row, while the existing open indexes retain one
    active visit per immutable worker.
    """
    from . import db

    rows = db.query(
        "WITH open_incident AS MATERIALIZED ("
        "SELECT id FROM machine_breakdowns "
        "WHERE id = %s AND resolved_at IS NULL FOR UPDATE"
        ") INSERT INTO wc_time_attributions "
        "(day, wc_name, person_name, start_utc, end_utc, source, breakdown_id, "
        "employee_odoo_id) "
        "SELECT %s, %s, %s, %s, %s, %s, open_incident.id, %s "
        "FROM open_incident ON CONFLICT DO NOTHING RETURNING id",
        (
            breakdown_id,
            day,
            wc_name,
            person_name,
            start_utc,
            end_utc,
            BREAKDOWN_SOURCE,
            employee_odoo_id,
        ),
    )
    if rows:
        return rows[0]["id"]
    existing = breakdown_row_for_visit(
        day,
        wc_name,
        person_name,
        start_utc,
        breakdown_id,
        employee_odoo_id=employee_odoo_id,
    )
    return existing["id"] if existing is not None else 0


def cap_breakdown(attribution_id: int, end_utc: datetime) -> None:
    """Close an open breakdown row at the operator's departure/incident-
    resolution time. No-op if already closed (idempotent against a
    detection tick re-processing the same incident)."""
    from . import db

    db.execute(
        "UPDATE wc_time_attributions "
        "SET end_utc = LEAST(COALESCE(end_utc, %s), %s) "
        "WHERE id = %s AND source = %s",
        (end_utc, end_utc, attribution_id, BREAKDOWN_SOURCE),
    )


def normalize_breakdown_visit(
    stale_row_id: int,
    breakdown_id: int,
    person_name: str,
    start_utc: datetime,
    *,
    employee_odoo_id: int | None = None,
    end_utc: datetime | None = None,
) -> int | None:
    """Normalize one pre-upgrade row without colliding with an exact visit.

    The incident lock is the common serialization boundary for detector adds,
    dismissal, and recovery.  If the canonical visit already exists, discard
    only the stale row for the same immutable identity and converge on that
    exact row.  ``end_utc=None`` means the visit is current and must be open;
    completed visits retain the earliest verified departure.
    """
    from . import db

    with db.cursor() as cur:
        cur.execute(
            "SELECT id FROM machine_breakdowns "
            "WHERE id = %s AND resolved_at IS NULL FOR UPDATE",
            (breakdown_id,),
        )
        if cur.fetchone() is None:
            return None

        if employee_odoo_id is None:
            identity_clause = "employee_odoo_id IS NULL AND person_name = %s"
            identity_params = (person_name,)
        else:
            identity_clause = "employee_odoo_id = %s"
            identity_params = (employee_odoo_id,)

        cur.execute(
            "SELECT id FROM wc_time_attributions "
            "WHERE breakdown_id = %s AND source = %s AND start_utc = %s "
            f"AND {identity_clause} AND id <> %s FOR UPDATE",
            (
                breakdown_id,
                BREAKDOWN_SOURCE,
                start_utc,
                *identity_params,
                stale_row_id,
            ),
        )
        exact = cur.fetchone()
        if exact is not None:
            cur.execute(
                "DELETE FROM wc_time_attributions "
                "WHERE id = %s AND breakdown_id = %s AND source = %s "
                f"AND {identity_clause}",
                (
                    stale_row_id,
                    breakdown_id,
                    BREAKDOWN_SOURCE,
                    *identity_params,
                ),
            )
            if end_utc is None:
                cur.execute(
                    "UPDATE wc_time_attributions "
                    "SET person_name = %s, end_utc = NULL "
                    "WHERE id = %s AND breakdown_id = %s AND source = %s",
                    (
                        person_name,
                        exact["id"],
                        breakdown_id,
                        BREAKDOWN_SOURCE,
                    ),
                )
            else:
                cur.execute(
                    "UPDATE wc_time_attributions "
                    "SET person_name = %s, "
                    "end_utc = LEAST(COALESCE(end_utc, %s), %s) "
                    "WHERE id = %s AND breakdown_id = %s AND source = %s",
                    (
                        person_name,
                        end_utc,
                        end_utc,
                        exact["id"],
                        breakdown_id,
                        BREAKDOWN_SOURCE,
                    ),
                )
            return exact["id"]

        if end_utc is None:
            update = "SET person_name = %s, start_utc = %s, end_utc = NULL "
            update_params = (person_name, start_utc)
        else:
            update = (
                "SET person_name = %s, start_utc = %s, "
                "end_utc = LEAST(COALESCE(end_utc, %s), %s) "
            )
            update_params = (person_name, start_utc, end_utc, end_utc)
        cur.execute(
            "UPDATE wc_time_attributions "
            f"{update}"
            "WHERE id = %s AND breakdown_id = %s AND source = %s "
            f"AND {identity_clause} RETURNING id",
            (
                *update_params,
                stale_row_id,
                breakdown_id,
                BREAKDOWN_SOURCE,
                *identity_params,
            ),
        )
        normalized = cur.fetchone()
        return normalized["id"] if normalized is not None else None


def reopen_breakdown(attribution_id: int) -> None:
    """Undo a cap: clear end_utc so the window is open again (breakdown
    transfer-undo)."""
    from . import db

    db.execute(
        "UPDATE wc_time_attributions SET end_utc = NULL WHERE id = %s AND source = %s",
        (attribution_id, BREAKDOWN_SOURCE),
    )


def open_breakdown_row(
    day: date,
    wc_name: str,
    person_name: str,
    *,
    employee_odoo_id: int | None = None,
    allow_legacy_fallback: bool = False,
) -> dict | None:
    """The operator's currently-OPEN breakdown row for (day, wc_name), if
    any. Returns {id, start_utc} or None. Used by the detection tick to find
    the row to cap when an operator leaves the machine."""
    from . import db

    if employee_odoo_id is None:
        rows = db.query(
            "SELECT id, start_utc FROM wc_time_attributions "
            "WHERE day = %s AND wc_name = %s AND person_name = %s "
            "AND employee_odoo_id IS NULL AND source = %s AND end_utc IS NULL",
            (day, wc_name, person_name, BREAKDOWN_SOURCE),
        )
    else:
        rows = db.query(
            "SELECT id, start_utc FROM wc_time_attributions "
            "WHERE day = %s AND wc_name = %s AND employee_odoo_id = %s "
            "AND source = %s AND end_utc IS NULL",
            (day, wc_name, employee_odoo_id, BREAKDOWN_SOURCE),
        )
        if not rows and allow_legacy_fallback:
            rows = db.query(
                "SELECT id, start_utc FROM wc_time_attributions "
                "WHERE day = %s AND wc_name = %s AND person_name = %s "
                "AND employee_odoo_id IS NULL AND source = %s AND end_utc IS NULL",
                (day, wc_name, person_name, BREAKDOWN_SOURCE),
            )
            if rows:
                claimed = db.query(
                    "UPDATE wc_time_attributions SET employee_odoo_id = %s "
                    "WHERE id = %s AND employee_odoo_id IS NULL "
                    "RETURNING id, start_utc",
                    (employee_odoo_id, rows[0]["id"]),
                )
                rows = claimed
    return rows[0] if rows else None


def breakdown_row_for_visit(
    day: date,
    wc_name: str,
    person_name: str,
    start_utc: datetime,
    breakdown_id: int,
    *,
    employee_odoo_id: int | None = None,
) -> dict | None:
    """One exact breakdown visit, whether still open or already capped."""
    from . import db

    if employee_odoo_id is None:
        rows = db.query(
            "SELECT id, start_utc, end_utc FROM wc_time_attributions "
            "WHERE day = %s AND wc_name = %s AND person_name = %s "
            "AND employee_odoo_id IS NULL AND start_utc = %s "
            "AND breakdown_id = %s AND source = %s",
            (
                day,
                wc_name,
                person_name,
                start_utc,
                breakdown_id,
                BREAKDOWN_SOURCE,
            ),
        )
    else:
        rows = db.query(
            "SELECT id, start_utc, end_utc FROM wc_time_attributions "
            "WHERE day = %s AND wc_name = %s AND employee_odoo_id = %s "
            "AND start_utc = %s AND breakdown_id = %s AND source = %s",
            (
                day,
                wc_name,
                employee_odoo_id,
                start_utc,
                breakdown_id,
                BREAKDOWN_SOURCE,
            ),
        )
    return rows[0] if rows else None


def _restore_breakdown_snapshot(cursor, rows: list[dict], breakdown_id: int) -> None:
    """Insert an exact dismiss snapshot through the caller's transaction."""
    for row in rows:
        row_breakdown_id = row.get("breakdown_id", breakdown_id)
        if row_breakdown_id != breakdown_id:
            raise ValueError("breakdown snapshot contains another incident")
        source = row.get("source", BREAKDOWN_SOURCE)
        if source != BREAKDOWN_SOURCE:
            raise ValueError("breakdown snapshot contains another source")
        cursor.execute(
            "INSERT INTO wc_time_attributions "
            "(day, wc_name, person_name, start_utc, end_utc, source, "
            "breakdown_id, employee_odoo_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (
                row["day"],
                row["wc_name"],
                row["person_name"],
                row["start_utc"],
                row.get("end_utc"),
                source,
                breakdown_id,
                row.get("employee_odoo_id"),
            ),
        )


def restore_breakdown_snapshot(rows: list[dict], breakdown_id: int) -> None:
    """Restore an exact snapshot in one transaction for legacy callers."""
    from . import db

    with db.cursor() as cursor:
        _restore_breakdown_snapshot(cursor, rows, breakdown_id)


def open_breakdown_rows_for_incident(breakdown_id: int) -> list[dict]:
    """Every still-open exclusion linked to one station incident."""
    from . import db

    return db.query(
        "SELECT id, person_name, employee_odoo_id, start_utc "
        "FROM wc_time_attributions WHERE breakdown_id = %s "
        "AND source = %s AND end_utc IS NULL ORDER BY id",
        (breakdown_id, BREAKDOWN_SOURCE),
    )


def delete_breakdown_rows_for_incident(breakdown_id: int) -> None:
    """Delete every wc_time_attributions row tied to one breakdown incident
    -- the "Not a breakdown" dismiss, which restores normal averages by
    removing the exclusion entirely."""
    from . import db

    db.execute(
        "DELETE FROM wc_time_attributions WHERE breakdown_id = %s AND source = %s",
        (breakdown_id, BREAKDOWN_SOURCE),
    )


UNATTRIBUTED_MIN_UNITS = 5
"""WCs with units at or below this threshold are skipped — could be a stray
sample / fluke and shouldn't surface as work to attribute. Matches the
dashboards' existing ACTIVE_UNITS_THRESHOLD."""


def unattributed_for_day(day: date, client) -> list[dict]:
    """Walk metered WCs for ``day``. Return rows for WCs that:
      1. Produced more than UNATTRIBUTED_MIN_UNITS (filters flukes)
      2. Are NOT in the schedule's assignments
      3. Are NOT in the attributions table

    Each result dict: ``{wc_name, units, first_sample_utc, last_sample_utc}``.
    """
    from . import staffing
    from . import leaderboard as _lb
    from .stations import STATIONS

    sched = staffing.load_schedule(day)
    scheduled_wcs = {
        wc for wc, ops in sched.assignments.items() if ops and wc != staffing.TIME_OFF_KEY
    }
    # One for_day fetch shared by both shapes (each used to re-query).
    try:
        att_rows = for_day(day)
    except Exception:
        att_rows = []
    attributed_wcs = set(people_by_wc(day, rows=att_rows).keys()) | set(
        testing_windows_for_day(day, rows=att_rows).keys()
    )

    # STATIONS uses short names (e.g., "Trim Saw", "Junior 2") while
    # LOCATIONS / schedules use the WC display name (e.g., "Trim Saw 1",
    # "Junior #2"). Map by meter_id so a schedule on "Trim Saw 1" is
    # recognized for the station with the matching meter.
    meter_to_loc_name = {loc.meter_id: loc.name for loc in staffing.LOCATIONS if loc.meter_id}

    # All metered work centers, regardless of cell. Production at any metered
    # WC without a schedule entry deserves to surface as a todo (Junior 2,
    # Trim Saw, etc., not just Recycling-cell stations).
    stations = [s for s in STATIONS if s.meter_id]
    # Don't pass now_utc for past days; for today use now.
    from .plant_day import today as plant_today

    today = plant_today()
    now_arg = datetime.now(UTC) if day == today else None
    results = _lb.cached_leaderboard(client, stations, day, now_utc=now_arg)

    out: list[dict] = []
    for r in results:
        if r.units <= UNATTRIBUTED_MIN_UNITS:
            continue
        # Use the LOCATION display name when available (matches the schedule).
        wc = meter_to_loc_name.get(r.station.meter_id, r.station.name)
        if wc in scheduled_wcs or wc in attributed_wcs:
            continue
        # Pull first/last sample times from active_intervals for time bounds.
        ais = r.active_intervals
        if not ais:
            continue
        first_utc = min(s for s, _ in ais)
        last_utc = max(e for _, e in ais)
        out.append(
            {
                "wc_name": wc,
                "units": int(r.units),
                "first_sample_utc": first_utc,
                "last_sample_utc": last_utc,
            }
        )
    return out


def shadow_unassigned_runs_for_day(
    day: date,
    client,
    *,
    now_utc: datetime,
):
    """Compute Task 5's strict run projection without changing saved credit.

    Normal shadow days intentionally remain on the legacy matcher, so the
    strict public day wrapper returns no rows. This read-only comparison uses
    the same normalized strict inputs, exclusions, and pure run grouper as the
    live wrapper. Task 13 will persist aggregate comparison health; this
    boundary only supplies today's visible comparison rows.
    """
    from . import production_history

    inputs = production_history._strict_inputs_for_day(  # noqa: SLF001
        day, client, now_utc=now_utc
    )
    _shift_start, shift_end = production_history._strict_shift_bounds(day)  # noqa: SLF001
    runs = []
    for wc_name, samples in inputs.samples_by_wc.items():
        exclusions = [*inputs.break_windows]
        exclusions.extend(inputs.testing_windows.get(wc_name, ()))
        for breakdown_key, windows in inputs.breakdown_windows.items():
            breakdown_wc = breakdown_key[-1]
            if breakdown_wc != wc_name:
                continue
            exclusions.extend(
                (start, end if end is not None else min(now_utc, shift_end))
                for start, end in windows
            )
        active_intervals = production_history._subtract_intervals(  # noqa: SLF001
            inputs.active_intervals_by_wc.get(wc_name, ()), exclusions
        )
        assigned_times = production_history._assigned_sample_times(  # noqa: SLF001
            samples, inputs.segments, wc_name
        )
        runs.extend(
            production_history.unassigned_runs_for_samples(
                samples,
                assigned_times,
                active_intervals,
                wc_name=wc_name,
            )
        )
    return tuple(sorted(runs, key=lambda run: (run.start_utc, run.wc_name)))
