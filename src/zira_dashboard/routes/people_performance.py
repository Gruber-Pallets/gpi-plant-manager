"""Authenticated People Performance dashboard and its live row partial."""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from .. import _http_cache
from ..deps import client as zira_client
from ..deps import templates
from ..people_performance_data import load_dashboard
from ..people_performance_view import dashboard_context
from ..plant_day import today as plant_today


router = APIRouter()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _request_clock() -> tuple[datetime, date]:
    """Capture one clock value so date, data, and cache decisions agree."""
    now_utc = _utc_now()
    if now_utc.utcoffset() is None:
        raise RuntimeError("People Performance requires an aware request clock")
    now_utc = now_utc.astimezone(UTC)
    return now_utc, plant_today(now_utc)


def _selected_day(raw: date | None, *, today: date) -> date:
    selected = raw or today
    if selected > today:
        raise HTTPException(status_code=400, detail="Choose today or an earlier day")
    return selected


def _context(
    day: date,
    *,
    attention: bool,
    now_utc: datetime,
    today: date,
) -> dict:
    model = load_dashboard(day, zira_client, now_utc=now_utc)
    return {
        **dashboard_context(model, attention_only=attention),
        "active": "people",
        "today": today.isoformat(),
        "rows_url": "/people-performance/rows",
    }


@router.get("/people-performance", response_class=HTMLResponse)
def people_performance(
    request: Request,
    day: date | None = Query(default=None),
    attention: bool = Query(default=False),
):
    now_utc, today = _request_clock()
    selected = _selected_day(day, today=today)
    response = templates.TemplateResponse(
        request,
        "people_performance.html",
        _context(
            selected,
            attention=attention,
            now_utc=now_utc,
            today=today,
        ),
    )
    if selected == today:
        _http_cache.set_cache_headers(response, includes_today=True)
    else:
        response.headers["Cache-Control"] = "private, no-cache"
    return response


@router.get("/people-performance/rows", response_class=HTMLResponse)
def people_performance_rows(
    request: Request,
    day: date | None = Query(default=None),
    attention: bool = Query(default=False),
):
    now_utc, today = _request_clock()
    selected = _selected_day(day, today=today)
    response = templates.TemplateResponse(
        request,
        "_people_performance_rows.html",
        _context(
            selected,
            attention=attention,
            now_utc=now_utc,
            today=today,
        ),
    )
    response.headers["Cache-Control"] = "no-store"
    # Task 7 checks this marker plus Response.redirected before replacing live
    # content. A followed sign-in redirect or another HTML response will not
    # carry the marker, so stale metrics are never mistaken for a fresh partial.
    response.headers["X-People-Performance-Response"] = "rows"
    return response


__all__ = ["router"]
