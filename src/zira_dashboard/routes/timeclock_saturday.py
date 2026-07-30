"""Token-gated employee responses to an optional Saturday work opening."""

from __future__ import annotations

import logging
from datetime import date, time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import saturday_recruiting as sr, saturday_recruiting_store as store, timeclock_i18n
from ..deps import templates
from ..plant_day import now as plant_now
from .timeclock import _expired_redirect, _mint_token, _person_by_id, _verify_token

router = APIRouter()
_log = logging.getLogger(__name__)


def _offer_context(offer: store.Offer) -> dict:
    is_holiday = offer.day_kind == "holiday"
    return {
        "day": offer.day.isoformat(),
        "day_label": f"{offer.day.strftime('%A, %B')} {offer.day.day}",
        "day_label_es": timeclock_i18n.spanish_date_label(offer.day),
        "shift_start": offer.shift_start.isoformat(timespec="minutes"),
        "shift_end": offer.shift_end.isoformat(timespec="minutes"),
        "shift_label": sr.format_time_range(offer.shift_start, offer.shift_end),
        "deadline_label": sr.format_deadline(offer.response_deadline),
        "deadline_label_es": timeclock_i18n.spanish_deadline_label(offer.response_deadline),
        "day_kind": offer.day_kind,
        "event_name": offer.event_name or ("Holiday" if is_holiday else "Saturday"),
        "is_holiday": is_holiday,
    }


def _person(request: Request, token: str):
    person_id = _verify_token(token)
    if person_id is None:
        return None, None, _expired_redirect(request)
    person = _person_by_id(person_id)
    if not person or person.get("wage_type") == "monthly":
        return None, None, RedirectResponse(url="/timeclock", status_code=303)
    return person_id, person, None


def _offer(person_id: int):
    try:
        return store.offer_for_person(person_id, plant_now())
    except Exception:
        _log.exception("Saturday offer lookup failed for person %s", person_id)
        return None


def _render(request, template, person, person_id, offer, *, status_code: int = 200, **context):
    return templates.TemplateResponse(
        request,
        template,
        {
            "person": person,
            "token": _mint_token(person_id),
            "offer": _offer_context(offer),
            **context,
            **timeclock_i18n.context_for_person(person),
        },
        status_code=status_code,
    )


def _parse_choice(day_value: str, start_value: str, end_value: str):
    return (
        date.fromisoformat(day_value),
        time.fromisoformat(start_value),
        time.fromisoformat(end_value),
    )


def _same_offer(offer: store.Offer, day: date) -> bool:
    return offer.day == day


def _partial_options(start: time, end: time) -> list[str]:
    values = []
    minute = start.hour * 60 + start.minute
    finish = end.hour * 60 + end.minute
    while minute <= finish:
        values.append(f"{minute // 60:02d}:{minute % 60:02d}")
        minute += 30
    return values


@router.get("/timeclock/saturday/{token}", response_class=HTMLResponse)
def saturday_offer(request: Request, token: str):
    person_id, person, redirect = _person(request, token)
    if redirect:
        return redirect
    offer = _offer(person_id)
    if offer is None:
        return RedirectResponse(
            url=f"/timeclock/dashboard/{_mint_token(person_id)}", status_code=303
        )
    return _render(request, "timeclock_saturday_offer.html", person, person_id, offer)


@router.get("/timeclock/saturday/partial/{token}", response_class=HTMLResponse)
def saturday_partial_get(request: Request, token: str):
    person_id, person, redirect = _person(request, token)
    if redirect:
        return redirect
    offer = _offer(person_id)
    if offer is None:
        return RedirectResponse(
            url=f"/timeclock/dashboard/{_mint_token(person_id)}", status_code=303
        )
    return _render(
        request,
        "timeclock_saturday_partial.html",
        person,
        person_id,
        offer,
        time_options=_partial_options(offer.shift_start, offer.shift_end),
    )


@router.post("/timeclock/saturday/partial/{token}", response_class=HTMLResponse)
def saturday_partial_post(
    request: Request,
    token: str,
    availability_start: str = Form(...),
    availability_end: str = Form(...),
):
    person_id, person, redirect = _person(request, token)
    if redirect:
        return redirect
    offer = _offer(person_id)
    if offer is None:
        return RedirectResponse(
            url=f"/timeclock/dashboard/{_mint_token(person_id)}", status_code=303
        )
    try:
        start, end = time.fromisoformat(availability_start), time.fromisoformat(availability_end)
        sr.validate_availability(start, end, offer.shift_start, offer.shift_end)
    except (ValueError, sr.InvalidAvailability):
        error = (
            "Availability must use 30-minute increments and stay within the optional shift."
            if offer.day_kind == "holiday"
            else "Availability must use 30-minute increments and stay within the Saturday shift."
        )
        return _render(
            request,
            "timeclock_saturday_partial.html",
            person,
            person_id,
            offer,
            time_options=_partial_options(offer.shift_start, offer.shift_end),
            error=error,
            status_code=422,
        )
    return _render(
        request,
        "timeclock_saturday_confirm.html",
        person,
        person_id,
        offer,
        availability_start=start.isoformat(timespec="minutes"),
        availability_end=end.isoformat(timespec="minutes"),
        selected_hours=sr.format_time_range(start, end),
    )


@router.post("/timeclock/saturday/confirm/{token}", response_class=HTMLResponse)
def saturday_confirm(
    request: Request,
    token: str,
    day: str = Form(...),
    availability_start: str = Form(...),
    availability_end: str = Form(...),
):
    person_id, person, redirect = _person(request, token)
    if redirect:
        return redirect
    offer = _offer(person_id)
    if offer is None:
        return RedirectResponse(
            url=f"/timeclock/dashboard/{_mint_token(person_id)}", status_code=303
        )
    try:
        requested_day, start, end = _parse_choice(day, availability_start, availability_end)
        if not _same_offer(offer, requested_day):
            raise ValueError
        sr.validate_availability(start, end, offer.shift_start, offer.shift_end)
    except (ValueError, sr.InvalidAvailability):
        return RedirectResponse(
            url=f"/timeclock/saturday/{_mint_token(person_id)}", status_code=303
        )
    return _render(
        request,
        "timeclock_saturday_confirm.html",
        person,
        person_id,
        offer,
        availability_start=start.isoformat(timespec="minutes"),
        availability_end=end.isoformat(timespec="minutes"),
        selected_hours=sr.format_time_range(start, end),
    )


def _decision_redirect(person_id: int):
    return RedirectResponse(url=f"/timeclock/dashboard/{_mint_token(person_id)}", status_code=303)


@router.post("/timeclock/saturday/commit/{token}", response_class=HTMLResponse)
def saturday_commit(
    request: Request,
    token: str,
    day: str = Form(...),
    availability_start: str = Form(...),
    availability_end: str = Form(...),
):
    person_id, person, redirect = _person(request, token)
    if redirect:
        return redirect
    try:
        requested_day, start, end = _parse_choice(day, availability_start, availability_end)
        store.commit(requested_day, person_id, start, end, plant_now())
    except store.NoCompatibleOpening:
        offer = _offer(person_id)
        if offer is None:
            return templates.TemplateResponse(
                request,
                "timeclock_saturday_offer.html",
                {
                    "person": person,
                    "token": _mint_token(person_id),
                    "offer": None,
                    "error": "That opening was just filled. You have not been scheduled.",
                    **timeclock_i18n.context_for_person(person),
                },
                status_code=409,
            )
        return _render(
            request,
            "timeclock_saturday_offer.html",
            person,
            person_id,
            offer,
            error="That opening was just filled. You have not been scheduled.",
            status_code=409,
        )
    except (ValueError, sr.SaturdayRecruitingError):
        return RedirectResponse(
            url=f"/timeclock/saturday/{_mint_token(person_id)}", status_code=303
        )
    except Exception:
        _log.exception("Saturday commit failed for person %s", person_id)
        return _decision_redirect(person_id)
    return _decision_redirect(person_id)


def _simple_decision(request, token, day: str, operation):
    person_id, person, redirect = _person(request, token)
    if redirect:
        return redirect
    try:
        operation(date.fromisoformat(day), person_id, plant_now())
    except (ValueError, sr.SaturdayRecruitingError):
        return _decision_redirect(person_id)
    except Exception:
        _log.exception("Saturday response failed for person %s", person_id)
        return _decision_redirect(person_id)
    return _decision_redirect(person_id)


@router.post("/timeclock/saturday/decline/{token}")
def saturday_decline(request: Request, token: str, day: str = Form(...)):
    return _simple_decision(request, token, day, store.decline)


@router.post("/timeclock/saturday/later/{token}")
def saturday_later(request: Request, token: str, day: str = Form(...)):
    return _simple_decision(request, token, day, store.record_later)


@router.post("/timeclock/saturday/cancel/{token}", response_class=HTMLResponse)
def saturday_cancel(request: Request, token: str, day: str = Form(...)):
    person_id, person, redirect = _person(request, token)
    if redirect:
        return redirect
    try:
        store.cancel_by_employee(date.fromisoformat(day), person_id, plant_now())
    except store.RecruitingClosed:
        commitment = None
        try:
            commitment = store.commitment_for_person(person_id, plant_now())
        except Exception:
            _log.exception("Saturday commitment lookup failed for person %s", person_id)
        return templates.TemplateResponse(
            request,
            "timeclock_saturday_offer.html",
            {
                "person": person,
                "token": _mint_token(person_id),
                "offer": None,
                "commitment": commitment,
                "error": "Contact a manager to make a change.",
                **timeclock_i18n.context_for_person(person),
            },
            status_code=409,
        )
    except (ValueError, sr.SaturdayRecruitingError):
        return _decision_redirect(person_id)
    except Exception:
        _log.exception("Saturday cancellation failed for person %s", person_id)
        return _decision_redirect(person_id)
    return _decision_redirect(person_id)
