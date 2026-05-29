"""Timeclock — Phase 0 (Dale-only pilot).

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

Sync model: every punch writes a row to `kiosk_punches_log` first, then
the success page is rendered immediately, then a FastAPI BackgroundTask
fires the Odoo XML-RPC write off the request path. The user never waits
on Odoo. On failure the row stays at synced_to_odoo=FALSE; the 60s sweep
worker (in app.py) retries unsynced rows as a safety net.

State reads on the dashboard come from `kiosk_punches_log` too, not
Odoo — `_current_state()` is a ~5ms local SELECT vs a ~200-500ms XML-RPC
call. Local DB is safe as the source of truth so long as no one is
punching via both the kiosk and StratusTime at the same time (revisit
during plant cutover if there's a transition period).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, kiosk_sync, shift_config, staffing
from ..deps import templates

router = APIRouter()
_log = logging.getLogger(__name__)


# ---------- session tokens ----------

_SESSION_SECRET = os.environ.get("KIOSK_SESSION_SECRET") or secrets.token_hex(32)
_TOKEN_TTL_SECONDS = 60


def _time_off_enabled() -> bool:
    """Whether the kiosk Time Off feature is exposed in the UI.

    Gated by env var so the schema + sync engine can ship dark while
    the user-facing tile stays hidden until we're ready to flip it on.
    """
    return os.environ.get("KIOSK_TIME_OFF_ENABLED", "").strip() == "1"


def _is_time_off_only(p: dict | None) -> bool:
    """True for fixed-wage (salaried) staff, who use the kiosk only to
    request time off — they never clock in/out. Odoo's hr.employee
    wage_type 'monthly' is "Fixed Wage" (vs 'hourly'); anyone not
    explicitly 'monthly' (hourly, or wage type unset) keeps the normal
    punch flow, so a mis-tagged hourly worker is never locked out of
    clocking in.

    Gated on the Time Off feature being live — with it dark there's no
    screen to divert them to, so they fall back to the punch dashboard.
    """
    return _time_off_enabled() and bool(p) and p.get("wage_type") == "monthly"


def _time_off_redirect_if_salaried(
    p: dict | None, person_id: int
) -> RedirectResponse | None:
    """Redirect salaried staff into the time-off flow (with a fresh
    token), or None for hourly staff so the caller proceeds normally.
    Applied on every punch screen/action so fixed-wage employees can't
    reach clock-in/out by any path (start, dashboard, Back link, or a
    stale form POST)."""
    if _is_time_off_only(p):
        return RedirectResponse(
            url=f"/kiosk/time-off/{_mint_token(person_id)}", status_code=303
        )
    return None


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
        "SELECT id, name, odoo_id, wage_type, spanish_speaker FROM people "
        "WHERE id = %s AND active = TRUE",
        (person_id,),
    )
    return rows[0] if rows else None


def _current_state(person_odoo_id: int) -> dict:
    """Return the kiosk's local view of an employee's current attendance
    state. Sourced from kiosk_punches_log — no Odoo round trip on the
    read path (was ~200-500ms XML-RPC, now ~5ms local SELECT).

    The most recent punch row determines the state. If the last action
    was clock_in or transfer_in, they're clocked in at that wc_name; if
    it was clock_out / transfer_out / no rows, they're clocked out. The
    Odoo attendance id, when known, lets the background writer close the
    right row on clock_out / transfer.

    Local DB as source of truth is safe for the Phase 0 pilot (Dale only,
    all his punches go through this kiosk) and Phase 1 (plant cutover,
    StratusTime is gone). It is NOT safe during a mixed transition where
    employees punch via both systems — revisit before mixing them.
    """
    rows = db.query(
        "SELECT action, wc_name, "
        "COALESCE(rounded_at, occurred_at) AS occurred_at, "
        "odoo_attendance_id "
        "FROM kiosk_punches_log WHERE person_odoo_id = %s "
        "ORDER BY occurred_at DESC, id DESC LIMIT 1",
        (person_odoo_id,),
    )
    if not rows or rows[0]["action"] in ("clock_out", "transfer_out"):
        return {
            "is_clocked_in": False,
            "current_wc": None,
            "check_in_ts": None,
            "open_odoo_attendance_id": None,
        }
    r = rows[0]
    return {
        "is_clocked_in": True,
        "current_wc": r["wc_name"],
        "check_in_ts": r["occurred_at"],
        "open_odoo_attendance_id": r["odoo_attendance_id"],
    }


def _fmt_short_dt(dt: datetime) -> str:
    """Format as 'M/D h:MM AM/PM' (no leading zeros). Windows needs
    `%#` where POSIX uses `%-`."""
    fmt = "%#m/%#d %#I:%M %p" if os.name == "nt" else "%-m/%-d %-I:%M %p"
    return dt.astimezone(shift_config.SITE_TZ).strftime(fmt)


def _sync_error_warning(person_odoo_id: int) -> dict | None:
    """Return a warning summary if this person has punches that tried to
    sync to Odoo and failed (synced_to_odoo=FALSE AND sync_error IS NOT
    NULL). Returns None if everything synced cleanly.

    "Pending sync" (synced=FALSE, error=NULL) is intentionally excluded —
    those resolve within a second of the next request via the immediate
    background task, so warning about them would be noise. Only persistent
    failures surface here."""
    rows = db.query(
        "SELECT COUNT(*) AS n, MAX(sync_error) AS latest_error, "
        "MAX(occurred_at) AS latest_at "
        "FROM kiosk_punches_log "
        "WHERE person_odoo_id = %s "
        "AND synced_to_odoo = FALSE "
        "AND sync_error IS NOT NULL",
        (person_odoo_id,),
    )
    if not rows or not rows[0]["n"]:
        return None
    return {
        "count": rows[0]["n"],
        "latest_error": rows[0]["latest_error"],
        "latest_at_display": _fmt_short_dt(rows[0]["latest_at"]),
    }


def _pending_time_off_count(person_odoo_id: int) -> int:
    """Count of this person's time-off requests still awaiting approval.

    "Pending" means the request is somewhere in the approval workflow but
    not yet validated or refused/cancelled — states ``draft``, ``confirm``,
    and ``validate1``. The badge on the kiosk Time Off tile uses this so
    employees see at a glance how many of their requests are still in
    flight without having to drill into the My Requests list.
    """
    rows = db.query(
        "SELECT COUNT(*) AS n FROM time_off_requests "
        "WHERE person_odoo_id = %s "
        "AND state IN ('draft', 'confirm', 'validate1')",
        (person_odoo_id,),
    )
    return rows[0]["n"] if rows else 0


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
    return dt.astimezone(shift_config.SITE_TZ).strftime(fmt)


def _open_log_row(
    person_odoo_id: int, action: str, wc_name: str | None
) -> tuple[int, datetime]:
    """Insert a kiosk_punches_log row (synced=FALSE), compute the rounded
    timestamp using current rounding settings, write it back to the row,
    and return (id, rounded_at). Both occurred_at (raw) and rounded_at
    are persisted; everything downstream reads COALESCE(rounded_at,
    occurred_at).

    If rounding fails for any reason (config corruption, unexpected
    timezone edge case, etc.), the INSERT is preserved — rounded_at
    stays NULL and downstream falls back to occurred_at via COALESCE.
    Better to record the raw punch than lose it entirely.
    """
    from .. import rounding, rounding_store
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO kiosk_punches_log "
            "(person_odoo_id, action, wc_name) VALUES (%s, %s, %s) "
            "RETURNING id, occurred_at",
            (person_odoo_id, action, wc_name),
        )
        row = cur.fetchone()
    log_id = row["id"]
    occurred_at = row["occurred_at"]

    try:
        local_date = occurred_at.astimezone(shift_config.SITE_TZ).date()
        rounded = rounding.apply_rounding(
            action,
            occurred_at,
            shift_config.shift_start_for(local_date),
            shift_config.shift_end_for(local_date),
            rounding_store.current(),
        )
        db.execute(
            "UPDATE kiosk_punches_log SET rounded_at = %s WHERE id = %s",
            (rounded, log_id),
        )
        return log_id, rounded
    except Exception:
        _log.exception(
            "Rounding failed for kiosk_punches_log id=%s; leaving rounded_at NULL",
            log_id,
        )
        return log_id, occurred_at


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
    salaried = _time_off_redirect_if_salaried(p, person_id)
    if salaried:
        return salaried
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
    # Fixed-wage staff have no punch screen — bounce to the time-off flow.
    # Covers the time-off landing's "Back" link, which points here.
    salaried = _time_off_redirect_if_salaried(p, person_id)
    if salaried:
        return salaried

    # Local-DB read — no Odoo XML-RPC on the hot path. See _current_state.
    state = _current_state(p["odoo_id"]) if p.get("odoo_id") else _current_state(-1)
    sync_warning = _sync_error_warning(p["odoo_id"]) if p.get("odoo_id") else None
    scheduled_wc = _scheduled_wc_for(p["name"])

    # Refresh the token so a slow user (reading the scheduled WC, picking
    # WCs) doesn't time out mid-action.
    fresh_token = _mint_token(person_id)

    # Time Off tile is flag-gated. Only run the count query when the flag
    # is on AND we have an Odoo person id to count against — saves a query
    # for the (currently common) flag-off case.
    time_off_on = _time_off_enabled()
    pending_time_off = (
        _pending_time_off_count(p["odoo_id"])
        if time_off_on and p.get("odoo_id")
        else 0
    )

    return templates.TemplateResponse(
        request,
        "kiosk_dashboard.html",
        {
            "person": p,
            "token": fresh_token,
            "is_clocked_in": state["is_clocked_in"],
            "current_wc": state["current_wc"],
            "check_in_display": _fmt_time(state["check_in_ts"]) if state["check_in_ts"] else None,
            "scheduled_wc": scheduled_wc,
            "sync_warning": sync_warning,
            "time_off_enabled": time_off_on,
            "pending_time_off_count": pending_time_off,
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
    salaried = _time_off_redirect_if_salaried(p, person_id)
    if salaried:
        return salaried
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
    background_tasks: BackgroundTasks,
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
    salaried = _time_off_redirect_if_salaried(p, person_id)
    if salaried:
        return salaried
    odoo_id = p["odoo_id"]
    log_id, rounded_at = _open_log_row(odoo_id, "clock_in", wc_name)
    # Odoo write runs after the response is sent. FastAPI runs sync `def`
    # background tasks in a threadpool, so the XML-RPC call doesn't block
    # the event loop. The 60s sweep worker remains a safety net for
    # transient failures.
    background_tasks.add_task(kiosk_sync.sync_one_by_id, log_id)
    if scheduled_wc_name and scheduled_wc_name != wc_name:
        _log_variance(odoo_id, scheduled_wc_name, wc_name)
    return templates.TemplateResponse(
        request,
        "kiosk_success.html",
        {
            "person": p,
            "message": f"Clocked in to {wc_name}",
            "time": _fmt_time(rounded_at),
        },
    )


@router.post("/kiosk/clock-out/{token}", response_class=HTMLResponse)
def kiosk_clock_out(
    request: Request,
    background_tasks: BackgroundTasks,
    token: str,
):
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/kiosk", status_code=303)
    p = _person_by_id(person_id)
    if not p or not p.get("odoo_id"):
        return RedirectResponse(url="/kiosk", status_code=303)
    salaried = _time_off_redirect_if_salaried(p, person_id)
    if salaried:
        return salaried
    odoo_id = p["odoo_id"]
    log_id, rounded_at = _open_log_row(odoo_id, "clock_out", None)
    background_tasks.add_task(kiosk_sync.sync_one_by_id, log_id)
    return templates.TemplateResponse(
        request,
        "kiosk_success.html",
        {
            "person": p,
            "message": "Clocked out",
            "time": _fmt_time(rounded_at),
        },
    )


@router.post("/kiosk/transfer/{token}", response_class=HTMLResponse)
def kiosk_transfer(
    request: Request,
    background_tasks: BackgroundTasks,
    token: str,
    new_wc_name: str = Form(...),
):
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/kiosk", status_code=303)
    p = _person_by_id(person_id)
    if not p or not p.get("odoo_id"):
        return RedirectResponse(url="/kiosk", status_code=303)
    salaried = _time_off_redirect_if_salaried(p, person_id)
    if salaried:
        return salaried
    odoo_id = p["odoo_id"]
    out_log, _ = _open_log_row(odoo_id, "transfer_out", None)
    in_log, in_rounded = _open_log_row(odoo_id, "transfer_in", new_wc_name)
    # FastAPI runs BackgroundTasks in the order they're added, so
    # transfer_out always syncs before transfer_in.
    background_tasks.add_task(kiosk_sync.sync_one_by_id, out_log)
    background_tasks.add_task(kiosk_sync.sync_one_by_id, in_log)
    return templates.TemplateResponse(
        request,
        "kiosk_success.html",
        {
            "person": p,
            "message": f"Transferred to {new_wc_name}",
            "time": _fmt_time(in_rounded),
        },
    )
