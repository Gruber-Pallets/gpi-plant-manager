"""Signed-token employee pages for PTO linked to a recorded past absence."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import absence_pto, absence_pto_store, timeclock_i18n
from ..deps import templates
from ..plant_day import today as plant_today
from .timeclock import _expired_redirect, _mint_token, _person_by_id, _verify_token


router = APIRouter()

_STATE_LABELS = {
    "pending": "Pending",
    "converting": "Processing",
    "approved": "Approved",
    "denied": "Denied",
    "needs_review": "Needs review",
    "resolved_manually": "Handled",
}


def _list_url(person_id: int) -> str:
    return f"/timeclock/time-off/past-absence/{_mint_token(person_id)}"


def _render_list(
    request: Request,
    person: dict,
    person_id: int,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    odoo_id = int(person["odoo_id"])
    context = {
        "person": person,
        "token": _mint_token(person_id),
        "candidates": absence_pto.list_candidates(odoo_id, plant_today()),
        "requests": absence_pto.employee_requests(odoo_id),
        "state_labels": _STATE_LABELS,
        "error": error,
        **timeclock_i18n.context_for_person(person),
    }
    return templates.TemplateResponse(
        request,
        "timeclock_absence_pto_list.html",
        context,
        status_code=status_code,
    )


@router.get("/timeclock/time-off/past-absence/{token}", response_class=HTMLResponse)
def past_absence_list(request: Request, token: str):
    person_id = _verify_token(token)
    if person_id is None:
        return _expired_redirect(request)
    person = _person_by_id(person_id)
    if not person or not person.get("odoo_id"):
        return RedirectResponse("/timeclock", status_code=303)
    return _render_list(request, person, person_id)


@router.post(
    "/timeclock/time-off/past-absence/{token}/{day}",
    response_class=HTMLResponse,
)
def submit_past_absence(
    request: Request,
    token: str,
    day: date,
    note: str = Form(""),
):
    person_id = _verify_token(token)
    if person_id is None:
        return _expired_redirect(request)
    person = _person_by_id(person_id)
    if not person or not person.get("odoo_id"):
        return RedirectResponse("/timeclock", status_code=303)
    try:
        linked = absence_pto.submit(
            person_id,
            int(person["odoo_id"]),
            person["name"],
            day,
            note.strip(),
            plant_today(),
        )
    except absence_pto.SubmissionError as error:
        status_code = (
            409 if isinstance(error, absence_pto.DuplicateSubmissionError) else 422
        )
        return _render_list(
            request,
            person,
            person_id,
            error=str(error),
            status_code=status_code,
        )
    return RedirectResponse(
        f"/timeclock/time-off/past-absence/{_mint_token(person_id)}"
        f"/requests/{linked.id}",
        status_code=303,
    )


@router.get(
    "/timeclock/time-off/past-absence/{token}/requests/{request_id}",
    response_class=HTMLResponse,
)
def past_absence_detail(request: Request, token: str, request_id: int):
    person_id = _verify_token(token)
    if person_id is None:
        return _expired_redirect(request)
    person = _person_by_id(person_id)
    if not person or not person.get("odoo_id"):
        return RedirectResponse("/timeclock", status_code=303)
    linked = absence_pto_store.get_request(request_id)
    if linked is None or linked.person_odoo_id != int(person["odoo_id"]):
        return RedirectResponse(_list_url(person_id), status_code=303)
    return templates.TemplateResponse(
        request,
        "timeclock_absence_pto_detail.html",
        {
            "person": person,
            "token": _mint_token(person_id),
            "request_row": linked,
            "state_label": _STATE_LABELS.get(linked.state, linked.state),
            **timeclock_i18n.context_for_person(person),
        },
    )
