"""Top-level dashboard routes: GET /work-centers and GET /api/leaderboard.

GET / redirects to /recycling so the home page lands on the Recycling VS
dashboard. The Work Centers page (formerly served at /) now lives at
/work-centers and is reachable from the Dashboards subnav.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..deps import (
    _filter_stations,
    _fmt_duration,
    _parse_day,
    _relative,
    _state,
    client,
    templates,
)
from ..leaderboard import cached_leaderboard as leaderboard
from ..stations import CATEGORIES

router = APIRouter()


@router.get("/")
def home():
    return RedirectResponse(url="/recycling", status_code=307)


@router.get("/work-centers", response_class=HTMLResponse)
def index(
    request: Request,
    day: str | None = Query(default=None),
    category: str | None = Query(default=None),
):
    d = _parse_day(day)
    today = datetime.now(timezone.utc).date()
    is_today = d == today
    # Try cached HTML response.
    from .._http_cache import get_cached_response, set_cache_headers, store_cached_response
    cache_key = ("work_centers", d.isoformat(), category or "")
    cached = get_cached_response(cache_key, includes_today=is_today)
    if cached is not None:
        return cached
    stations = _filter_stations(category)
    now = datetime.now(timezone.utc)
    results = leaderboard(client, stations, d, now_utc=now if is_today else None)

    enriched = []
    counts = {"Running": 0, "Stopped": 0, "Offline": 0}
    for r in results:
        state = _state(r, now, is_today)
        if state in counts:
            counts[state] += 1
        enriched.append(
            {
                "station": r.station,
                "units": r.units,
                "reading_count": r.reading_count,
                "truncated": r.truncated,
                "downtime_minutes": r.downtime_minutes,
                "downtime_display": _fmt_duration(r.downtime_minutes),
                "last_reading_at": r.last_reading_at,
                "last_relative": _relative(r.last_reading_at, now),
                "last_status": r.last_status,
                "state": state,
            }
        )

    top = max((r.units for r in results), default=0)
    category_order = {"Dismantler": 0, "Repair": 1, "Other": 2}
    by_category: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    for row in enriched:
        by_category[row["station"].category].append(row)

    response = templates.TemplateResponse(
        request,
        "index.html",
        {
            "day": d.isoformat(),
            "today": today.isoformat(),
            "is_today": is_today,
            "category": category or "All",
            "categories": ("All",) + CATEGORIES,
            "ordered_categories": sorted(CATEGORIES, key=lambda c: category_order.get(c, 99)),
            "rows": enriched,
            "by_category": by_category,
            "counts": counts,
            "top_units": top,
            "refreshed_at": now.strftime("%H:%M:%S UTC"),
            "active_vs": "work_centers",
        },
    )
    set_cache_headers(response, includes_today=is_today)
    store_cached_response(cache_key, includes_today=is_today, response=response)
    return response


@router.get("/api/leaderboard")
def api_leaderboard(
    day: str | None = Query(default=None),
    category: str | None = Query(default=None),
):
    d = _parse_day(day)
    today = datetime.now(timezone.utc).date()
    is_today = d == today
    stations = _filter_stations(category)
    now = datetime.now(timezone.utc)
    results = leaderboard(client, stations, d, now_utc=now if is_today else None)
    return JSONResponse(
        {
            "day": d.isoformat(),
            "category": category or "All",
            "stations": [
                {
                    "rank": i + 1,
                    "name": r.station.name,
                    "category": r.station.category,
                    "meter_id": r.station.meter_id,
                    "units": r.units,
                    "reading_count": r.reading_count,
                    "truncated": r.truncated,
                    "downtime_minutes": r.downtime_minutes,
                    "last_reading_at": r.last_reading_at.isoformat() if r.last_reading_at else None,
                    "last_status": r.last_status,
                    "state": _state(r, now, is_today),
                }
                for i, r in enumerate(results)
            ],
        }
    )
