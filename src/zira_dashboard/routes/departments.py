"""Department dashboard pages: GET /recycling and GET /new.

(The pre-2026-05-26 legacy URLs /new-vs and /tv/new-vs were removed
2026-07-22 after 30 days of zero traffic — Dale signed off per-URL.)"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, UTC

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from .. import (
    layout_store,
    recycling_range,
    settings_store,
    shift_config,
    staffing,
    wc_dashboard_data,
    widget_customizer,
    work_centers_store,
)
from ..deps import _state, client, templates
from ..leaderboard import cached_leaderboard as leaderboard
from ..plant_day import today as plant_today
from ..progress import progress_buckets
from ..recycling_data import (
    aggregate_buckets,
    build_bars,
    build_downtime_rows,
    compute_per_wc_expected,
    group_goal,
    sort_bars,
)
from ..shift_config import shift_elapsed_minutes
from ..stations import Station, recycling_stations

router = APIRouter()

# Persistent pool for range-view day fan-out (see _render_recycling). A
# per-request ThreadPoolExecutor paid thread spin-up/teardown on every render;
# this one lives for the process. Cap at 4 workers so we don't starve the DB
# pool (maxconn=20) or hammer the Zira API on multi-month ranges.
_RANGE_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dept-range")


def _present_assignments(
    assignments: dict[str, list[str]],
    absent_names: set[str] | None,
) -> dict[str, list[str]]:
    absent = set(absent_names or ())
    return {
        wc_name: [name for name in (ops or []) if name not in absent]
        for wc_name, ops in (assignments or {}).items()
    }


def _who_by_wc(
    assignments: dict[str, list[str]],
    day,
    absent_names: set[str] | None = None,
) -> dict[str, str]:
    """Map work-center name → " + "-joined operator string for the dashboard
    `who` labels. Starts from the schedule assignments, then layers in retro
    WC attributions on top so saved attributions appear immediately on the
    bar / downtime widgets. Full-day absent people are filtered from both
    sources without changing the saved schedule. Dedupes scheduled-then-
    attributed people, keeps scheduled order first.
    """
    absent = set(_absent_names(day) if absent_names is None else absent_names)
    out: dict[str, str] = {}
    for wc_name, ops in assignments.items():
        if wc_name == staffing.TIME_OFF_KEY or not ops:
            continue
        present_ops = [name for name in ops if name not in absent]
        if present_ops:
            out[wc_name] = " + ".join(present_ops)
    try:
        from .. import wc_attributions
        for wc_name, names in wc_attributions.people_by_wc(day).items():
            present_names = [name for name in (names or []) if name not in absent]
            if not present_names:
                continue
            existing = out.get(wc_name, "")
            existing_names = [n.strip() for n in existing.split(" + ") if n.strip()] if existing else []
            seen, combined = set(), []
            for n in existing_names + present_names:
                if n and n not in seen:
                    seen.add(n)
                    combined.append(n)
            out[wc_name] = " + ".join(combined)
    except Exception:
        pass
    return out


def _absent_names(d) -> set:
    """Full-day absences for day `d` (approved full-day off, manual absences,
    derived no-punch), used to exclude scheduled-but-absent people from
    man-hours. Falls back to an empty set if attendance can't be loaded."""
    try:
        from .. import attendance
        return attendance.full_day_absent_names(d)
    except Exception:
        return set()


def _assign_popover_context(today, client):
    """Inline-assign popover data for the single-day "today" view: any
    unattributed WCs (so "(no assignment)" lines become click-to-attribute)
    plus the active roster names. Returns
    (assignments_todo_by_wc, all_active_people); both empty on error."""
    assignments_todo_by_wc: dict[str, dict] = {}
    all_active_people: list[str] = []
    try:
        from .. import staffing as _staffing, wc_attributions
        todo = wc_attributions.unattributed_for_day(today, client)
        site_tz = shift_config.SITE_TZ
        for item in todo:
            first = item["first_sample_utc"].astimezone(site_tz)
            last = item["last_sample_utc"].astimezone(site_tz)
            assignments_todo_by_wc[item["wc_name"]] = {
                "wc_name": item["wc_name"],
                "units": item["units"],
                "first_label": first.strftime("%I:%M %p").lstrip("0"),
                "last_label": last.strftime("%I:%M %p").lstrip("0"),
                "first_iso": item["first_sample_utc"].isoformat(),
                "last_iso": item["last_sample_utc"].isoformat(),
            }
        roster = _staffing.load_roster()
        all_active_people = sorted((p.name for p in roster if p.active), key=str.lower)
    except Exception:
        assignments_todo_by_wc = {}
        all_active_people = []
    return assignments_todo_by_wc, all_active_people


def _department_day_data(
    d,
    now,
    is_today_d,
    *,
    stations: list[Station],
    labor_department: str,
    group_categories: tuple[str, ...],
    align_to_standard: bool = False,
):
    """Compute the per-day numbers for a department dashboard.

    Returns a dict with the keys the route handler needs to aggregate:
      total_units, total_downtime, elapsed, available, uptime_minutes,
      total_man_hours, total_recycling_people,
      per_wc_units {name: int}, per_wc_downtime {name: int},
      per_wc_expected {name: float}, per_wc_who {name: str|None},
      per_wc_state {name: str},  # only meaningful when is_today_d
      group_buckets,  # dict[str, list[dict]] from progress_buckets
      shift_start_label, schedule_assignments,
      active_wc_names, per_wc_category, per_wc_station_obj.
    Days outside the working schedule (weekends) return zero-shaped values.
    """
    results = leaderboard(client, stations, d, now_utc=now if is_today_d else None)

    sched = staffing.load_schedule(d)
    # Full-day absences (approved full-day off, manual absences, derived
    # no-punch). Use a present-only copy for live dashboard display/goal math
    # while preserving the saved schedule for undo/history.
    _absent_today = _absent_names(d)
    present_assignments = _present_assignments(sched.assignments, _absent_today)

    # Resolve the day's shift bounds first; reused for the man-hours window,
    # the grace interval, the productive-intervals math below, AND the work
    # segment resolution (which needs them to floor/cap punch + attribution
    # windows). Honors per-day custom_hours via the `_for(d)` variants.
    shift_start_local = datetime.combine(d, shift_config.shift_start_for(d), tzinfo=shift_config.SITE_TZ)
    shift_end_local = datetime.combine(d, shift_config.shift_end_for(d), tzinfo=shift_config.SITE_TZ)
    now_local = now.astimezone(shift_config.SITE_TZ)
    window_end_local = min(now_local, shift_end_local) if is_today_d else shift_end_local
    window_start_utc = shift_start_local.astimezone(UTC)
    window_end_utc = window_end_local.astimezone(UTC)

    # Merge timeclock attendance + open-ended attributions + schedule into closed
    # work segments. The TIMECLOCK is the source of truth for where each operator
    # is clocked in: attendance_windows_for_day reads the COMPLETE set of Odoo
    # hr.attendance records (morning record, auto-lunch's afternoon record, every
    # mid-shift transfer), so a person clocked in all day gets a full-day goal and
    # a mid-day transfer moves the goal to the new WC. Manual attributions are the
    # fallback for production at a WC the operator never transferred into. Per
    # person, the timeclock wins over the schedule; people with no attendance
    # records fall back to their schedule.
    from .. import assignment_windows, timeclock_windows, wc_attributions, machine_breakdown
    segments = assignment_windows.resolve_segments(
        assignments=present_assignments,
        attributions=wc_attributions.creditable_for_day(d),
        punch_windows=timeclock_windows.attendance_windows_for_day(d),
        shift_start_utc=window_start_utc,
        cap_utc=window_end_utc,
        time_off_key=staffing.TIME_OFF_KEY,
        excluded_people=_absent_today,
    )
    who_by_wc = assignment_windows.who_by_wc(segments)

    ACTIVE_UNITS_THRESHOLD = 5
    active_wc_names: set[str] = set(who_by_wc.keys())
    for r in results:
        if r.units > ACTIVE_UNITS_THRESHOLD:
            active_wc_names.add(r.station.name)
    active_results = [r for r in results if r.station.name in active_wc_names]
    active_stations = [s for s in stations if s.name in active_wc_names]
    total_units = sum(r.units for r in active_results)
    total_downtime = sum(r.downtime_minutes for r in active_results)
    elapsed = shift_elapsed_minutes(d, now)
    available = elapsed * len(active_stations)
    uptime_minutes = max(0, available - total_downtime)

    # Partial-day off intervals fetched ONCE per day and passed into
    # effective_minutes_worked below (it used to re-query per person).
    try:
        from .. import attendance as _attendance
        _partials = _attendance.partial_off_intervals(d)
    except Exception:
        _partials = None

    total_man_minutes = 0
    total_recycling_people = 0
    for loc in staffing.LOCATIONS:
        # Filter on loc.department (the static "Recycled / New / Supervisor /
        # Maintenance" classification) rather than the user-editable
        # work_centers_store.department — the latter has Loading/Jockeying,
        # Tablets, and Work Orders set to "Recycled" as a value-stream
        # association, but those are forklift + mechanic support roles, not
        # production-line labor on the recycling line.
        if loc.department != labor_department:
            continue
        for person_name in present_assignments.get(loc.name, []):
            total_recycling_people += 1
            total_man_minutes += staffing.effective_minutes_worked(
                person_name, d, window_start_utc, window_end_utc,
                partials=_partials,
            )
    # Fallback for days without a published schedule: if nobody was scheduled
    # but production still happened, estimate man-hours from the active WCs.
    # Each WC that produced above the activity threshold counts as one person
    # working the full shift window. Keeps pph_per_person honest in ranges
    # that include older days Dale never published a schedule for.
    if total_recycling_people == 0 and active_results:
        window_minutes = max(0, int((window_end_utc - window_start_utc).total_seconds() // 60))
        inferred_people = len(active_results)
        total_man_minutes = window_minutes * inferred_people
        total_recycling_people = inferred_people
    total_man_hours = total_man_minutes / 60.0

    group_results = {
        category: sorted(
            [r for r in active_results if r.station.category == category],
            key=lambda r: r.station.name,
        )
        for category in group_categories
    }

    # ---- Productive intervals per WC ----
    grace_end_local = shift_start_local + timedelta(minutes=60)
    grace_end_capped_local = min(grace_end_local, now_local) if is_today_d else grace_end_local
    grace_interval_utc = (
        shift_start_local.astimezone(UTC),
        grace_end_capped_local.astimezone(UTC),
    )
    people_by_wc: dict[str, int] = {
        wc: len(ops) for wc, ops in present_assignments.items()
        if wc != staffing.TIME_OFF_KEY and ops
    }

    def _merge(intervals):
        if not intervals:
            return []
        intervals = sorted(intervals, key=lambda x: x[0])
        out = [intervals[0]]
        for s, e in intervals[1:]:
            if s <= out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], e))
            else:
                out.append((s, e))
        return out

    breaks_utc: list[tuple[datetime, datetime]] = []
    for b in shift_config.breaks_for(d):
        bs = datetime.combine(d, b.start, tzinfo=shift_config.SITE_TZ).astimezone(UTC)
        be = datetime.combine(d, b.end, tzinfo=shift_config.SITE_TZ).astimezone(UTC)
        if be > bs:
            breaks_utc.append((bs, be))

    def _subtract_breaks(intervals):
        if not breaks_utc:
            return intervals
        chunks = list(intervals)
        for b_s, b_e in breaks_utc:
            new_chunks = []
            for c_s, c_e in chunks:
                if b_e <= c_s or b_s >= c_e:
                    new_chunks.append((c_s, c_e))
                    continue
                if c_s < b_s:
                    new_chunks.append((c_s, b_s))
                if c_e > b_e:
                    new_chunks.append((b_e, c_e))
            chunks = new_chunks
        return chunks

    grace_has_duration = grace_interval_utc[1] > grace_interval_utc[0]
    productive_by_wc: dict[str, list[tuple[datetime, datetime]]] = {}
    for r in active_results:
        ints = list(r.active_intervals)
        if r.station.name in people_by_wc and grace_has_duration:
            ints.append(grace_interval_utc)
        productive_by_wc[r.station.name] = _subtract_breaks(_merge(ints))

    def _productive_minutes(name: str) -> float:
        return sum((b - a).total_seconds() / 60.0 for a, b in productive_by_wc.get(name, []))

    def _make_target_fn(group):
        def fn(b_start_local: datetime, b_end_local: datetime) -> float:
            bucket_min = (b_end_local - b_start_local).total_seconds() / 60.0
            if b_end_local <= grace_end_local:
                tot = 0.0
                for r in group:
                    name = r.station.name
                    if name not in people_by_wc:
                        continue
                    tot += settings_store.station_target(r.station) * people_by_wc[name] * bucket_min / 60.0
                return tot
            tot = 0.0
            for r in group:
                hr = settings_store.station_target(r.station)
                if hr <= 0:
                    continue
                for ai_s_utc, ai_e_utc in productive_by_wc.get(r.station.name, []):
                    ai_s = ai_s_utc.astimezone(shift_config.SITE_TZ)
                    ai_e = ai_e_utc.astimezone(shift_config.SITE_TZ)
                    o_s = max(ai_s, b_start_local)
                    o_e = min(ai_e, b_end_local)
                    if o_e > o_s:
                        tot += hr * (o_e - o_s).total_seconds() / 60.0 / 60.0
            return tot
        return fn

    group_buckets = {
        category: progress_buckets(
            rows,
            d,
            now,
            target_fn=_make_target_fn(rows),
            align_to_standard=align_to_standard,
        )
        for category, rows in group_results.items()
    }

    # Per-WC dicts the aggregator can sum.
    per_wc_units = {r.station.name: r.units for r in active_results}
    per_wc_downtime = {r.station.name: r.downtime_minutes for r in active_results}
    # Per-WC expected pallets: prorate each work segment from its OWN start
    # (mid-day assignments included) to its end/transfer/now, using productive
    # minutes (breaks + partial time-off already netted out). Replaces the old
    # scheduled_headcount x shift-wide elapsed_hours, which ignored mid-day
    # attributions/punches and so showed no goal for unscheduled-but-worked WCs.
    target_per_hour = {
        r.station.name: settings_store.station_target(r.station) for r in active_results
    }
    # Prorate the goal by BREAKS-ONLY productive minutes in each segment's
    # window (matches the long-standing shift_elapsed_minutes pace target).
    # NOT effective_minutes_worked -- that nets out an operator's partial
    # time-off, which wrongly shrinks a station's pace goal when someone takes
    # leave. The per-segment window still makes a mid-day assignment (e.g.
    # Dismantler 4) accrue only from its own start.
    breakdown_windows = wc_attributions.breakdown_windows_for_day(d)

    def _productive_minutes_less_breakdown(name, wc_name, s_utc, e_utc):
        raw = shift_config.productive_minutes_in_window(d, s_utc, e_utc)
        excluded = machine_breakdown.excluded_minutes_overlapping(
            breakdown_windows.get((name, wc_name), []),
            s_utc, e_utc, now, d,
            shift_config.productive_minutes_in_window,
        )
        return max(0.0, raw - excluded)

    per_wc_expected = compute_per_wc_expected(
        segments=segments,
        active_wc_names=active_wc_names,
        target_per_hour=target_per_hour,
        productive_minutes=_productive_minutes_less_breakdown,
    )
    per_wc_state = {r.station.name: _state(r, now, is_today_d) for r in active_results}
    per_wc_who = {r.station.name: who_by_wc.get(r.station.name) for r in active_results}
    per_wc_category = {r.station.name: r.station.category for r in active_results}
    per_wc_station_obj = {r.station.name: r.station for r in active_results}

    return {
        "total_units": total_units,
        "total_downtime": total_downtime,
        "elapsed": elapsed,
        "available": available,
        "uptime_minutes": uptime_minutes,
        "total_man_hours": total_man_hours,
        "total_recycling_people": total_recycling_people,
        "per_wc_units": per_wc_units,
        "per_wc_downtime": per_wc_downtime,
        "per_wc_expected": per_wc_expected,
        "per_wc_state": per_wc_state,
        "per_wc_who": per_wc_who,
        "per_wc_category": per_wc_category,
        "per_wc_station_obj": per_wc_station_obj,
        "active_wc_names": active_wc_names,
        "schedule_assignments": present_assignments,
        "group_buckets": group_buckets,
        "shift_start_label": shift_start_local.strftime("%H:%M"),
    }


def _recycling_day_data(d, now, is_today_d, align_to_standard=False):
    data = _department_day_data(
        d,
        now,
        is_today_d,
        stations=recycling_stations(),
        labor_department="Recycled",
        group_categories=("Dismantler", "Repair"),
        align_to_standard=align_to_standard,
    )
    data["dism_buckets"] = data["group_buckets"]["Dismantler"]
    data["repair_buckets"] = data["group_buckets"]["Repair"]
    return data


def _new_stations() -> list[Station]:
    return [
        Station(
            meter_id=loc.meter_id,
            name=loc.name,
            category="New",
            cell="New",
        )
        for loc in staffing.LOCATIONS
        if work_centers_store.department(loc) == "New" and loc.meter_id
    ]


def _new_day_data(d, now, is_today_d, align_to_standard=False):
    return _department_day_data(
        d,
        now,
        is_today_d,
        stations=_new_stations(),
        labor_department="New",
        group_categories=("New",),
        align_to_standard=align_to_standard,
    )


@router.get("/recycling", response_class=HTMLResponse)
def recycling(
    request: Request,
    window: str = Query(default="today"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    return _render_recycling(
        request,
        window=window,
        start=start,
        end=end,
        tv_mode=False,
        tv_theme="dark",
    )


def _render_recycling(
    request: Request,
    *,
    window: str,
    start: str | None,
    end: str | None,
    tv_mode: bool,
    tv_theme: str,
):
    """Shared implementation for /recycling (screen) and /tv/recycling
    (TV). Cache key includes tv_mode + tv_theme so screen and TV variants
    have separate cache entries; otherwise a cached screen response would
    be served to the TV route and vice-versa, both losing the per-variant
    context.
    """
    from ..deps import resolve_range

    today = plant_today()
    start_d, end_d, custom_range_active = resolve_range(window, start, end, today)

    is_today = (start_d == end_d == today)
    is_range = (start_d != end_d)
    range_includes_today = (start_d <= today <= end_d)

    # Cache key includes both bounds.
    from .._http_cache import get_cached_response, set_cache_headers, store_cached_response
    cache_key = ("recycling", start_d.isoformat(), end_d.isoformat(), tv_mode, tv_theme)
    cached = get_cached_response(cache_key, includes_today=range_includes_today)
    if cached is not None:
        return cached

    now = datetime.now(UTC)

    # Walk every day in the range, computing per-day data.
    days: list = []
    cursor = start_d
    while cursor <= end_d:
        days.append(cursor)
        cursor += timedelta(days=1)

    # Range views (week/month/quarter/year) used to walk days sequentially,
    # paying full I/O cost per day on a cold cache. Fan out across the
    # module-level pool — `_recycling_day_data` is read-only and the caches it
    # touches (leaderboard TTL, per-day schedule cache, work_centers,
    # settings) are thread-safe. The pool is capped at 4 workers so we don't
    # starve the DB pool (maxconn=30) or hammer the Zira API on multi-month
    # ranges. Single-day stays inline so the most common case doesn't queue
    # behind a busy pool.
    def _compute_day(d):
        return _recycling_day_data(d, now, d == today, align_to_standard=is_range)

    if len(days) > 1:
        per_day = list(_RANGE_POOL.map(_compute_day, days))
    else:
        per_day = [_compute_day(d) for d in days]

    aggregate = recycling_range.aggregate_range(per_day, days, is_range=is_range)
    total_units = aggregate.total_units
    total_downtime = aggregate.total_downtime
    total_elapsed = aggregate.total_elapsed
    total_available = aggregate.total_available
    total_uptime_minutes = aggregate.total_uptime_minutes
    total_man_hours = aggregate.total_man_hours
    agg_units = aggregate.agg_units
    agg_downtime = aggregate.agg_downtime
    agg_expected = aggregate.agg_expected
    agg_who_today = aggregate.agg_who_today
    agg_category = aggregate.agg_category
    agg_active_names = aggregate.agg_active_names
    schedule_today_assignments = aggregate.schedule_today_assignments

    uptime_pct = (total_uptime_minutes / total_available * 100.0) if total_available > 0 else 0.0
    pallets_per_hour = (total_units / (total_elapsed / 60.0)) if total_elapsed > 0 else 0.0
    pph_per_person = (total_units / total_man_hours) if total_man_hours > 0 else 0.0

    # Dismantler 4 secondary metric: same denominator (we still pay for the
    # operator labor) but the numerator drops D4's pallets. D4 reprocesses
    # reject material that's already been counted upstream by the ERP, so the
    # all-stations pph/person reads ~30% high vs. Dale's ERP-derived number.
    # Keeping this as a smaller side-by-side helps spot when D4's share of
    # total volume drifts (good = repair quality up, bad = D4 idle).
    d4_units = agg_units.get("Dismantler 4", 0)
    units_ex_d4 = max(0, total_units - d4_units)
    pph_per_person_ex_d4 = (units_ex_d4 / total_man_hours) if total_man_hours > 0 else 0.0

    # Buckets aggregated by time-of-day label.
    dism_progress = aggregate_buckets([p["dism_buckets"] for p in per_day])
    repair_progress = aggregate_buckets([p["repair_buckets"] for p in per_day])

    # Group hourly target — average over total elapsed hours, summing per-WC expected.
    elapsed_hours_total = total_elapsed / 60.0 if total_elapsed else 0.0
    dism_group_target = group_goal(
        "Dismantler",
        elapsed_hours_total=elapsed_hours_total,
        agg_expected=agg_expected,
        agg_category=agg_category,
    )
    repair_group_target = group_goal(
        "Repair",
        elapsed_hours_total=elapsed_hours_total,
        agg_expected=agg_expected,
        agg_category=agg_category,
    )

    customs_all = widget_customizer.load_all("recycling")

    def _bars(category: str) -> list[dict]:
        return build_bars(
            category,
            agg_active_names=agg_active_names,
            agg_category=agg_category,
            agg_units=agg_units,
            agg_expected=agg_expected,
            agg_who_today=agg_who_today,
            is_range=is_range,
            agg_downtime=agg_downtime,
        )

    def _sorted_bars(items: list, widget_id: str) -> list:
        return sort_bars(items, widget_id, customs_all=customs_all)

    def _downtime_rows():
        return build_downtime_rows(
            agg_active_names=agg_active_names,
            agg_category=agg_category,
            agg_downtime=agg_downtime,
            total_elapsed=total_elapsed,
            agg_who_today=agg_who_today,
            is_range=is_range,
        )

    now_local = now.astimezone(shift_config.SITE_TZ)
    now_label = now_local.strftime("%H:%M")
    shift_start_label = per_day[-1]["shift_start_label"] if per_day else ""

    # People count: total person-days across the range for ranges; today's count for Today.
    if is_range:
        dism_people = 0
        repair_people = 0
        for p in per_day:
            for name, ops in p["schedule_assignments"].items():
                if name == staffing.TIME_OFF_KEY or not ops:
                    continue
                cat = p["per_wc_category"].get(name)
                if cat == "Dismantler":
                    dism_people += len(ops)
                elif cat == "Repair":
                    repair_people += len(ops)
    else:
        dism_people = sum(
            len(schedule_today_assignments.get(name, []))
            for name in agg_active_names
            if agg_category.get(name) == "Dismantler"
        )
        repair_people = sum(
            len(schedule_today_assignments.get(name, []))
            for name in agg_active_names
            if agg_category.get(name) == "Repair"
        )

    # Inline-assign popover: for single-day "today" view, list any unattributed
    # WCs so the dashboard's "(no assignment)" lines can become click-to-attribute.
    assignments_todo_by_wc: dict[str, dict] = {}
    all_active_people: list[str] = []
    if is_today:
        assignments_todo_by_wc, all_active_people = _assign_popover_context(today, client)

    operator_links_by_wc: dict[str, str] = {}
    if not is_range:
        for name in agg_active_names:
            href = wc_dashboard_data.dashboard_url_for_wc_day(name, start_d)
            if href:
                operator_links_by_wc[name] = href

    response = templates.TemplateResponse(
        request,
        "recycling.html",
        {
            "active_vs": "recycling",
            "active_dashboard_key": "vs_recycling",
            "assignments_todo_by_wc": assignments_todo_by_wc,
            "all_active_people": all_active_people,
            "window": window,
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "today": today.isoformat(),
            "is_today": is_today,
            "is_range": is_range,
            "range_includes_today": range_includes_today,
            "custom_range_active": custom_range_active,
            "operator_links_by_wc": operator_links_by_wc,
            "total_units": total_units,
            "total_downtime_minutes": total_downtime,
            "total_downtime_display": f"{total_downtime / 60:.1f} h",
            "uptime_pct": round(uptime_pct, 1),
            "pallets_per_hour": round(pallets_per_hour, 1),
            "pph_per_person": round(pph_per_person, 1),
            "pph_per_person_ex_d4": round(pph_per_person_ex_d4, 1),
            "elapsed_minutes": total_elapsed,
            "dismantler_bars": _sorted_bars(_bars("Dismantler"), "dismantler-bars"),
            "repair_bars": _sorted_bars(_bars("Repair"), "repair-bars"),
            "downtime_rows": _downtime_rows(),
            "dismantler_progress": dism_progress,
            "repair_progress": repair_progress,
            "dismantler_group_target": dism_group_target,
            "repair_group_target": repair_group_target,
            "dismantler_people": dism_people,
            "repair_people": repair_people,
            "layout": layout_store.layout_map("recycling"),
            "customs": customs_all,
            "now_label": now_label,
            "shift_start_label": shift_start_label,
            "refreshed_at": now.astimezone(shift_config.SITE_TZ).strftime("%-I:%M:%S %p"),
            "tv_mode": tv_mode,
            "tv_theme": tv_theme,
            # GOAT Watch banner data — live contenders (only on today)
            # and persisted NEW GOAT alerts (visible through next
            # business day).
            "goat_contenders": (
                _goat_watch_contenders(today, now) if is_today else []
            ),
            "goat_alerts_active": _goat_watch_active_alerts(today),
        },
    )
    set_cache_headers(response, includes_today=range_includes_today)
    store_cached_response(cache_key, includes_today=range_includes_today, response=response)
    return response


def _goat_watch_contenders(day, now_utc):
    try:
        from .. import goat_watch
        return goat_watch.contenders_for_now(day, now_utc)
    except Exception:
        return []


def _goat_watch_active_alerts(today):
    try:
        from .. import goat_watch
        return goat_watch.active_alerts(today)
    except Exception:
        return []


@router.get("/tv/recycling", response_class=HTMLResponse)
def tv_recycling(request: Request, theme: str | None = Query(default=None)):
    """Read-only TV variant of /recycling. No top nav, no range chips,
    no widget edit buttons, larger fonts. Always shows today.

    Theme: 'dark' (default) or 'light' via ?theme=light.
    """
    tv_theme = "light" if theme == "light" else "dark"
    return _render_recycling(
        request,
        window="today",
        start=None,
        end=None,
        tv_mode=True,
        tv_theme=tv_theme,
    )


@router.get("/new", response_class=HTMLResponse)
def new_dept(
    request: Request,
    window: str = Query(default="today"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    day: str | None = Query(default=None),
):
    """Departments → New subtab. Shows only work centers whose Settings
    department is "New" and that have a meter ID. Sparse data is the norm
    here today since most "New" stations aren't metered yet."""
    if day and not (start and end):
        start = end = day
    return _render_new_dept(
        request,
        window=window,
        start=start,
        end=end,
        day=day,
        tv_mode=False,
        tv_theme="dark",
    )


def _render_new_dept(
    request: Request,
    *,
    window: str,
    start: str | None,
    end: str | None,
    day: str | None,
    tv_mode: bool,
    tv_theme: str,
):
    """Shared implementation for /new (screen) and /tv/new (TV).
    Cache key includes tv_mode + tv_theme so screen and TV variants have
    separate cache entries.
    """
    from ..deps import resolve_range

    today = plant_today()
    start_d, end_d, custom_range_active = resolve_range(window, start, end, today)
    is_today = start_d == end_d == today
    is_range = start_d != end_d
    range_includes_today = start_d <= today <= end_d

    from .._http_cache import get_cached_response, set_cache_headers, store_cached_response
    cache_key = (
        "new_dept", start_d.isoformat(), end_d.isoformat(), tv_mode, tv_theme
    )
    cached = get_cached_response(cache_key, includes_today=range_includes_today)
    if cached is not None:
        return cached

    now = datetime.now(UTC)

    days: list = []
    cursor = start_d
    while cursor <= end_d:
        days.append(cursor)
        cursor += timedelta(days=1)

    def _compute_day(d):
        return _new_day_data(d, now, d == today, align_to_standard=is_range)

    if len(days) > 1:
        per_day = list(_RANGE_POOL.map(_compute_day, days))
    else:
        per_day = [_compute_day(d) for d in days]

    aggregate = recycling_range.aggregate_range(per_day, days, is_range=is_range)
    configured_new_meter_count = len(_new_stations())
    new_progress = aggregate_buckets([
        item["group_buckets"]["New"] for item in per_day
    ])
    customs_all = widget_customizer.load_all("new")
    new_bars = build_bars(
        "New",
        agg_active_names=aggregate.agg_active_names,
        agg_category=aggregate.agg_category,
        agg_units=aggregate.agg_units,
        agg_expected=aggregate.agg_expected,
        agg_who_today=aggregate.agg_who_today,
        is_range=is_range,
        agg_downtime=aggregate.agg_downtime,
    )
    new_bars = sort_bars(new_bars, "new-bars", customs_all=customs_all)
    downtime_rows = build_downtime_rows(
        agg_active_names=aggregate.agg_active_names,
        agg_category=aggregate.agg_category,
        agg_downtime=aggregate.agg_downtime,
        total_elapsed=aggregate.total_elapsed,
        agg_who_today=aggregate.agg_who_today,
        is_range=is_range,
        categories=("New",),
    )

    total_units = aggregate.total_units
    total_elapsed = aggregate.total_elapsed
    total_available = aggregate.total_available
    total_uptime_minutes = aggregate.total_uptime_minutes
    total_man_hours = aggregate.total_man_hours
    uptime_pct = (
        total_uptime_minutes / total_available * 100.0
        if total_available > 0 else 0.0
    )
    pph_per_person = (
        total_units / total_man_hours if total_man_hours > 0 else 0.0
    )
    elapsed_hours_total = total_elapsed / 60.0 if total_elapsed else 0.0
    new_group_target = group_goal(
        "New",
        elapsed_hours_total=elapsed_hours_total,
        agg_expected=aggregate.agg_expected,
        agg_category=aggregate.agg_category,
    )
    new_people = sum(item["total_recycling_people"] for item in per_day)
    operator_links_by_wc = {
        wc_name: wc_dashboard_data.dashboard_url_for_wc_day(wc_name, end_d)
        for wc_name in aggregate.agg_active_names
    }

    # Inline-assign popover: today only. Mirrors the recycling route so the
    # "(no assignment)" bars on /new become click-to-attribute buttons.
    assignments_todo_by_wc: dict[str, dict] = {}
    all_active_people: list[str] = []
    if is_today:
        assignments_todo_by_wc, all_active_people = _assign_popover_context(today, client)

    response = templates.TemplateResponse(
        request,
        "new_dept.html",
        {
            "active_vs": "new",
            "active_dashboard_key": "vs_new",
            "assignments_todo_by_wc": assignments_todo_by_wc,
            "all_active_people": all_active_people,
            "window": window,
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "today": today.isoformat(),
            "is_today": is_today,
            "is_range": is_range,
            "range_includes_today": range_includes_today,
            "custom_range_active": custom_range_active,
            "operator_links_by_wc": operator_links_by_wc,
            "total_units": total_units,
            "pph_per_person": round(pph_per_person, 1),
            "new_bars": new_bars,
            "configured_new_meter_count": configured_new_meter_count,
            "new_progress": new_progress,
            "new_group_target": new_group_target,
            "new_people": new_people,
            "downtime_rows": downtime_rows,
            "uptime_pct": uptime_pct,
            "layout": layout_store.layout_map("new"),
            "layout_key": "new",
            "customs": customs_all,
            # Compatibility values for the legacy New template. Task 4 will
            # consume the range-aware values above directly.
            "day": end_d.isoformat(),
            "total_downtime_display": f"{aggregate.total_downtime / 60:.1f} h",
            "pallets_per_hour": round(
                total_units / elapsed_hours_total if elapsed_hours_total > 0 else 0.0,
                1,
            ),
            "elapsed_minutes": total_elapsed,
            "bars": new_bars,
            "has_dismantlers": False,
            "has_repairs": False,
            "new_dism_progress": [],
            "new_repair_progress": [],
            "new_dism_group_target": 0.0,
            "new_repair_group_target": 0.0,
            "new_dism_people": 0,
            "new_repair_people": 0,
            "refreshed_at": now.astimezone(shift_config.SITE_TZ).strftime("%-I:%M:%S %p"),
            "tv_mode": tv_mode,
            "tv_theme": tv_theme,
            # NEW GOAT alerts surface on every dashboard so a record-breaker
            # is celebrated plant-wide. Live contenders stay on /recycling
            # only — they're a per-group projection, not a per-WC stat.
            "goat_alerts_active": _goat_watch_active_alerts(today),
            "goat_contenders": [],
        },
    )
    set_cache_headers(response, includes_today=range_includes_today)
    store_cached_response(cache_key, includes_today=range_includes_today, response=response)
    return response


@router.get("/tv/new", response_class=HTMLResponse)
def tv_new_dept(request: Request, theme: str | None = Query(default=None)):
    """Read-only TV variant of /new. See tv_recycling for theme rules."""
    tv_theme = "light" if theme == "light" else "dark"
    return _render_new_dept(
        request,
        window="today",
        start=None,
        end=None,
        day=None,
        tv_mode=True,
        tv_theme=tv_theme,
    )


