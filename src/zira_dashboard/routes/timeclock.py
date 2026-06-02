"""Timeclock — Phase 0 (Dale-only pilot).

Replaces StratusTime for clock-in/out and adds mid-shift work-center
transfers. Punches write to Odoo `hr.attendance` (sole system of record
for time-clock) and to a local `timeclock_punches_log` for offline tolerance
and audit. The kiosk is designed for touch devices in fullscreen browser
mode; the templates use big-touch / no-scroll layout.

Flow:
  1. GET  /timeclock                       — searchable / scrollable name list
  2. GET  /timeclock/start/{person_id}     — mint a token, redirect to dashboard
  3. GET  /timeclock/dashboard/{token}     — clocked-in state + actions
  4. GET  /timeclock/pick-wc/{token}       — WC picker (for override / transfer)
  5. POST /timeclock/clock-in/{token}      — open hr.attendance with WC
  6. POST /timeclock/clock-out/{token}     — close hr.attendance
  7. POST /timeclock/transfer/{token}      — close + reopen at new WC

Auth: name-pick only — no PIN, by design. Dale's call: PINs add friction
without meaningfully reducing the trust assumption (anyone on the shop
floor who could guess a PIN could also stand at the kiosk and tap a
name). The /timeclock route itself is gated behind the plant-manager session
login (RequireAuthMiddleware), so unauthenticated reach is impossible
from the public internet.

Tokens are HMAC-signed (person_id + issued-at, 60s TTL). Secret comes
from KIOSK_SESSION_SECRET; a fresh random one is generated each process
boot if the env var is unset (all tokens then invalidate on restart,
which is fine for a pilot).

Sync model: every punch writes a row to `timeclock_punches_log` first, then
the success page is rendered immediately, then a FastAPI BackgroundTask
fires the Odoo XML-RPC write off the request path. The user never waits
on Odoo. On failure the row stays at synced_to_odoo=FALSE; the 60s sweep
worker (in app.py) retries unsynced rows as a safety net.

State reads on the dashboard come from `timeclock_punches_log` too, not
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

from .. import db, timeclock_sync, shift_config, staffing, live_cache, attendance_state
from ..deps import templates

router = APIRouter()
_log = logging.getLogger(__name__)


# ---------- back-compat: /kiosk → /timeclock ----------
# The timeclock app moved from /kiosk to /timeclock. These redirects keep
# old bookmarks working — most importantly the plant tablet pinned to
# /kiosk — and catch any internal link that still points at the old path.
@router.get("/kiosk")
def _legacy_kiosk_root(request: Request):
    q = request.url.query
    return RedirectResponse(
        url="/timeclock" + (f"?{q}" if q else ""), status_code=307
    )


@router.get("/kiosk/{rest:path}")
def _legacy_kiosk_deep(request: Request, rest: str):
    q = request.url.query
    return RedirectResponse(
        url=f"/timeclock/{rest}" + (f"?{q}" if q else ""), status_code=307
    )


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
            url=f"/timeclock/time-off/{_mint_token(person_id)}", status_code=303
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


# Reconciliation moved to attendance_state.py (shared with the auto-lunch
# worker). Aliased here so existing call sites are unchanged.
_latest_punch = attendance_state.latest_punch
_state_from_log = attendance_state.state_from_log
_trust_local = attendance_state.trust_local
_current_state = attendance_state.current_state


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
        "FROM timeclock_punches_log "
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


def _shift_for_punch(person_odoo_id: int, local_date):
    """Resolve (shift_start, shift_end, RoundingSettings) for a punch.

    An employee on an Odoo work schedule that has a configured override
    (with hours for this weekday) gets that schedule's boundaries + windows;
    everyone else — and any misconfiguration (no calendar, no override, or
    an override missing this weekday's hours) — falls back to the plant
    default. We never guess a boundary."""
    from .. import rounding_store, work_schedule_store
    rows = db.query(
        "SELECT resource_calendar_id FROM people WHERE odoo_id = %s",
        (person_odoo_id,),
    )
    cal_id = rows[0]["resource_calendar_id"] if rows else None
    if cal_id is not None:
        ws = work_schedule_store.get(cal_id)
        if ws is not None:
            hours = ws.work_hours.get(local_date.weekday())
            if hours is not None:
                return hours[0], hours[1], ws.rounding
            _log.warning(
                "Work schedule override %s has no hours for weekday %s "
                "(person %s); using plant default rounding",
                cal_id, local_date.weekday(), person_odoo_id,
            )
    return (
        shift_config.shift_start_for(local_date),
        shift_config.shift_end_for(local_date),
        rounding_store.current(),
    )


def _open_log_row(
    person_odoo_id: int, action: str, wc_name: str | None
) -> tuple[int, datetime]:
    """Insert a timeclock_punches_log row (synced=FALSE), compute the rounded
    timestamp using current rounding settings, write it back to the row,
    and return (id, rounded_at). Both occurred_at (raw) and rounded_at
    are persisted; everything downstream reads COALESCE(rounded_at,
    occurred_at).

    If rounding fails for any reason (config corruption, unexpected
    timezone edge case, etc.), the INSERT is preserved — rounded_at
    stays NULL and downstream falls back to occurred_at via COALESCE.
    Better to record the raw punch than lose it entirely.
    """
    from .. import rounding
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO timeclock_punches_log "
            "(person_odoo_id, action, wc_name) VALUES (%s, %s, %s) "
            "RETURNING id, occurred_at",
            (person_odoo_id, action, wc_name),
        )
        row = cur.fetchone()
    log_id = row["id"]
    occurred_at = row["occurred_at"]

    try:
        local_date = occurred_at.astimezone(shift_config.SITE_TZ).date()
        shift_start, shift_end, windows = _shift_for_punch(person_odoo_id, local_date)
        rounded = rounding.apply_rounding(
            action, occurred_at, shift_start, shift_end, windows,
        )
        db.execute(
            "UPDATE timeclock_punches_log SET rounded_at = %s WHERE id = %s",
            (rounded, log_id),
        )
        return log_id, rounded
    except Exception:
        _log.exception(
            "Rounding failed for timeclock_punches_log id=%s; leaving rounded_at NULL",
            log_id,
        )
        return log_id, occurred_at


def _log_variance(person_odoo_id: int, scheduled: str | None, actual: str) -> None:
    db.execute(
        "INSERT INTO timeclock_schedule_variances "
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

@router.get("/timeclock", response_class=HTMLResponse)
def timeclock_home(request: Request):
    """Searchable employee list. JS filters as the user types; tapping a
    name navigates to the PIN screen."""
    rows = db.query(
        "SELECT id, name FROM people "
        "WHERE active = TRUE AND NOT excluded "
        "ORDER BY lower(name)"
    )
    return templates.TemplateResponse(
        request, "timeclock_home.html", {"people": rows}
    )


@router.get("/timeclock/start/{person_id}")
def kiosk_start(person_id: int):
    """Mint a fresh session token for `person_id` and bounce to the
    dashboard. No PIN check — picking your name from the home list is
    the auth (intentional design, not a Phase-0 shortcut)."""
    p = _person_by_id(person_id)
    if not p:
        return RedirectResponse(url="/timeclock", status_code=303)
    salaried = _time_off_redirect_if_salaried(p, person_id)
    if salaried:
        return salaried
    token = _mint_token(person_id)
    return RedirectResponse(
        url=f"/timeclock/dashboard/{token}", status_code=303
    )


@router.get("/timeclock/dashboard/{token}", response_class=HTMLResponse)
def timeclock_dashboard(request: Request, token: str):
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/timeclock", status_code=303)
    p = _person_by_id(person_id)
    if not p:
        return RedirectResponse(url="/timeclock", status_code=303)
    # Fixed-wage staff have no punch screen — bounce to the time-off flow.
    # Covers the time-off landing's "Back" link, which points here.
    salaried = _time_off_redirect_if_salaried(p, person_id)
    if salaried:
        return salaried

    # Local-DB read — no Odoo XML-RPC on the hot path. See _current_state.
    state = _current_state(p["odoo_id"]) if p.get("odoo_id") else _current_state(-1)

    # Auto-lunch overlay: during the lunch gap the employee is still "on shift"
    # from their point of view (the auto sign-out is invisible payroll), so keep
    # showing the sign-out action. A sign-out during the gap ends their day
    # (handled in kiosk_clock_out).
    on_lunch = False
    if p.get("odoo_id"):
        from .. import auto_lunch
        now_local = datetime.now(timezone.utc).astimezone(shift_config.SITE_TZ)
        lunch_run = auto_lunch.active_lunch_run(p["odoo_id"], now_local)
        if lunch_run is not None:
            state = {**state, "is_clocked_in": True,
                     "current_wc": lunch_run.get("wc_name")}
            on_lunch = True

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
        "timeclock_dashboard.html",
        {
            "person": p,
            "token": fresh_token,
            "is_clocked_in": state["is_clocked_in"],
            "current_wc": state["current_wc"],
            "on_lunch": on_lunch,
            "check_in_display": _fmt_time(state["check_in_ts"]) if state["check_in_ts"] else None,
            "scheduled_wc": scheduled_wc,
            "sync_warning": sync_warning,
            "time_off_enabled": time_off_on,
            "pending_time_off_count": pending_time_off,
            "bilingual": bool(p.get("spanish_speaker")),
        },
    )


@router.get("/timeclock/pick-wc/{token}", response_class=HTMLResponse)
def timeclock_pick_wc(
    request: Request,
    token: str,
    purpose: str = Query(default="transfer"),
    scheduled: str = Query(default=""),
):
    """Grid of work centers to pick from. `purpose` controls what POST
    URL the form submits to (clock-in vs transfer)."""
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/timeclock", status_code=303)
    p = _person_by_id(person_id)
    if not p:
        return RedirectResponse(url="/timeclock", status_code=303)
    salaried = _time_off_redirect_if_salaried(p, person_id)
    if salaried:
        return salaried
    if purpose not in {"clock_in", "transfer"}:
        purpose = "transfer"
    fresh_token = _mint_token(person_id)
    return templates.TemplateResponse(
        request,
        "timeclock_pick_wc.html",
        {
            "person": p,
            "token": fresh_token,
            "purpose": purpose,
            "scheduled": scheduled,
            "work_centers": _wc_list(),
            "bilingual": bool(p.get("spanish_speaker")),
        },
    )


@router.post("/timeclock/clock-in/{token}", response_class=HTMLResponse)
def kiosk_clock_in(
    request: Request,
    background_tasks: BackgroundTasks,
    token: str,
    wc_name: str = Form(...),
    scheduled_wc_name: str = Form(default=""),
):
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/timeclock", status_code=303)
    p = _person_by_id(person_id)
    if not p or not p.get("odoo_id"):
        return RedirectResponse(url="/timeclock", status_code=303)
    salaried = _time_off_redirect_if_salaried(p, person_id)
    if salaried:
        return salaried
    odoo_id = p["odoo_id"]
    log_id, rounded_at = _open_log_row(odoo_id, "clock_in", wc_name)
    # Odoo write runs after the response is sent. FastAPI runs sync `def`
    # background tasks in a threadpool, so the XML-RPC call doesn't block
    # the event loop. The 60s sweep worker remains a safety net for
    # transient failures.
    background_tasks.add_task(timeclock_sync.sync_one_by_id, log_id)
    if scheduled_wc_name and scheduled_wc_name != wc_name:
        _log_variance(odoo_id, scheduled_wc_name, wc_name)
    return templates.TemplateResponse(
        request,
        "timeclock_success.html",
        {
            "person": p,
            "message": f"Clocked in to {wc_name}",
            "time": _fmt_time(rounded_at),
            "bilingual": bool(p.get("spanish_speaker")),
        },
    )


@router.post("/timeclock/clock-out/{token}", response_class=HTMLResponse)
def kiosk_clock_out(
    request: Request,
    background_tasks: BackgroundTasks,
    token: str,
):
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/timeclock", status_code=303)
    p = _person_by_id(person_id)
    if not p or not p.get("odoo_id"):
        return RedirectResponse(url="/timeclock", status_code=303)
    salaried = _time_off_redirect_if_salaried(p, person_id)
    if salaried:
        return salaried
    odoo_id = p["odoo_id"]
    log_id, rounded_at = _open_log_row(odoo_id, "clock_out", None)
    background_tasks.add_task(timeclock_sync.sync_one_by_id, log_id)
    return templates.TemplateResponse(
        request,
        "timeclock_success.html",
        {
            "person": p,
            "message": "Clocked out",
            "time": _fmt_time(rounded_at),
            "bilingual": bool(p.get("spanish_speaker")),
        },
    )


@router.post("/timeclock/transfer/{token}", response_class=HTMLResponse)
def kiosk_transfer(
    request: Request,
    background_tasks: BackgroundTasks,
    token: str,
    new_wc_name: str = Form(...),
):
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/timeclock", status_code=303)
    p = _person_by_id(person_id)
    if not p or not p.get("odoo_id"):
        return RedirectResponse(url="/timeclock", status_code=303)
    salaried = _time_off_redirect_if_salaried(p, person_id)
    if salaried:
        return salaried
    odoo_id = p["odoo_id"]
    out_log, _ = _open_log_row(odoo_id, "transfer_out", None)
    in_log, in_rounded = _open_log_row(odoo_id, "transfer_in", new_wc_name)
    # FastAPI runs BackgroundTasks in the order they're added, so
    # transfer_out always syncs before transfer_in.
    background_tasks.add_task(timeclock_sync.sync_one_by_id, out_log)
    background_tasks.add_task(timeclock_sync.sync_one_by_id, in_log)
    return templates.TemplateResponse(
        request,
        "timeclock_success.html",
        {
            "person": p,
            "message": f"Transferred to {new_wc_name}",
            "time": _fmt_time(in_rounded),
            "bilingual": bool(p.get("spanish_speaker")),
        },
    )
