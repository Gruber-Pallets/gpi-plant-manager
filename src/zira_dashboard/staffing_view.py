"""Pure render-model builder for the staffing scheduler page.

Builds the per-work-center "bays" render model and its companion
left-rail lists (Unscheduled / Reserves / Time Off) for GET /staffing.

Pure in the same sense as ``wc_dashboard_data.py``: no FastAPI / Request /
template imports, and no DB / Odoo / live_cache / attendance / scheduler I/O
of its own. The route does all the I/O (roster, schedule, Odoo time-off,
attendance) and passes the results in; this module only reshapes them. The
only collaborators are the pure helpers on ``staffing`` (LOCATIONS,
TIME_OFF_KEY, BAY_SUBTITLES, skill_color, present_operators) and the
config pass-throughs on ``work_centers_store`` (required_skills / min_ops /
max_ops / default_people) — exactly the surface ``wc_dashboard_data.py``
already leans on and that the staffing tests monkeypatch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from .attendance_timeline import LocationSpan, LocationStatus


@dataclass(frozen=True)
class StaffingPersonLocation:
    employee_odoo_id: int
    person_name: str
    planned_work_center: str | None
    live_work_center: str | None
    raw_odoo_work_center: str | None
    status: LocationStatus
    since_utc: datetime
    source_fresh_at: datetime | None

    @property
    def working_elsewhere(self) -> bool:
        return bool(
            self.status == "valid"
            and self.planned_work_center
            and self.live_work_center
            and self.planned_work_center != self.live_work_center
        )

    @property
    def display_text(self) -> str:
        if self.status == "valid":
            return self.live_work_center or "Location unavailable"
        if self.status == "unmapped_location":
            raw = self.raw_odoo_work_center or "Unknown Odoo location"
            return f"{raw} · Odoo only — mapping needed"
        if self.status == "pending_first_location":
            return "Waiting for Odoo location"
        if self.status == "missing_required_location":
            return "Location missing"
        if self.status == "conflicting_location":
            return "Location conflict"
        if self.status == "exempt_no_location":
            return "Outside work-center bays"
        if self.status == "stale_open_location":
            raw = self.raw_odoo_work_center or self.live_work_center or "Last known location"
            return f"{raw} · stale"
        return "Location unavailable"


def build_live_locations(
    planned_by_wc: Mapping[str, Sequence[str]],
    spans: Sequence[LocationSpan],
    *,
    as_of_utc: datetime,
    planned_employee_ids: Mapping[str, int] | None = None,
) -> tuple[StaffingPersonLocation, ...]:
    """Overlay one current projected location without moving planned seats."""
    if as_of_utc.utcoffset() is None:
        raise ValueError("as_of_utc must be timezone-aware")
    planned_employee_ids = planned_employee_ids or {}
    planned_by_id: dict[int, str] = {}
    planned_name_by_id: dict[int, str] = {}
    planned_order: list[int] = []
    for work_center, names in planned_by_wc.items():
        for name in names:
            employee_id = planned_employee_ids.get(name)
            if employee_id is not None and employee_id not in planned_by_id:
                planned_by_id[employee_id] = work_center
                planned_name_by_id[employee_id] = name
                planned_order.append(employee_id)

    current_by_id: dict[int, LocationSpan] = {}
    for span in spans:
        if not span.start_utc <= as_of_utc < span.end_utc:
            continue
        previous = current_by_id.get(span.employee_odoo_id)
        if previous is None or (span.start_utc, span.end_utc) > (
            previous.start_utc,
            previous.end_utc,
        ):
            current_by_id[span.employee_odoo_id] = span

    employee_ids = planned_order + sorted(
        (
            employee_id
            for employee_id in current_by_id
            if employee_id not in planned_by_id
        ),
        key=lambda employee_id: (
            current_by_id[employee_id].employee_name.lower(),
            employee_id,
        ),
    )
    result = []
    for employee_id in employee_ids:
        span = current_by_id.get(employee_id)
        if span is None:
            continue
        result.append(
            StaffingPersonLocation(
                employee_odoo_id=employee_id,
                person_name=planned_name_by_id.get(employee_id, span.employee_name),
                planned_work_center=planned_by_id.get(employee_id),
                live_work_center=span.app_work_center_name,
                raw_odoo_work_center=span.odoo_work_center_name,
                status=span.status,
                since_utc=span.start_utc,
                source_fresh_at=as_of_utc,
            )
        )
    return tuple(result)


def build_staffing_bays(
    roster, sched, time_off_entries, publish_blocked, enabled_work_centers=None,
    saturday_commitments=None, saturday_shift=None, saturday_availability_overrides=None,
    publish_errors=None, optional_commitments=None, training_reservations_by_center=None,
):
    """Build the per-work-center render model from already-fetched inputs.

    Parameters (all supplied by the route after its I/O completes):
      roster:            list[staffing.Person] — full roster (active + inactive).
      sched:             staffing.Schedule for the day (``.assignments``,
                         ``.wc_notes``); assignments are already snapshot-swapped
                         / default-seeded by the route before this is called.
      time_off_entries:  list[dict] from the Odoo-backed scheduler_time_off
                         mirror — full-day entries have ``hours is None``;
                         partials carry a numeric off-span.
      publish_blocked:   truthy only on the bounce-back after a failed publish;
                         gates ``publish_block_reasons``.
      enabled_work_centers:
                         work centers currently On in the scheduler. Disabled
                         centers do not participate in publish minimum checks.
      optional_commitments:
                         people available for an optional Saturday or holiday.
                         ``saturday_commitments`` remains a temporary alias;
                         callers must not supply both.
      training_reservations_by_center:
                         active level-zero trainee names keyed by their exact
                         protocol work center. These names stay visible in
                         that center's picker while training is active.

    Returns a dict of exactly the bands-A+B context keys the route merges
    into its TemplateResponse: bays, publish_block_reasons, defaults_by_loc,
    unassigned, reserves, time_off_names, time_off_entries,
    partial_hours_by_name, partial_range_by_name, partial_clear_by_name,
    people_meta, all_active_people.
    """
    from . import staffing, work_centers_store

    # Full-day absences drive BOTH the Time Off panel and the roster-availability
    # exclusion. Partial-day people are deliberately NOT treated as "in the Time
    # Off section": they stay in the assignable pool / Unscheduled list (badged
    # with their off-window) so they can still be scheduled around their partial.
    # Full-day entries have hours=None; partials carry a numeric off-span
    # (see scheduler_time_off).
    full_day_entries = [e for e in time_off_entries if e.get("hours") is None]
    time_off_set = {e["name"] for e in full_day_entries}

    active_people = [p for p in roster if p.active]
    all_by_name = {p.name: p for p in roster}
    training_reservations_by_center = training_reservations_by_center or {}

    all_active_people = sorted(p.name for p in active_people)

    # Per-person hours-off-today (for partial entries) so the scheduler
    # can show a badge next to their name. Full days carry hours=None (the
    # sync layer normalizes whole-shift windows to full_day), so any positive
    # off-span is a genuine partial — no shift-length threshold needed.
    partial_hours_by_name: dict[str, float] = {
        e["name"]: e["hours"]
        for e in time_off_entries
        if e.get("hours") is not None and e["hours"] > 0
    }
    # Badge text prefers the shaped timing label ("arrives 11:30am" /
    # "leaves 2:00pm" / "gone 10:00am–12:00pm") — a bare off-window range is
    # ambiguous about whether it's the hours here or the hours gone.
    partial_range_by_name: dict[str, str] = {
        e["name"]: (e.get("timing_label") or e["time_range"])
        for e in time_off_entries
        if e.get("time_range") and e.get("hours") is not None and e["hours"] > 0
    }
    # Per-partial clear key. Every partial gets a × button; the value
    # carries either a request_id (StratusTime time-off request path)
    # or an emp_id (StratusTime non-work-shift path) so the JS can hit
    # the right backend route. Derived/manual absences are full-day
    # and don't appear here.
    partial_clear_by_name: dict[str, dict] = {}
    for e in time_off_entries:
        if e.get("hours") is None or e["hours"] <= 0:
            continue
        key: dict = {}
        if e.get("request_id"):
            key["request_id"] = int(e["request_id"])
        elif e.get("emp_id"):
            key["emp_id"] = str(e["emp_id"])
        if key:
            partial_clear_by_name[e["name"]] = key

    _options_cache: dict[tuple[str, ...], list[dict]] = {}

    def options_for(required: tuple[str, ...]) -> list[dict]:
        """All active people, tagged with trained = (level >= 1 in ALL required skills).
        Untrained people are hidden client-side unless the WC's per-row Training
        checkbox is ticked. Reserves are tagged so they can be split into a
        secondary picker section (office/manager pool, only used when short).

        Memoized within this request — many WCs share the same `required`
        tuple, so we compute each unique skill set only once."""
        cached = _options_cache.get(required)
        if cached is not None:
            return cached
        rows = []
        for p in active_people:
            if required:
                levels = [p.level(s) for s in required]
                min_lvl = min(levels)
                trained = all(l >= 1 for l in levels)
                color = staffing.skill_color(min_lvl)
            else:
                # No required skills → don't color-code; everyone is a
                # valid option. lvl-2 CSS class renders as a neutral pill.
                min_lvl = 2
                trained = True
                color = "neutral"
            rows.append({
                "name": p.name,
                "level": min_lvl,
                "color": color,
                "trained": trained,
                "reserve": p.reserve,
            })
        _options_cache[required] = rows
        return rows

    if optional_commitments is not None and saturday_commitments is not None:
        raise ValueError(
            "Pass optional_commitments or saturday_commitments, not both."
        )

    # Optional-workday recruiting deliberately begins with the plant closed.
    # Only volunteers are allowed into the staffing grid; stale draft
    # placements must not make a non-volunteer look scheduled.  The returned
    # Saturday-named keys remain compatibility contracts for the template and
    # browser code while callers migrate to the date-neutral input.
    commitments = (
        optional_commitments
        if optional_commitments is not None
        else saturday_commitments
    )
    is_saturday_recruiting = commitments is not None
    effective_saturday_commitments = staffing.effective_saturday_commitments(
        commitments,
        saturday_availability_overrides,
        *(saturday_shift or (None, None)),
    )
    committed_names = set(effective_saturday_commitments)
    if is_saturday_recruiting:
        assignments = {
            wc_name: [name for name in names if name in committed_names]
            for wc_name, names in (sched.assignments or {}).items()
        }
    else:
        assignments = sched.assignments or {}

    saturday_availability_by_name = {
        name: f"{start.strftime('%I:%M %p').lstrip('0')}–{end.strftime('%I:%M %p').lstrip('0')}"
        for name, value in effective_saturday_commitments.items()
        for start, end in [(value["start"], value["end"])]
        if saturday_shift is None or (start, end) != saturday_shift
    }

    # Build a location-level render model and group by bay (preserving LOCATIONS order).
    bays: list[dict] = []
    current_bay: str | None = None
    for loc in staffing.LOCATIONS:
        required = tuple(work_centers_store.required_skills(loc))
        min_ops = work_centers_store.min_ops(loc)
        max_ops = work_centers_store.max_ops(loc)
        assigned_names = assignments.get(loc.name, [])
        assigned = []
        for n in assigned_names:
            p = all_by_name.get(n)
            if not required:
                # Blank required → render at neutral lvl-2, no color scale.
                lvl = 2
                color = "neutral"
            elif p:
                lvl = min(p.level(s) for s in required)
                color = staffing.skill_color(lvl)
            else:
                lvl = 0
                color = staffing.skill_color(0)
            assigned.append(
                {
                    "name": n,
                    "employee_odoo_id": getattr(p, "employee_id", None),
                    "level": lvl,
                    "color": color,
                }
            )
        # Filter out anyone in Time Off — they shouldn't appear in any WC's
        # picker. The "currently-assigned safety net" below re-adds anyone
        # already historically assigned to this WC, so dirty data won't be
        # silently dropped.
        reserved_names = set(training_reservations_by_center.get(loc.name, ()))
        pool = [
            {**r, "training_reserved": r["name"] in reserved_names}
            for r in options_for(required)
            if r["name"] not in time_off_set
        ]
        if is_saturday_recruiting:
            assigned_safety_net = set(assigned_names)
            pool = [r for r in pool if r["name"] in committed_names or r["name"] in assigned_safety_net]
        assigned_set = {a["name"] for a in assigned}
        # Ensure currently-assigned people appear in pool even if below the filter.
        # (Assigned names are already in the pool since options_for returns everyone,
        # but inactive/deleted people might have been assigned historically.)
        pool_names = {r["name"] for r in pool}
        for a in assigned:
            if a["name"] not in pool_names:
                pool.append({"name": a["name"], "level": a["level"], "color": a["color"], "trained": a["level"] >= 1, "reserve": False, "training_reserved": a["name"] in reserved_names})
                pool_names.add(a["name"])
        # Reserves go last so the template can split them into the bottom group.
        pool.sort(key=lambda r: (r["reserve"], -r["level"], r["name"].lower()))
        # Full-day-off / absent people stay assigned in the saved data (picker
        # checkbox + form input below), but are pulled from the station's
        # display and headcount so the slot reads as needing coverage.
        present_assigned = staffing.present_operators(assigned, time_off_set)
        # Headcount status
        count = len(present_assigned)
        hc_status = "ok"
        if count == 0:
            hc_status = "empty"
        elif count < min_ops:
            hc_status = "under"
        elif max_ops is not None and count > max_ops:
            hc_status = "over"
        # Default people for this WC (editable inline in the scheduler).
        defaults_list = work_centers_store.default_people(loc)
        default_set = set(defaults_list)
        # Auto-open the picker's reserves group when a reserve is currently chosen there.
        has_selected_reserve = any(r["reserve"] and r["name"] in assigned_set for r in pool)
        has_default_reserve = any(r["reserve"] and r["name"] in default_set for r in pool)
        row = {
            "loc": loc,
            "assigned": assigned,
            "present_assigned": present_assigned,
            "pool": pool,
            "assigned_set": assigned_set,
            "min_ops": min_ops,
            "max_ops": max_ops,
            "max_ops_label": ("∞" if max_ops is None else str(max_ops)),
            "required_skills": list(required),
            "default_people": defaults_list,
            "default_set": default_set,
            "has_selected_reserve": has_selected_reserve,
            "has_default_reserve": has_default_reserve,
            "hc_status": hc_status,
            "hc_badge": (
                "needs " + str(min_ops) if hc_status == "under"
                else ("max " + str(max_ops) if hc_status == "over" else "")
            ),
            "wc_note": (sched.wc_notes or {}).get(loc.name, ""),
        }
        if loc.bay != current_bay:
            bays.append({"name": loc.bay, "subtitle": staffing.BAY_SUBTITLES.get(loc.bay, ""), "rows": [row]})
            current_bay = loc.bay
        else:
            bays[-1]["rows"].append(row)

    # Only populate block reasons if we just came back from a failed publish attempt.
    publish_block_reasons = []
    if publish_errors:
        publish_block_reasons = list(publish_errors)
    elif publish_blocked:
        enabled = (
            {loc.name for loc in staffing.LOCATIONS}
            if enabled_work_centers is None else set(enabled_work_centers)
        )
        for bay in bays:
            for r in bay["rows"]:
                if r["loc"].name not in enabled:
                    continue
                if len(r["assigned"]) < r["min_ops"]:
                    publish_block_reasons.append(
                        f"{r['loc'].name} requires {r['min_ops']} operators — currently {len(r['assigned'])}."
                    )

    # Per-WC default operators (set in Settings → Work Centers). Exposed as a
    # plain dict for the scheduler's "Reset to defaults" button.
    defaults_by_loc = {
        loc.name: list(work_centers_store.default_people(loc))
        for loc in staffing.LOCATIONS
    }

    # Unscheduled = active non-reserve people with no station and not on time off.
    # Reserves (office staff / managers) live in their own list regardless of state.
    assigned_today = {
        n
        for key, names in assignments.items()
        if key != staffing.TIME_OFF_KEY
        for n in names
    }
    if is_saturday_recruiting:
        unassigned = [
            p.name for p in active_people
            if not p.reserve and p.name in committed_names
            and p.name not in assigned_today and p.name not in time_off_set
        ]
        off = [
            p.name for p in active_people
            if not p.reserve and p.name not in committed_names
            and p.name not in assigned_today and p.name not in time_off_set
        ]
    else:
        unassigned = [
            p.name
            for p in active_people
            if not p.reserve and p.name not in assigned_today and p.name not in time_off_set
        ]
        off = []
    reserves = [p.name for p in active_people if p.reserve and p.name not in time_off_set]

    return {
        "bays": bays,
        "publish_block_reasons": publish_block_reasons,
        # Time Off panel + the client-side __timeOffNames set are
        # FULL-DAY only. Partial-day people live in Unscheduled/Reserves
        # (with an off-window badge); listing them here would let the
        # left-rail "defensive sweep" pull them back out of Unscheduled.
        "time_off_names": sorted(e["name"] for e in full_day_entries),
        "time_off_entries": sorted(full_day_entries, key=lambda e: e["name"].lower()),
        "partial_hours_by_name": partial_hours_by_name,
        "partial_range_by_name": partial_range_by_name,
        "partial_clear_by_name": partial_clear_by_name,
        "unassigned": sorted(unassigned),
        "off": sorted(off),
        "saturday_committed_names": sorted(committed_names),
        "saturday_availability_by_name": saturday_availability_by_name,
        "is_saturday_recruiting": is_saturday_recruiting,
        "reserves": sorted(reserves),
        # JS uses this to route auto-removed people back to the right
        # left-rail list (Unscheduled vs Reserves) on uncheck/X.
        "people_meta": {p.name: {"reserve": p.reserve} for p in active_people},
        "defaults_by_loc": defaults_by_loc,
        "all_active_people": all_active_people,
    }
