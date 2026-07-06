"""Time-off approvals page.

The route renders from local mirrors only: pending leave requests, cached
balances, department-scoped coverage, and recent in-app decisions.
"""

from __future__ import annotations

from datetime import date, datetime, UTC
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import time_off_audit, time_off_context
from ..deps import templates
from ..plant_day import SITE_TZ, today as plant_today

router = APIRouter()


def _pending_rows(today: date) -> list[dict[str, Any]]:
    """Every pending request in the mirror, ordered by start date."""
    from .. import db

    return db.query(
        "SELECT r.id, r.person_odoo_id, r.holiday_status_id, r.shape, "
        "r.date_from, r.date_to, r.hour_from, r.hour_to, r.state, "
        "COALESCE(p.name, '#' || r.person_odoo_id::text) AS person_name, "
        "COALESCE(lt.name, 'Time off') AS leave_type "
        "FROM time_off_requests r "
        "LEFT JOIN people p ON p.odoo_id = r.person_odoo_id "
        "LEFT JOIN leave_types_cache lt ON lt.holiday_status_id = r.holiday_status_id "
        "WHERE r.state IN ('draft','draft_edit','confirm','validate1') "
        "ORDER BY r.date_from, lower(COALESCE(p.name, '#' || r.person_odoo_id::text))",
        (),
    )


def _hour_label(value: Any) -> str:
    total_minutes = int(round(float(value) * 60))
    hour = (total_minutes // 60) % 24
    minute = total_minutes % 60
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d} {suffix}"


def _date_label(row: dict[str, Any]) -> str:
    start = row["date_from"]
    end = row["date_to"]
    label = f"{start} to {end}" if start != end else str(start)
    if row.get("hour_from") is not None and row.get("hour_to") is not None:
        label += f" - {_hour_label(row['hour_from'])} to {_hour_label(row['hour_to'])}"
    return label


def _decision_time_label(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local = value.astimezone(SITE_TZ)
    return local.strftime("%-m/%-d %-I:%M %p")


def _state_label(state: str | None) -> str:
    if state == "validate1":
        return "Awaiting 2nd approval"
    return "To approve"


def _pending_payload(today: date) -> list[dict[str, Any]]:
    """Attach balance, coverage, and risk flags to each pending row."""
    rows = []
    for row in _pending_rows(today):
        balance = time_off_context.balance_for(
            row["person_odoo_id"], row["holiday_status_id"]
        )
        coverage = time_off_context.coverage_for(
            row["person_odoo_id"], row["date_from"], row["date_to"]
        )
        amount, unit = time_off_context.request_amount(row)
        over_balance = bool(
            balance
            and balance.get("unit") == unit
            and amount > float(balance.get("remaining") or 0)
        )
        rows.append({
            **row,
            "balance": balance,
            "coverage": coverage,
            "date_label": _date_label(row),
            "request_amount": amount,
            "request_unit": unit,
            "over_balance": over_balance,
            "past_due": row["date_to"] < today,
            "awaiting_second": row["state"] == "validate1",
            "state_label": _state_label(row.get("state")),
        })
    return rows


def _recent_payload(days: int = 30) -> list[dict[str, Any]]:
    return [
        {
            **row,
            "date_label": _date_label(row),
            "decided_label": _decision_time_label(row.get("decided_at")),
        }
        for row in time_off_audit.recent_decisions(days=days)
    ]


@router.get("/staffing/time-off/approvals", response_class=HTMLResponse)
def time_off_approvals(request: Request):
    today = plant_today()
    return templates.TemplateResponse(
        request,
        "time_off_approvals.html",
        {
            "active": "time_off_approvals",
            "today_iso": today.isoformat(),
            "pending": _pending_payload(today),
            "recent": _recent_payload(days=30),
        },
    )
