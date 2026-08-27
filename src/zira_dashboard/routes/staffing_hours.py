"""Read-only Staffing Hours report page."""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from .. import odoo_client, staffing
from .. import staffing_hours as hours
from ..deps import templates
from ..plant_day import now as plant_now
from ..plant_day import today as plant_today


log = logging.getLogger(__name__)
router = APIRouter()

_SOURCES = (
    ("clocked", "Clocked time"),
    ("payroll", "Payroll hours"),
)
_RANGES = (
    ("this_week", "This week"),
    ("last_week", "Last week"),
    ("this_pay_period", "This pay period"),
    ("last_pay_period", "Last pay period"),
    ("this_month", "This month"),
    ("last_month", "Last month"),
    ("custom", "Custom range"),
)
_ATTENTION_FILTERS = {"all", "approaching_40", "over_40", "attention"}


def _employee_ids(roster: list[object]) -> tuple[int, ...]:
    """Return active non-reserve Odoo ids once, before calling Odoo reads."""
    return tuple(sorted({
        person.employee_id
        for person in roster
        if getattr(person, "active", False)
        and not getattr(person, "reserve", False)
        and isinstance(getattr(person, "employee_id", None), int)
        and not isinstance(getattr(person, "employee_id", None), bool)
    }))


def _active_roster() -> list[object]:
    """Hours are for current, non-reserve plant staff only."""
    return [
        person
        for person in staffing.load_roster()
        if getattr(person, "active", False)
        and not getattr(person, "reserve", False)
        and isinstance(getattr(person, "employee_id", None), int)
        and not isinstance(getattr(person, "employee_id", None), bool)
    ]


def _load_complete_report(source, resolution, query, department, attention):
    """Fetch every required source before returning a report.

    A failure in any attendance, work-entry, employee, or roster read raises to
    the route.  The route then renders no rows, rather than a partial report.
    """
    if source not in {item[0] for item in _SOURCES}:
        raise ValueError("Choose clocked time or payroll hours.")
    if attention not in _ATTENTION_FILTERS:
        raise ValueError("Choose a valid hours filter.")

    roster = _active_roster()
    employee_ids = _employee_ids(roster)
    departments = (
        odoo_client.fetch_employee_departments(employee_ids)
        if employee_ids else {}
    )
    if source == "clocked":
        attendances = (
            odoo_client.fetch_attendance_intervals_for_range(
                employee_ids, resolution.start, resolution.end
            )
            if employee_ids else []
        )
        work_entries = []
    else:
        attendances = []
        work_entries = (
            odoo_client.fetch_payroll_work_entries(
                employee_ids, resolution.start, resolution.end
            )
            if employee_ids else []
        )

    return hours.build_hours_report(
        source=source,
        roster=roster,
        start=resolution.start,
        end=resolution.end,
        now=plant_now(),
        attendances=attendances,
        work_entries=work_entries,
        departments=departments,
        query=query,
        department=department,
        attention=attention,
    )


def _query_string(
    *, source: str, selected_range: str, start: str | None, end: str | None,
    query: str, department: str, attention: str,
) -> str:
    return urlencode({
        "source": source,
        "range": selected_range,
        "start": start or "",
        "end": end or "",
        "q": query,
        "department": department,
        "attention": attention,
    })


def _report_url(**values: str) -> str:
    return f"/staffing/hours?{_query_string(**values)}"


def _range_label(resolution) -> str:
    if resolution.error:
        return ""
    return f"{resolution.start.strftime('%b %-d, %Y')} – {resolution.end.strftime('%b %-d, %Y')}"


def _render_hours(
    request: Request, *, source: str, selected_range: str, start: str | None,
    end: str | None, query: str, department: str, attention: str, resolution,
    report, error: str | None,
):
    query_values = {
        "source": source,
        "selected_range": selected_range,
        "start": start,
        "end": end,
        "query": query,
        "department": department,
        "attention": attention,
    }
    source_links = [
        {
            "value": value,
            "label": label,
            "url": _report_url(**(query_values | {"source": value})),
        }
        for value, label in _SOURCES
    ]
    range_links = [
        {
            "value": value,
            "label": label,
            "url": _report_url(**(query_values | {"selected_range": value})),
        }
        for value, label in _RANGES
        if value != "custom"
    ]
    summary_filters = [
        {
            "value": value,
            "label": label,
            "url": _report_url(**(query_values | {"attention": value})),
        }
        for value, label in (
            ("approaching_40", "36–39.99 hours"),
            ("over_40", "40+ hours"),
            ("attention", "Needs attention"),
        )
    ]
    return templates.TemplateResponse(
        request,
        "staffing_hours.html",
        {
            "active": "hours",
            "source": source,
            "source_label": dict(_SOURCES).get(source, "Hours"),
            "selected_range": selected_range,
            "start": start or "",
            "end": end or "",
            "query": query,
            "department": department,
            "attention": attention,
            "source_links": source_links,
            "range_links": range_links,
            "summary_filters": summary_filters,
            "available_departments": report.available_departments if report else (),
            "resolution": resolution,
            "range_label": _range_label(resolution),
            "report": report,
            "error": error,
        },
    )


@router.get("/staffing/hours", response_class=HTMLResponse)
def staffing_hours(
    request: Request,
    source: str = Query("clocked"),
    range: str = Query("this_week"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    q: str = Query(""),
    department: str = Query(""),
    attention: str = Query("all"),
):
    resolution = hours.resolve_hours_range(
        range, start, end, plant_today(), odoo_client.fetch_payroll_batches
    )
    if resolution.error:
        return _render_hours(
            request,
            source=source,
            selected_range=range,
            start=start,
            end=end,
            query=q,
            department=department,
            attention=attention,
            resolution=resolution,
            report=None,
            error=resolution.error,
        )
    try:
        report = _load_complete_report(source, resolution, q, department, attention)
    except Exception:  # never turn unavailable source data into partial rows
        log.exception("staffing hours report failed")
        return _render_hours(
            request,
            source=source,
            selected_range=range,
            start=start,
            end=end,
            query=q,
            department=department,
            attention=attention,
            resolution=resolution,
            report=None,
            error="Hours could not be refreshed. Try again soon.",
        )
    return _render_hours(
        request,
        source=source,
        selected_range=range,
        start=start,
        end=end,
        query=q,
        department=department,
        attention=attention,
        resolution=resolution,
        report=report,
        error=None,
    )
