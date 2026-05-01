"""Time-off calendar route: GET /staffing/time-off."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from .. import _http_cache, stratustime_client
from ..deps import templates

router = APIRouter()


def _time_off_by_day(start_d: date, end_d: date) -> dict[date, list[dict]]:
    """Return {day: [time_off_entry, ...]} for [start_d, end_d] inclusive,
    sourced from StratusTime via a single bulk fetch (one StratusTime call
    per data type for the whole range, vs N per-day calls)."""
    try:
        return stratustime_client.time_off_entries_for_range(start_d, end_d)
    except Exception:
        # Fallback: per-day path so a transient StratusTime hiccup never
        # blanks the whole calendar.
        out: dict[date, list[dict]] = {}
        cursor = start_d
        while cursor <= end_d:
            try:
                out[cursor] = stratustime_client.time_off_entries_for_day(cursor)
            except Exception:
                out[cursor] = []
            cursor += timedelta(days=1)
        return out


@router.get("/staffing/time-off", response_class=HTMLResponse)
def staffing_time_off(
    request: Request,
    scale: str = Query(default="month"),
    date_: str | None = Query(default=None, alias="date"),
):
    scale = scale if scale in {"day", "week", "month", "quarter", "year"} else "month"
    today = datetime.now(timezone.utc).date()
    try:
        cursor = date.fromisoformat(date_) if date_ else today
    except ValueError:
        cursor = today

    # Server-side response cache: today bucket (15s) for any view that
    # includes today; past bucket (5min) for purely historical views.
    is_today = cursor >= today  # conservative — if cursor is on/after today, treat as live
    cache_key = ("time_off", scale, cursor.isoformat())
    cached_resp = _http_cache.get_cached_response(cache_key, includes_today=is_today)
    if cached_resp is not None:
        return cached_resp

    # Compute the visible date range so we only fetch StratusTime data for what
    # the user actually sees (cached 5 min per day inside the client).
    if scale == "day":
        range_start = cursor
        range_end = cursor
    elif scale == "week":
        monday = cursor - timedelta(days=cursor.weekday())
        range_start = monday
        range_end = monday + timedelta(days=6)
    elif scale == "month":
        first = cursor.replace(day=1)
        # Month grid includes leading + trailing days from adjacent months.
        range_start = first - timedelta(days=first.weekday())
        if first.month == 12:
            next_first = first.replace(year=first.year + 1, month=1)
        else:
            next_first = first.replace(month=first.month + 1)
        last = next_first - timedelta(days=1)
        # Trailing days to fill out the last week (Mon-start, Sun-end).
        range_end = last + timedelta(days=(6 - last.weekday()))
    elif scale == "quarter":
        q_start_month = ((cursor.month - 1) // 3) * 3 + 1
        range_start = date(cursor.year, q_start_month, 1)
        # End of quarter = day before the start of the next quarter.
        end_month = q_start_month + 3
        end_year = cursor.year + (1 if end_month > 12 else 0)
        end_month = ((end_month - 1) % 12) + 1
        range_end = date(end_year, end_month, 1) - timedelta(days=1)
    else:  # year
        range_start = date(cursor.year, 1, 1)
        range_end = date(cursor.year, 12, 31)

    off_map = _time_off_by_day(range_start, range_end)

    import calendar as _cal
    ctx: dict = {
        "active": "time_off",
        "scale": scale,
        "cursor_iso": cursor.isoformat(),
        "today_iso": today.isoformat(),
    }

    def _month_cells(year: int, month: int):
        weeks = _cal.Calendar(firstweekday=0).monthdatescalendar(year, month)
        out = []
        for week in weeks:
            w = []
            for d in week:
                w.append({
                    "num": d.day,
                    "outside": d.month != month,
                    "is_today": d == today,
                    "weekend": d.weekday() >= 5,
                    "names": off_map.get(d, []),
                    "count": len(off_map.get(d, [])),
                })
            out.append(w)
        return out

    if scale == "day":
        ctx["heading"] = cursor.strftime("%A · %B %d, %Y").replace(" 0", " ")
        ctx["cursor_label"] = ctx["heading"]
        ctx["day_names"] = off_map.get(cursor, [])
        ctx["prev_date"] = (cursor - timedelta(days=1)).isoformat()
        ctx["next_date"] = (cursor + timedelta(days=1)).isoformat()
    elif scale == "week":
        # Week starting Monday.
        monday = cursor - timedelta(days=cursor.weekday())
        days = []
        for i in range(7):
            d = monday + timedelta(days=i)
            days.append({
                "label": d.strftime("%a"),
                "num": d.day,
                "iso": d.isoformat(),
                "is_today": d == today,
                "names": off_map.get(d, []),
            })
        ctx["heading"] = f"Week of {monday.isoformat()}"
        ctx["week_days"] = days
        ctx["prev_date"] = (monday - timedelta(days=7)).isoformat()
        ctx["next_date"] = (monday + timedelta(days=7)).isoformat()
    elif scale == "month":
        ctx["heading"] = cursor.strftime("%B %Y")
        ctx["month_weeks"] = _month_cells(cursor.year, cursor.month)
        # prev / next month
        prev_m = (cursor.replace(day=1) - timedelta(days=1)).replace(day=1)
        # next month
        if cursor.month == 12:
            next_m = cursor.replace(year=cursor.year + 1, month=1, day=1)
        else:
            next_m = cursor.replace(month=cursor.month + 1, day=1)
        ctx["prev_date"] = prev_m.isoformat()
        ctx["next_date"] = next_m.isoformat()
    elif scale == "quarter":
        q_start_month = ((cursor.month - 1) // 3) * 3 + 1
        months = []
        for i in range(3):
            m = q_start_month + i
            y = cursor.year
            months.append({"label": date(y, m, 1).strftime("%B %Y"), "weeks": _month_cells(y, m)})
        ctx["heading"] = f"Q{(q_start_month - 1) // 3 + 1} {cursor.year}"
        ctx["quarter_months"] = months
        ctx["prev_date"] = (date(cursor.year, q_start_month, 1) - timedelta(days=1)).isoformat()
        end = date(cursor.year + (1 if q_start_month + 3 > 12 else 0), ((q_start_month + 2) % 12) + 1, 1)
        ctx["next_date"] = end.isoformat()
    else:  # year
        months = [{"label": date(cursor.year, m, 1).strftime("%b"), "weeks": _month_cells(cursor.year, m)} for m in range(1, 13)]
        ctx["heading"] = str(cursor.year)
        ctx["year_months"] = months
        ctx["prev_date"] = date(cursor.year - 1, cursor.month, 1).isoformat()
        ctx["next_date"] = date(cursor.year + 1, cursor.month, 1).isoformat()

    response = templates.TemplateResponse(request, "time_off.html", ctx)
    _http_cache.set_cache_headers(response, includes_today=is_today)
    _http_cache.store_cached_response(cache_key, includes_today=is_today, response=response)
    return response
