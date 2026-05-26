"""Plant kiosk — Phase 0 (Dale-only pilot).

Replaces StratusTime for clock-in/out and adds mid-shift work-center
transfers. Punches write to Odoo `hr.attendance` (sole system of record
for time-clock) and to a local `kiosk_punches_log` for offline tolerance
and audit. The kiosk is designed for touch devices in fullscreen browser
mode; the templates use big-touch / no-scroll layout.

Flow:
  1. GET  /kiosk                       — searchable / scrollable name list
  2. GET  /kiosk/start/{person_id}     — mint a token, redirect to dashboard
  3. GET  /kiosk/dashboard/{token}     — clocked-in state + actions
  4. GET  /kiosk/pick-wc/{token}       — WC picker (for override / transfer)
  5. POST /kiosk/clock-in/{token}      — open hr.attendance with WC
  6. POST /kiosk/clock-out/{token}     — close hr.attendance
  7. POST /kiosk/transfer/{token}      — close + reopen at new WC

Auth: name-pick only — no PIN, by design. Dale's call: PINs add friction
without meaningfully reducing the trust assumption (anyone on the shop
floor who could guess a PIN could also stand at the kiosk and tap a
name). The /kiosk route itself is gated behind the plant-manager session
login (RequireAuthMiddleware), so unauthenticated reach is impossible
from the public internet.

Tokens are HMAC-signed (person_id + issued-at, 60s TTL). Secret comes
from KIOSK_SESSION_SECRET; a fresh random one is generated each process
boot if the env var is unset (all tokens then invalidate on restart,
which is fine for a pilot).

Sync model: every punch action writes a row to `kiosk_punches_log` with
synced_to_odoo=FALSE first, then attempts the Odoo write. On success the
row is flipped to TRUE; on failure the kiosk still shows success and the
background worker (in app.py) retries unsynced rows every 60s. This
makes the kiosk usable even when Odoo is briefly unreachable.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, odoo_client, staffing
from ..deps import templates

router = APIRouter()
_log = logging.getLogger(__name__)


# ---------- session tokens ----------

_SESSION_SECRET = os.environ.get("KIOSK_SESSION_SECRET") or secrets.token_hex(32)
_TOKEN_TTL_SECONDS = 60


def _mint_token(person_id: int) -> str:
    issued = int(time.time())
    payload = f"{person_id}:{issued}"
    sig = hmac.new(
        _SESSION_SECRET.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:16]
    return f"{payload}:{sig}"


def _verify_token(token: str) -> int | None:
    """Return person_id if token is valid + within TTL, else None."""
    try:
        pid_s, issued_s, sig = token.split(":")
        person_id = int(pid_s)
        issued = int(issued_s)
    except (ValueError, AttributeError):
        return None
    expected_payload = f"{person_id}:{issued}"
    expected_sig = hmac.new(
        _SESSION_SECRET.encode(), expected_payload.encode(), hashlib.sha256
    ).hexdigest()[:16]
    if not hmac.compare_digest(sig, expected_sig):
        return None
    if int(time.time()) - issued > _TOKEN_TTL_SECONDS:
        return None
    return person_id


# ---------- helpers ----------

def _person_by_id(person_id: int) -> dict | None:
    rows = db.query(
        "SELECT id, name, odoo_id FROM people WHERE id = %s AND active = TRUE",
        (person_id,),
    )
    return rows[0] if rows else None


def _scheduled_wc_for(person_name: str) -> str | None:
    """Today's scheduled WC for `person_name`, or None if unscheduled.
    Returns the first match if scheduled on multiple."""
    today = datetime.now(timezone.utc).date()
    sched = staffing.load_schedule(today)
    for wc_name, names in (sched.assignments or {}).items():
        if person_name in names:
            return wc_name
    return None


def _fmt_time(dt: datetime) -> str:
    """Format as 'H:MM AM/PM' (no leading zero on hour). The `%-I`
    directive doesn't work on Windows — use `%#I` there."""
    fmt = "%#I:%M %p" if os.name == "nt" else "%-I:%M %p"
    return dt.astimezone().strftime(fmt)


def _open_log_row(person_odoo_id: int, action: str, wc_name: str | None) -> int:
    """Insert a kiosk_punches_log row (synced=FALSE) and return its id."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO kiosk_punches_log "
            "(person_odoo_id, action, wc_name) VALUES (%s, %s, %s) "
            "RETURNING id",
            (person_odoo_id, action, wc_name),
        )
        row = cur.fetchone()
        return row["id"]


def _mark_log_synced(log_id: int, odoo_attendance_id: int | None) -> None:
    db.execute(
        "UPDATE kiosk_punches_log SET synced_to_odoo = TRUE, "
        "odoo_attendance_id = %s, sync_error = NULL, synced_at = now() "
        "WHERE id = %s",
        (odoo_attendance_id, log_id),
    )


def _mark_log_failed(log_id: int, err: str) -> None:
    db.execute(
        "UPDATE kiosk_punches_log SET sync_error = %s WHERE id = %s",
        (err[:500], log_id),
    )


def _log_variance(person_odoo_id: int, scheduled: str | None, actual: str) -> None:
    db.execute(
        "INSERT INTO kiosk_schedule_variances "
        "(person_odoo_id, scheduled_wc_name, actual_wc_name) VALUES (%s, %s, %s)",
        (person_odoo_id, scheduled, actual),
    )


def _wc_list() -> list[dict]:
    """All work centers from the static staffing.LOCATIONS, shaped for
    the kiosk picker template."""
    return [
        {"name": loc.name, "bay": loc.bay, "department": loc.department}
        for loc in staffing.LOCATIONS
    ]


# ---------- routes ----------

@router.get("/kiosk", response_class=HTMLResponse)
def kiosk_home(request: Request):
    """Searchable employee list. JS filters as the user types; tapping a
    name navigates to the PIN screen."""
    rows = db.query(
        "SELECT id, name FROM people "
        "WHERE active = TRUE AND NOT excluded "
        "ORDER BY lower(name)"
    )
    return templates.TemplateResponse(
        request, "kiosk_home.html", {"people": rows}
    )


@router.get("/kiosk/start/{person_id}")
def kiosk_start(person_id: int):
    """Mint a fresh session token for `person_id` and bounce to the
    dashboard. No PIN check — picking your name from the home list is
    the auth (intentional design, not a Phase-0 shortcut)."""
    p = _person_by_id(person_id)
    if not p:
        return RedirectResponse(url="/kiosk", status_code=303)
    token = _mint_token(person_id)
    return RedirectResponse(
        url=f"/kiosk/dashboard/{token}", status_code=303
    )


@router.get("/kiosk/dashboard/{token}", response_class=HTMLResponse)
def kiosk_dashboard(request: Request, token: str):
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/kiosk", status_code=303)
    p = _person_by_id(person_id)
    if not p:
        return RedirectResponse(url="/kiosk", status_code=303)

    # Pull current attendance from Odoo. If Odoo is unreachable, show as
    # not-clocked-in but warn the user; they can still act, we'll queue
    # the punch and the background worker will sync when Odoo comes back.
    current: dict | None = None
    current_wc: str | None = None
    odoo_error: str | None = None
    try:
        if p.get("odoo_id"):
            current = odoo_client.get_current_attendance(p["odoo_id"])
            field = odoo_client._kiosk_wc_field()
            if current and field:
                current_wc = current.get(field)
    except Exception as e:  # noqa: BLE001 — Odoo outage must not block kiosk
        odoo_error = str(e)
        _log.warning(
            "Kiosk dashboard couldn't fetch Odoo attendance for %s: %s",
            p["name"], e,
        )

    scheduled_wc = _scheduled_wc_for(p["name"])

    # Refresh the token so a slow user (reading the scheduled WC, picking
    # WCs) doesn't time out mid-action.
    fresh_token = _mint_token(person_id)

    return templates.TemplateResponse(
        request,
        "kiosk_dashboard.html",
        {
            "person": p,
            "token": fresh_token,
            "is_clocked_in": current is not None,
            "current_attendance": current,
            "current_wc": current_wc,
            "scheduled_wc": scheduled_wc,
            "odoo_error": odoo_error,
        },
    )


@router.get("/kiosk/pick-wc/{token}", response_class=HTMLResponse)
def kiosk_pick_wc(
    request: Request,
    token: str,
    purpose: str = Query(default="transfer"),
    scheduled: str = Query(default=""),
):
    """Grid of work centers to pick from. `purpose` controls what POST
    URL the form submits to (clock-in vs transfer)."""
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/kiosk", status_code=303)
    p = _person_by_id(person_id)
    if not p:
        return RedirectResponse(url="/kiosk", status_code=303)
    if purpose not in {"clock_in", "transfer"}:
        purpose = "transfer"
    fresh_token = _mint_token(person_id)
    return templates.TemplateResponse(
        request,
        "kiosk_pick_wc.html",
        {
            "person": p,
            "token": fresh_token,
            "purpose": purpose,
            "scheduled": scheduled,
            "work_centers": _wc_list(),
        },
    )


@router.post("/kiosk/clock-in/{token}", response_class=HTMLResponse)
def kiosk_clock_in(
    request: Request,
    token: str,
    wc_name: str = Form(...),
    scheduled_wc_name: str = Form(default=""),
):
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/kiosk", status_code=303)
    p = _person_by_id(person_id)
    if not p or not p.get("odoo_id"):
        return RedirectResponse(url="/kiosk", status_code=303)
    odoo_id = p["odoo_id"]
    now = datetime.now(timezone.utc)
    log_id = _open_log_row(odoo_id, "clock_in", wc_name)
    sync_error: str | None = None
    try:
        att_id = odoo_client.clock_in(odoo_id, wc_name, now)
        _mark_log_synced(log_id, att_id)
    except Exception as e:  # noqa: BLE001
        sync_error = str(e)
        _mark_log_failed(log_id, sync_error)
        _log.warning(
            "Kiosk clock-in Odoo sync failed for person %s: %s", odoo_id, e
        )
    # Override variance: scheduled vs actual WC mismatch
    if scheduled_wc_name and scheduled_wc_name != wc_name:
        _log_variance(odoo_id, scheduled_wc_name, wc_name)
    return templates.TemplateResponse(
        request,
        "kiosk_success.html",
        {
            "person": p,
            "message": f"Clocked in to {wc_name}",
            "time": _fmt_time(now),
            "sync_error": sync_error,
        },
    )


@router.post("/kiosk/clock-out/{token}", response_class=HTMLResponse)
def kiosk_clock_out(request: Request, token: str):
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/kiosk", status_code=303)
    p = _person_by_id(person_id)
    if not p or not p.get("odoo_id"):
        return RedirectResponse(url="/kiosk", status_code=303)
    odoo_id = p["odoo_id"]
    now = datetime.now(timezone.utc)
    log_id = _open_log_row(odoo_id, "clock_out", None)
    sync_error: str | None = None
    try:
        current = odoo_client.get_current_attendance(odoo_id)
        if current:
            odoo_client.clock_out(current["id"], now)
            _mark_log_synced(log_id, current["id"])
        else:
            # Nothing to close — flag as synced (no-op).
            _mark_log_synced(log_id, None)
    except Exception as e:  # noqa: BLE001
        sync_error = str(e)
        _mark_log_failed(log_id, sync_error)
        _log.warning(
            "Kiosk clock-out Odoo sync failed for person %s: %s", odoo_id, e
        )
    return templates.TemplateResponse(
        request,
        "kiosk_success.html",
        {
            "person": p,
            "message": "Clocked out",
            "time": _fmt_time(now),
            "sync_error": sync_error,
        },
    )


@router.post("/kiosk/transfer/{token}", response_class=HTMLResponse)
def kiosk_transfer(
    request: Request, token: str, new_wc_name: str = Form(...)
):
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/kiosk", status_code=303)
    p = _person_by_id(person_id)
    if not p or not p.get("odoo_id"):
        return RedirectResponse(url="/kiosk", status_code=303)
    odoo_id = p["odoo_id"]
    now = datetime.now(timezone.utc)
    out_log = _open_log_row(odoo_id, "transfer_out", None)
    in_log = _open_log_row(odoo_id, "transfer_in", new_wc_name)
    sync_error: str | None = None
    try:
        closed_id, new_id = odoo_client.transfer(odoo_id, new_wc_name, now)
        _mark_log_synced(out_log, closed_id)
        _mark_log_synced(in_log, new_id)
    except Exception as e:  # noqa: BLE001
        sync_error = str(e)
        _mark_log_failed(out_log, sync_error)
        _mark_log_failed(in_log, sync_error)
        _log.warning(
            "Kiosk transfer Odoo sync failed for person %s: %s", odoo_id, e
        )
    return templates.TemplateResponse(
        request,
        "kiosk_success.html",
        {
            "person": p,
            "message": f"Transferred to {new_wc_name}",
            "time": _fmt_time(now),
            "sync_error": sync_error,
        },
    )
