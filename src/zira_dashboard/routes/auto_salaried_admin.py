"""Auto-salaried punch 'needs a human' list: days the robot wouldn't touch
(late-leave conflicts, incomplete days, unreadable departments). Read the
reasons, fix the day in Odoo if needed, hit Resolve."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..deps import templates

router = APIRouter()


@router.get("/auto-salaried/flags", response_class=HTMLResponse)
def auto_salaried_flags(request: Request):
    from .. import db

    rows = db.query(
        "SELECT f.id, f.person_odoo_id, f.day, f.reason, f.details, f.created_at, "
        "COALESCE(p.name, '#' || f.person_odoo_id::text) AS person_name "
        "FROM auto_salaried_flags f "
        "LEFT JOIN people p ON p.odoo_id = f.person_odoo_id "
        "WHERE f.resolved_at IS NULL "
        "ORDER BY f.day DESC, person_name",
        (),
    )
    return templates.TemplateResponse(
        request, "auto_salaried_flags.html", {"flags": rows})


@router.post("/auto-salaried/flags/{flag_id}/resolve")
def resolve_flag(flag_id: int):
    from .. import db

    db.execute(
        "UPDATE auto_salaried_flags SET resolved_at = now() "
        "WHERE id = %s AND resolved_at IS NULL", (flag_id,))
    return RedirectResponse("/auto-salaried/flags", status_code=303)
