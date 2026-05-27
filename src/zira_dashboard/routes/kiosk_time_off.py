"""Kiosk time-off routes — gated by the same HMAC token as `routes/kiosk.py`.

Surfaces a touch-friendly time-off flow on the kiosk: landing with three
big-touch actions (Request Time Off / My Requests / Who's Out), the
request wizard, the mine list/detail, and the calendar. This module
currently only owns the landing route; the request wizard, mine, and
calendar pages get appended by subsequent tasks in the plan.

Auth is identical to `routes/kiosk.py`: every URL takes a 60s HMAC token
in the path, and an invalid/expired token bounces back to `/kiosk` so a
shared device never leaks one user's data to the next. The helpers
``_mint_token`` / ``_verify_token`` / ``_person_by_id`` live in
`routes/kiosk.py` and are reused here verbatim — duplicating them would
risk drift in the auth boundary.

The landing route also surfaces a warning banner if any of this person's
recent submissions are stuck in the sync queue (synced_to_odoo=FALSE AND
sync_error IS NOT NULL), mirroring the same UX pattern used on the kiosk
dashboard for stuck punches — so an employee whose request hasn't made
it to Odoo isn't left wondering why HR hasn't seen it.

Routes:
  GET /kiosk/time-off/{token}              Landing with 3 buttons
  GET /kiosk/time-off/request/{token}      Wizard step 1 — shape picker
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db
from ..deps import templates
from .kiosk import _mint_token, _person_by_id, _verify_token

router = APIRouter()


def _pending_count(person_odoo_id: int) -> int:
    """Count of this person's requests still in-flight (not yet validated
    or refused/cancelled). Matches `_pending_time_off_count` in
    `routes/kiosk.py` — kept local because the badge math may diverge
    once the wizard exists (e.g. distinguishing "draft I haven't
    submitted" from "pending HR approval")."""
    rows = db.query(
        "SELECT COUNT(*) AS n FROM time_off_requests "
        "WHERE person_odoo_id = %s "
        "AND state IN ('draft', 'confirm', 'validate1')",
        (person_odoo_id,),
    )
    return rows[0]["n"] if rows else 0


def _all_count(person_odoo_id: int) -> int:
    """Total count of this person's requests, used as the badge on the
    My Requests action so the user sees there is history to look at even
    after everything has been approved or refused."""
    rows = db.query(
        "SELECT COUNT(*) AS n FROM time_off_requests "
        "WHERE person_odoo_id = %s",
        (person_odoo_id,),
    )
    return rows[0]["n"] if rows else 0


def _sync_error_warning(person_odoo_id: int) -> dict | None:
    """Return a warning summary if this person has requests that tried
    to sync to Odoo and failed (synced_to_odoo=FALSE AND sync_error IS
    NOT NULL). Returns None if everything synced cleanly.

    Mirrors `_sync_error_warning` in `routes/kiosk.py` — same shape so
    the template renders both with the same `k-warning` styling."""
    rows = db.query(
        "SELECT COUNT(*) AS n, MAX(sync_error) AS latest "
        "FROM time_off_requests WHERE person_odoo_id = %s "
        "AND synced_to_odoo = FALSE AND sync_error IS NOT NULL",
        (person_odoo_id,),
    )
    if not rows or not rows[0]["n"]:
        return None
    return {"count": rows[0]["n"], "latest_error": rows[0]["latest"]}


@router.get("/kiosk/time-off/{token}", response_class=HTMLResponse)
def time_off_landing(request: Request, token: str):
    """Landing page with three big-touch actions: Request Time Off,
    My Requests, Who's Out. Same HMAC gate as the rest of /kiosk — an
    invalid or expired token bounces to /kiosk so a stale URL on a shared
    device never lets the next user act as the previous one.

    Mints a fresh token before render so a user reading the screen (or
    pausing to think) doesn't time out mid-tap. The counts come from the
    local `time_off_requests` mirror so this is a few millisecond
    Postgres SELECTs, no Odoo XML-RPC on the hot path.
    """
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/kiosk", status_code=303)
    p = _person_by_id(person_id)
    if not p:
        return RedirectResponse(url="/kiosk", status_code=303)
    fresh = _mint_token(person_id)
    # If a person has no Odoo id mapped, fall back to a sentinel that
    # matches nothing in time_off_requests rather than returning early —
    # the page still renders with zero counts and a generic landing.
    odoo_id = p.get("odoo_id") or -1
    return templates.TemplateResponse(
        request,
        "kiosk_time_off_landing.html",
        {
            "person": p,
            "token": fresh,
            "pending_count": _pending_count(odoo_id),
            "all_count": _all_count(odoo_id),
            "sync_warning": _sync_error_warning(odoo_id),
        },
    )


@router.get("/kiosk/time-off/request/{token}", response_class=HTMLResponse)
def request_shape(request: Request, token: str):
    """Wizard step 1 — four big-touch cards that each link to step 2
    with a `shape=` query param. Same HMAC gate as the landing; invalid
    token bounces to /kiosk.

    Mints a fresh token before render so the user has the full TTL to
    pick a shape; the next page picks up that token and mints again.
    """
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/kiosk", status_code=303)
    p = _person_by_id(person_id)
    if not p:
        return RedirectResponse(url="/kiosk", status_code=303)
    fresh = _mint_token(person_id)
    return templates.TemplateResponse(
        request,
        "kiosk_time_off_request_shape.html",
        {"person": p, "token": fresh},
    )
