"""Per-skill production leaderboards: GET /staffing/leaderboards."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import settings_store, staffing
from ..deps import _window_dates, client, templates
from ..stations import Station


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
    from datetime import date as _date
    from .. import cert_lookup
    from .. import leaderboard_settings_store as lstore
    from .. import production_history
    from .. import work_centers_store

    person_certs = cert_lookup.load_person_certs()

    today_d = datetime.now(timezone.utc).date()
    custom_range_active = False
    if start and end:
        try:
            start_d = _date.fromisoformat(start)
            end_d = _date.fromisoformat(end)
            if end_d >= start_d:
                custom_range_active = True
        except ValueError:
            start_d, end_d = _window_dates(window, today_d)
    if not custom_range_active:
        start_d, end_d = _window_dates(window, today_d)

    records = production_history.daily_records(start_d, end_d, client)

    snap = lstore.snapshot()
    wc_settings_dict = snap.get("wc", {})
    group_settings_dict = snap.get("group", {})

    # Per-WC top-5 computation.
    sections = []
    for loc in staffing.LOCATIONS:
        target_per_day = settings_store.station_target_per_day(
            Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)
        )
        wc_records = [r for r in records if r["wc"] == loc.name]

        # Compute metric per record + sort.
        def metric_value(r):
            if metric == "units":
                return r["units"]
            if target_per_day <= 0:
                return 0.0
            return r["units"] / target_per_day

        # Sort: metric desc; tiebreak by ascending day (oldest first).
        wc_records.sort(key=lambda r: (-metric_value(r), r["day"]))
        top = wc_records[:5]

        # Per-row name_count = total days that operator worked at this WC across the whole range.
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
        auto_inactive = not rows
        sections.append({
            "loc_name": loc.name,
            "rows": rows,
            "is_inactive": wc_settings["is_inactive"] or auto_inactive,
            "is_manually_inactive": wc_settings["is_inactive"],
            "is_auto_empty": auto_inactive and not wc_settings["is_inactive"],
            "sort_order": wc_settings["sort_order"],
        })

    # Stable secondary sort by LOCATIONS index (bay-organized natural order).
    loc_index = {loc.name: i for i, loc in enumerate(staffing.LOCATIONS)}
    sort_key = lambda s: (s["sort_order"], loc_index.get(s["loc_name"], 999))
    active_sections = sorted([s for s in sections if not s["is_inactive"]], key=sort_key)
    inactive_sections = sorted([s for s in sections if s["is_inactive"]], key=sort_key)

    # Per-group top-5 computation.
    group_sections = []
    for group_name in work_centers_store.registered_groups():
        member_locs = work_centers_store.members("group", group_name)
        member_names = {loc.name for loc in member_locs}
        if not member_names:
            continue
        g_records = [r for r in records if r["wc"] in member_names]
        target_by_wc = {
            loc.name: settings_store.station_target_per_day(
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
        auto_inactive = not rows
        group_sections.append({
            "loc_name": group_name,
            "rows": rows,
            "is_inactive": g_set["is_inactive"] or auto_inactive,
            "is_manually_inactive": g_set["is_inactive"],
            "is_auto_empty": auto_inactive and not g_set["is_inactive"],
            "sort_order": g_set["sort_order"],
        })

    active_groups = sorted(
        [s for s in group_sections if not s["is_inactive"]],
        key=lambda s: s["sort_order"],
    )
    inactive_groups = sorted(
        [s for s in group_sections if s["is_inactive"]],
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
            "window": window,
            "metric": metric,
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "today": today_d.isoformat(),
            "custom_range_active": custom_range_active,
            "person_certs": person_certs,
        },
    )
    from .._http_cache import set_cache_headers, range_includes_today
    set_cache_headers(response, includes_today=range_includes_today(end_d, today_d))
    return response


@router.post("/staffing/leaderboards/order")
async def leaderboards_set_order(request: Request, kind: str = Query(default="wc")):
    from .. import leaderboard_settings_store as lstore
    if kind not in ("wc", "group"):
        return JSONResponse({"ok": False, "error": "invalid kind"}, status_code=400)
    body = await request.json()
    order = body.get("order") or []
    if not isinstance(order, list):
        return JSONResponse({"ok": False, "error": "order must be a list"}, status_code=400)
    lstore.set_order(kind, [str(x) for x in order if isinstance(x, str)])
    return JSONResponse({"ok": True})


@router.post("/staffing/leaderboards/wc/{name}/inactive")
def leaderboards_set_inactive(name: str, kind: str = Query(default="wc")):
    from .. import leaderboard_settings_store as lstore
    if kind not in ("wc", "group"):
        return JSONResponse({"ok": False, "error": "invalid kind"}, status_code=400)
    lstore.set_inactive(kind, name, True)
    return JSONResponse({"ok": True})


@router.post("/staffing/leaderboards/wc/{name}/active")
def leaderboards_set_active(name: str, kind: str = Query(default="wc")):
    from .. import leaderboard_settings_store as lstore
    if kind not in ("wc", "group"):
        return JSONResponse({"ok": False, "error": "invalid kind"}, status_code=400)
    lstore.set_inactive(kind, name, False)
    return JSONResponse({"ok": True})
