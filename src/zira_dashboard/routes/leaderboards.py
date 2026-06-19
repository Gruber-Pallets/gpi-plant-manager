"""Per-skill production leaderboards: GET /staffing/leaderboards."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import settings_store, staffing
from ..deps import resolve_range, templates
from ..plant_day import today as plant_today
from ..production_history import attribution_per_day
from ..stations import Station
from .._cache import TTLCache

# Cached responses by (name, scope-key, start, end). Past-only ranges
# get the longer TTL; ranges that include today get the shorter one
# so a fresh-published schedule shows up on the next click.
_PERSON_DAYS_CACHE_TODAY = TTLCache(ttl_seconds=60.0, max_entries=128)
_PERSON_DAYS_CACHE_PAST = TTLCache(ttl_seconds=3600.0, max_entries=512)


def averages_for_wc(
    records: list[dict],
    target_per_hour: float,
    productive_minutes_for,
    mode: str,
) -> list[dict]:
    """Per-person averages across the records (already filtered to one WC).

    `records` is a list of dicts with keys: day, person, wc, units,
    downtime, hours — same shape as production_history.daily_records().

    `target_per_hour` is the hourly target for this WC.

    `productive_minutes_for(day)` returns productive minutes for that day,
    honoring per-day custom_hours. Inject shift_config.productive_minutes_for.

    `mode` is 'units' or 'pct' — drives the sort.

    Returns rows sorted by the active metric desc, with rank assigned.
    Days where the operator earned zero units are excluded so they don't
    drag down the average. Tiebreak: more days_worked ranks higher.
    """
    rows = [r for r in records if r["units"] > 0]
    by_person: dict[str, list[dict]] = {}
    for r in rows:
        by_person.setdefault(r["person"], []).append(r)

    out: list[dict] = []
    for person, recs in by_person.items():
        days_worked = len(recs)
        total_units = sum(r["units"] for r in recs)
        avg_units = total_units / days_worked

        pct_per_day: list[float] = []
        for r in recs:
            prod_hr = productive_minutes_for(r["day"]) / 60.0
            expected = target_per_hour * prod_hr
            pct_per_day.append((r["units"] / expected) if expected > 0 else 0.0)
        avg_pct = sum(pct_per_day) / len(pct_per_day) if pct_per_day else 0.0

        out.append({
            "name": person,
            "name_count": days_worked,
            "avg_units": avg_units,
            "avg_pct": avg_pct,
        })

    if mode == "pct":
        out.sort(key=lambda r: (-r["avg_pct"], -r["name_count"], r["name"].lower()))
    else:
        out.sort(key=lambda r: (-r["avg_units"], -r["name_count"], r["name"].lower()))

    for i, row in enumerate(out, 1):
        row["rank"] = i
    return out


def averages_for_group(
    records: list[dict],
    target_per_hour_by_wc: dict[str, float],
    productive_minutes_for,
    mode: str,
) -> list[dict]:
    """Per-person averages across a group's WCs.

    Each (person, day, wc) record is one sample. `expected` for the
    pct math is computed per record using that record's WC target.
    `top_wc` = the WC the operator most often worked in the range
    (highest day count); ties broken by WC name alphabetical.

    Same filtering, sorting, and tiebreak rules as averages_for_wc.
    """
    rows = [r for r in records if r["units"] > 0]
    by_person: dict[str, list[dict]] = {}
    for r in rows:
        by_person.setdefault(r["person"], []).append(r)

    out: list[dict] = []
    for person, recs in by_person.items():
        days_worked = len(recs)
        total_units = sum(r["units"] for r in recs)
        avg_units = total_units / days_worked

        pct_per_day: list[float] = []
        wc_counts: dict[str, int] = {}
        for r in recs:
            wc_counts[r["wc"]] = wc_counts.get(r["wc"], 0) + 1
            prod_hr = productive_minutes_for(r["day"]) / 60.0
            target = target_per_hour_by_wc.get(r["wc"], 0.0)
            expected = target * prod_hr
            pct_per_day.append((r["units"] / expected) if expected > 0 else 0.0)
        avg_pct = sum(pct_per_day) / len(pct_per_day) if pct_per_day else 0.0

        # top_wc: highest count; tiebreak alphabetical by WC name.
        top_wc = min(wc_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]

        out.append({
            "name": person,
            "name_count": days_worked,
            "top_wc": top_wc,
            "avg_units": avg_units,
            "avg_pct": avg_pct,
        })

    if mode == "pct":
        out.sort(key=lambda r: (-r["avg_pct"], -r["name_count"], r["name"].lower()))
    else:
        out.sort(key=lambda r: (-r["avg_units"], -r["name_count"], r["name"].lower()))

    for i, row in enumerate(out, 1):
        row["rank"] = i
    return out


router = APIRouter()


@router.get("/staffing/leaderboards", response_class=HTMLResponse)
def staffing_leaderboards(
    request: Request,
    window: str = Query(default="week"),
    metric: str = Query(default="pct"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    from .. import cert_lookup
    from .. import leaderboard_settings_store as lstore
    from .. import production_history
    from .. import work_centers_store
    from .._http_cache import (
        get_cached_response, range_includes_today, set_cache_headers,
        store_cached_response,
    )

    today_d = plant_today()
    start_d, end_d, custom_range_active = resolve_range(window, start, end, today_d)
    includes_today = range_includes_today(end_d, today_d)

    # Server-side HTML response cache, same pattern /recycling uses.
    # Heavy route: 22-WC loop × per-person-day records over the range.
    cache_key = (
        "staffing_leaderboards", window, metric,
        start_d.isoformat(), end_d.isoformat(),
    )
    cached = get_cached_response(cache_key, includes_today=includes_today)
    if cached is not None:
        return cached

    person_certs = cert_lookup.load_person_certs()

    records = production_history.daily_records(start_d, end_d)
    # Bucket once by WC so the per-WC and per-group sections below reuse the
    # buckets instead of rescanning the full record list per section.
    records_by_wc: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        records_by_wc[r["wc"]].append(r)

    snap = lstore.snapshot()
    wc_settings_dict = snap.get("wc", {})
    group_settings_dict = snap.get("group", {})
    wc_avg_settings_dict = snap.get("wc-avg", {})
    group_avg_settings_dict = snap.get("group-avg", {})

    # Per-WC top-5 (best days) + per-WC averages computation.
    from .. import shift_config

    # Memoize productive minutes per day for this request — averages_for_wc /
    # averages_for_group otherwise recompute it once per record per section.
    _pm_by_day: dict[date, float] = {}

    def _productive_minutes_cached(day):
        v = _pm_by_day.get(day)
        if v is None:
            v = shift_config.productive_minutes_for(day)
            _pm_by_day[day] = v
        return v

    sections = []
    avg_sections = []
    for loc in staffing.LOCATIONS:
        station = Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)
        target_per_day = settings_store.station_target_per_day(station)
        target_per_hour = settings_store.station_target(station)
        wc_records = records_by_wc.get(loc.name, [])

        # --- Best Days (existing top-5) ---
        def metric_value(r):
            if metric == "units":
                return r["units"]
            if target_per_day <= 0:
                return 0.0
            return r["units"] / target_per_day

        # sorted() copy — the shared bucket must stay unmutated for the
        # group sections below.
        wc_records = sorted(wc_records, key=lambda r: (-metric_value(r), r["day"]))
        top = wc_records[:5]

        name_counts: dict[str, int] = {}
        for r in wc_records:
            name_counts[r["person"]] = name_counts.get(r["person"], 0) + 1

        rows = []
        for i, r in enumerate(top, start=1):
            day = r["day"]
            day_label = f"{day.strftime('%a')} {day.month}/{day.day}"
            expected = target_per_day
            pct = (r["units"] / expected) if expected > 0 else 0.0
            rows.append({
                "rank": i,
                "name": r["person"],
                "name_count": name_counts.get(r["person"], 0),
                "day": day.isoformat(),
                "day_label": day_label,
                "units": r["units"],
                "expected": expected,
                "pct": pct,
            })

        wc_settings = wc_settings_dict.get(loc.name, {"sort_order": 0, "is_inactive": False})
        # auto_inactive: empty when there's no production AT ALL for the WC in the
        # range. Both halves use the same flag — they share a row.
        auto_inactive = not wc_records
        sections.append({
            "loc_name": loc.name,
            "rows": rows,
            "is_inactive": wc_settings["is_inactive"] or auto_inactive,
            "is_manually_inactive": wc_settings["is_inactive"],
            "is_auto_empty": auto_inactive and not wc_settings["is_inactive"],
            "sort_order": wc_settings["sort_order"],
        })

        # --- Best Averages (new) ---
        wc_avg_settings = wc_avg_settings_dict.get(loc.name, {"sort_order": 0, "is_inactive": False})
        avg_auto_inactive = not wc_records
        avg_rows = averages_for_wc(
            wc_records, target_per_hour, _productive_minutes_cached, metric,
        )
        avg_sections.append({
            "loc_name": loc.name,
            "rows": avg_rows,
            "is_inactive": wc_avg_settings["is_inactive"] or avg_auto_inactive,
            "is_manually_inactive": wc_avg_settings["is_inactive"],
            "is_auto_empty": avg_auto_inactive and not wc_avg_settings["is_inactive"],
            "sort_order": wc_avg_settings["sort_order"],
        })

    # Stable secondary sort by LOCATIONS index (bay-organized natural order).
    loc_index = {loc.name: i for i, loc in enumerate(staffing.LOCATIONS)}
    sort_key = lambda s: (s["sort_order"], loc_index.get(s["loc_name"], 999))
    active_sections = sorted([s for s in sections if not s["is_inactive"]], key=sort_key)
    inactive_sections = sorted([s for s in sections if s["is_inactive"]], key=sort_key)
    active_avg_sections = sorted([s for s in avg_sections if not s["is_inactive"]], key=sort_key)
    inactive_avg_sections = sorted([s for s in avg_sections if s["is_inactive"]], key=sort_key)

    # Per-group top-5 (best days) + per-group averages computation.
    group_sections = []
    avg_group_sections = []
    for group_name in work_centers_store.registered_groups():
        member_locs = work_centers_store.members("group", group_name)
        member_names = {loc.name for loc in member_locs}
        if not member_names:
            continue
        g_records = [
            r for loc in member_locs for r in records_by_wc.get(loc.name, [])
        ]
        target_by_wc = {
            loc.name: settings_store.station_target_per_day(
                Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)
            )
            for loc in member_locs
        }
        target_per_hour_by_wc = {
            loc.name: settings_store.station_target(
                Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)
            )
            for loc in member_locs
        }

        def metric_value_g(r, _target_by_wc=target_by_wc):
            if metric == "units":
                return r["units"]
            t = _target_by_wc.get(r["wc"], 0)
            return (r["units"] / t) if t > 0 else 0.0

        g_records.sort(key=lambda r: (-metric_value_g(r), r["day"]))
        top = g_records[:5]

        counts: dict[str, int] = {}
        for r in g_records:
            counts[r["person"]] = counts.get(r["person"], 0) + 1

        rows = []
        for i, r in enumerate(top, start=1):
            day = r["day"]
            target = target_by_wc.get(r["wc"], 0)
            rows.append({
                "rank": i,
                "name": r["person"],
                "name_count": counts.get(r["person"], 0),
                "day": day.isoformat(),
                "day_label": f"{day.strftime('%a')} {day.month}/{day.day}",
                "wc": r["wc"],
                "units": r["units"],
                "pct": (r["units"] / target) if target > 0 else 0.0,
            })

        g_set = group_settings_dict.get(group_name, {"sort_order": 0, "is_inactive": False})
        auto_inactive = not g_records
        group_sections.append({
            "loc_name": group_name,
            "rows": rows,
            "is_inactive": g_set["is_inactive"] or auto_inactive,
            "is_manually_inactive": g_set["is_inactive"],
            "is_auto_empty": auto_inactive and not g_set["is_inactive"],
            "sort_order": g_set["sort_order"],
        })

        # --- Best Averages for this group (new) ---
        g_avg_set = group_avg_settings_dict.get(group_name, {"sort_order": 0, "is_inactive": False})
        avg_auto_inactive = not g_records
        avg_rows = averages_for_group(
            g_records, target_per_hour_by_wc, _productive_minutes_cached, metric,
        )
        avg_group_sections.append({
            "loc_name": group_name,
            "rows": avg_rows,
            "is_inactive": g_avg_set["is_inactive"] or avg_auto_inactive,
            "is_manually_inactive": g_avg_set["is_inactive"],
            "is_auto_empty": avg_auto_inactive and not g_avg_set["is_inactive"],
            "sort_order": g_avg_set["sort_order"],
        })

    active_groups = sorted(
        [s for s in group_sections if not s["is_inactive"]],
        key=lambda s: s["sort_order"],
    )
    inactive_groups = sorted(
        [s for s in group_sections if s["is_inactive"]],
        key=lambda s: s["sort_order"],
    )
    active_avg_groups = sorted(
        [s for s in avg_group_sections if not s["is_inactive"]],
        key=lambda s: s["sort_order"],
    )
    inactive_avg_groups = sorted(
        [s for s in avg_group_sections if s["is_inactive"]],
        key=lambda s: s["sort_order"],
    )

    response = templates.TemplateResponse(
        request,
        "leaderboards.html",
        {
            "active": "leaderboards",
            "active_sections": active_sections,
            "inactive_sections": inactive_sections,
            "active_groups": active_groups,
            "inactive_groups": inactive_groups,
            "active_avg_sections": active_avg_sections,
            "inactive_avg_sections": inactive_avg_sections,
            "active_avg_groups": active_avg_groups,
            "inactive_avg_groups": inactive_avg_groups,
            "window": window,
            "metric": metric,
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "today": today_d.isoformat(),
            "custom_range_active": custom_range_active,
            "person_certs": person_certs,
        },
    )
    set_cache_headers(response, includes_today=includes_today)
    store_cached_response(cache_key, includes_today=includes_today, response=response)
    return response


@router.post("/staffing/leaderboards/order")
async def leaderboards_set_order(request: Request, kind: str = Query(default="wc")):
    from .. import leaderboard_settings_store as lstore
    if kind not in ("wc", "group", "wc-avg", "group-avg"):
        return JSONResponse({"ok": False, "error": "invalid kind"}, status_code=400)
    body = await request.json()
    order = body.get("order") or []
    if not isinstance(order, list):
        return JSONResponse({"ok": False, "error": "order must be a list"}, status_code=400)

    def _work():
        lstore.set_order(kind, [str(x) for x in order if isinstance(x, str)])
        from .. import _http_cache
        _http_cache.invalidate_all_cache()
        return JSONResponse({"ok": True})

    return await asyncio.to_thread(_work)


@router.post("/staffing/leaderboards/wc/{name}/inactive")
def leaderboards_set_inactive(name: str, kind: str = Query(default="wc")):
    from .. import leaderboard_settings_store as lstore
    if kind not in ("wc", "group", "wc-avg", "group-avg"):
        return JSONResponse({"ok": False, "error": "invalid kind"}, status_code=400)
    lstore.set_inactive(kind, name, True)
    from .. import _http_cache
    _http_cache.invalidate_all_cache()
    return JSONResponse({"ok": True})


@router.post("/staffing/leaderboards/wc/{name}/active")
def leaderboards_set_active(name: str, kind: str = Query(default="wc")):
    from .. import leaderboard_settings_store as lstore
    if kind not in ("wc", "group", "wc-avg", "group-avg"):
        return JSONResponse({"ok": False, "error": "invalid kind"}, status_code=400)
    lstore.set_inactive(kind, name, False)
    from .. import _http_cache
    _http_cache.invalidate_all_cache()
    return JSONResponse({"ok": True})


@router.get("/api/staffing/leaderboards/person-days")
def person_days_json(
    name: str = Query(...),
    wc: str | None = Query(default=None),
    group: str | None = Query(default=None),
    start: str = Query(...),
    end: str = Query(...),
):
    """Per-day breakdown of a person's production within a scope (a single
    WC or a category group) over [start, end] inclusive. Used by the
    leaderboards averages popup. Returns rows sorted newest-first.
    Cached per (name, scope, range) — 60s TTL when the range includes
    today, 1h TTL otherwise.
    """
    if (wc and group) or (not wc and not group):
        return JSONResponse({"error": "exactly one of wc / group must be set"}, status_code=400)
    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except (ValueError, TypeError):
        return JSONResponse({"error": "start / end must be YYYY-MM-DD"}, status_code=400)
    if end_d < start_d:
        return JSONResponse({"error": "end must be on or after start"}, status_code=400)

    scope_key = f"wc:{wc}" if wc else f"group:{group}"
    cache_key = (name, scope_key, start_d.isoformat(), end_d.isoformat())
    today = plant_today()
    includes_today = start_d <= today <= end_d
    cache = _PERSON_DAYS_CACHE_TODAY if includes_today else _PERSON_DAYS_CACHE_PAST
    cached = cache.peek(cache_key)
    if cached is not None:
        return JSONResponse(cached)

    if wc:
        wc_filter = {wc}
    else:
        # Leaderboards "groups" are user-defined memberships maintained in
        # work_centers_store, NOT the loc.skill category. Resolve the group
        # name to its WC members exactly the way the leaderboards page does.
        from .. import work_centers_store
        wc_filter = {loc.name for loc in work_centers_store.members("group", group)}
        if not wc_filter:
            payload = {"rows": []}
            cache.set(cache_key, payload)
            return JSONResponse(payload)

    rows: list[dict] = []
    for day, daily in attribution_per_day(start_d, end_d):
        person_data = daily.get(name, {})
        matching = {w: t for w, t in person_data.items() if w in wc_filter}
        if not matching:
            continue
        rows.append({
            "date": day.isoformat(),
            "wcs": sorted(matching.keys()),
            "units": sum(t["units"] for t in matching.values()),
            "downtime": sum(t["downtime"] for t in matching.values()),
        })
    rows.sort(key=lambda r: r["date"], reverse=True)
    payload = {"rows": rows}
    cache.set(cache_key, payload)
    return JSONResponse(payload)
