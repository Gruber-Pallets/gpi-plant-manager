"""Kiosk time-off routes — gated by the same HMAC token as `routes/kiosk.py`.

Surfaces a touch-friendly time-off flow on the kiosk: landing with three
big-touch actions (Request Time Off / My Requests / Who's Out), the
request wizard, the mine list/detail, and the calendar. This module
currently only owns the landing route; the request wizard, mine, and
calendar pages get appended by subsequent tasks in the plan.

Auth is identical to `routes/kiosk.py`: every URL takes a 60s HMAC token
in the path, and an invalid/expired token bounces back to `/timeclock` so a
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
  GET  /timeclock/time-off/{token}                              Landing — shape picker + My Requests + Who's Out
  GET  /timeclock/time-off/request/{token}                      Legacy — redirects to landing
  GET  /timeclock/time-off/request/{token}/details?shape=…      Wizard step 2 — details form
  POST /timeclock/time-off/request/{token}/submit               Wizard step 3 — submit + queue sync
  GET  /timeclock/time-off/mine/{token}                         My Requests list
  GET  /timeclock/time-off/mine/{token}/{rid}                   My Requests detail (with Cancel)
  POST /timeclock/time-off/mine/{token}/{rid}/cancel            Cancel a pending or approved request
  GET  /timeclock/time-off/mine/{token}/{rid}/edit              Edit form pre-filled with current values
  POST /timeclock/time-off/mine/{token}/{rid}/edit              Persist edits + queue Odoo write
"""

from __future__ import annotations

import calendar as _cal
import json as _json
from datetime import date as _date, timedelta as _td

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import (
    db,
    odoo_client,
    schedule_store,
    settings_store,
    time_off_balances,
    time_off_sync,
)
from ..deps import templates
from .kiosk import (
    _is_time_off_only,
    _mint_token,
    _person_by_id,
    _verify_token,
)

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


@router.get("/timeclock/time-off/{token}", response_class=HTMLResponse)
def time_off_landing(request: Request, token: str):
    """Landing page: the time-off-shape picker (Full Day + three
    partial-day cards) up top, then My Requests and Who's Out below a
    divider. Same HMAC gate as the rest of /timeclock — an
    invalid or expired token bounces to /timeclock so a stale URL on a shared
    device never lets the next user act as the previous one.

    Mints a fresh token before render so a user reading the screen (or
    pausing to think) doesn't time out mid-tap. The counts come from the
    local `time_off_requests` mirror so this is a few millisecond
    Postgres SELECTs, no Odoo XML-RPC on the hot path.
    """
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/timeclock", status_code=303)
    p = _person_by_id(person_id)
    if not p:
        return RedirectResponse(url="/timeclock", status_code=303)
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
            # Salaried staff land here directly (no punch dashboard), so
            # their "Back" should exit to the home roster, not /dashboard.
            "time_off_only": _is_time_off_only(p),
            "bilingual": bool(p.get("spanish_speaker")),
        },
    )


@router.get("/timeclock/time-off/request/{token}", response_class=HTMLResponse)
def request_shape(request: Request, token: str):
    """Legacy shape-picker route. The shape cards now live on the landing
    page (`kiosk_time_off_landing.html`), so this just redirects there —
    kept so old bookmarks and the wizard's bad-input fallbacks still land
    somewhere sensible. Same HMAC gate; invalid token bounces to /timeclock.
    """
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/timeclock", status_code=303)
    p = _person_by_id(person_id)
    if not p:
        return RedirectResponse(url="/timeclock", status_code=303)
    return RedirectResponse(
        url=f"/timeclock/time-off/{_mint_token(person_id)}",
        status_code=303,
    )


# ----- Wizard step 2 — details form (Task 17) -----

_VALID_SHAPES = {"full_day", "late_arrival", "early_leave", "midday_gap"}


def _fetch_visible_leave_types(shape: str) -> list[dict]:
    """All hr.leave.type from local cache minus hidden ones, filtered to
    the unit matching the shape.

    The shape picker decides which time-off shape the user wants
    (full day vs partial day); the leave-type unit must agree — day-unit
    types only fit a full-day shape, hour-unit types only fit the three
    partial-day shapes. Half-day-unit types behave like day-unit for our
    purposes (kept in the full-day bucket).

    Reads from `leave_types_cache` first. If the table is empty (the
    poller hasn't successfully run yet — common right after flipping the
    feature flag on), falls back to `odoo_client.fetch_leave_types()` and
    opportunistically populates the cache so the next read is fast.
    """
    rows = db.query(
        "SELECT holiday_status_id, name, request_unit, requires_allocation "
        "FROM leave_types_cache WHERE active = TRUE "
        "ORDER BY name"
    )
    if not rows:
        # Cache miss — try Odoo directly. Swallows errors and returns []
        # if Odoo is unreachable (template's existing empty-state copy
        # explains the situation to the user).
        rows = _fallback_fetch_and_cache_leave_types()

    hidden = set(settings_store.get_hidden_leave_type_ids())
    out: list[dict] = []
    for r in rows:
        if r["holiday_status_id"] in hidden:
            continue
        if shape == "full_day":
            # No unit filter: full-day shape works with day-unit,
            # half-day-unit, and hour-unit types alike. Hour-unit types
            # (e.g., "Unpaid Time Off") get submitted with full-shift
            # hour bounds — see `_type_request_unit` + submit handlers.
            pass
        else:
            # Partial-day shapes only make sense against hour-unit types.
            if r["request_unit"] != "hour":
                continue
        out.append({
            "id": r["holiday_status_id"],
            "name": r["name"],
            "request_unit": r["request_unit"],
            "requires_allocation": r["requires_allocation"],
        })
    return out


def _type_request_unit(holiday_status_id: int) -> str | None:
    """Return the ``request_unit`` ('day' | 'half_day' | 'hour') for a
    given leave type, looked up from `leave_types_cache`. Returns None
    if the type isn't in the cache (caller treats as day-unit fallback
    so an unknown type doesn't accidentally inject hour bounds)."""
    rows = db.query(
        "SELECT request_unit FROM leave_types_cache "
        "WHERE holiday_status_id = %s",
        (holiday_status_id,),
    )
    return rows[0]["request_unit"] if rows else None


def _fallback_fetch_and_cache_leave_types() -> list[dict]:
    """Hit Odoo directly for the leave types and write them back into the
    local cache so the next read is fast. Returns rows in the same shape
    `_fetch_visible_leave_types` expects from the cache table
    (``{holiday_status_id, name, request_unit, requires_allocation}``).

    Bust the in-process cache first so a previously-cached empty list
    (from a failed cold-start auth attempt) doesn't shadow the fresh
    Odoo call — same defense the Settings refresh button uses."""
    import logging
    _log = logging.getLogger(__name__)
    try:
        odoo_client._leave_types_cache = None
        types = odoo_client.fetch_leave_types()
    except Exception as e:  # noqa: BLE001 — return empty + log; UI shows the empty state
        _log.warning(
            "kiosk wizard fallback fetch_leave_types failed: %s", e,
            exc_info=True,
        )
        return []

    # Opportunistically populate the cache table so the next render
    # doesn't pay the Odoo round-trip again. Per-row try/except so one
    # bad row (e.g., schema CHECK violation) doesn't poison the batch.
    for t in types:
        try:
            db.execute(
                "INSERT INTO leave_types_cache "
                "(holiday_status_id, name, request_unit, "
                "requires_allocation, color, active, last_pulled_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (holiday_status_id) DO UPDATE SET "
                "name = EXCLUDED.name, "
                "request_unit = EXCLUDED.request_unit, "
                "requires_allocation = EXCLUDED.requires_allocation, "
                "color = EXCLUDED.color, "
                "active = EXCLUDED.active, "
                "last_pulled_at = now()",
                (t["id"], t["name"], t["request_unit"],
                 t["requires_allocation"], t.get("color"),
                 t.get("active", True)),
            )
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "leave_types_cache insert failed for type id=%s: %s",
                t.get("id"), e, exc_info=True,
            )

    # Return rows in the same shape the caller expects from the cache
    # table — only the four fields that get filtered/displayed.
    return [
        {
            "holiday_status_id": t["id"],
            "name": t["name"],
            "request_unit": t["request_unit"],
            "requires_allocation": t["requires_allocation"],
        }
        for t in types
    ]


def _refresh_and_load_balances(person_odoo_id: int) -> list[dict]:
    """Synchronous balance refresh before render (~200-500ms blocking).

    The wizard needs a fresh balance number to show in the panel and feed
    into the live-calc — a stale number would mean the JS would happily
    let the user submit something they no longer have allocation for.
    `time_off_balances.refresh_for_employee` already swallows Odoo errors
    so the worst case is a render with the previous cached balance, which
    is still better than crashing the request."""
    try:
        time_off_balances.refresh_for_employee(person_odoo_id)
    except Exception:  # noqa: BLE001 — never let a refresh error block the wizard
        pass
    return time_off_balances.get_for_employee(person_odoo_id)


def _shift_window_for(person_odoo_id: int) -> tuple[float, float]:
    """Return (hour_from, hour_to) for the employee's shift.

    Tries Odoo `resource.calendar` first (a per-employee shift); falls
    back to the global Company Schedule (Settings → Timeclock → Company
    Schedule) which already stores shift_start / shift_end. The three
    partial-day shapes (late_arrival, early_leave, midday_gap) need
    these bounds to validate the user's chosen time(s) and to drive the
    live-calc request-size math in the JS."""
    try:
        cal = odoo_client.fetch_resource_calendar(person_odoo_id)
    except Exception:  # noqa: BLE001 — fall back to global schedule rather than crash
        cal = None
    if (cal
            and cal.get("hour_from") is not None
            and cal.get("hour_to") is not None):
        return (float(cal["hour_from"]), float(cal["hour_to"]))
    # Convert the global Company Schedule's datetime.time fields into
    # decimal-hour floats (e.g. time(6, 0) → 6.0, time(14, 30) → 14.5).
    sched = schedule_store.current()
    return (
        sched.shift_start.hour + sched.shift_start.minute / 60.0,
        sched.shift_end.hour + sched.shift_end.minute / 60.0,
    )


@router.get("/timeclock/time-off/request/{token}/details",
            response_class=HTMLResponse)
def request_details(request: Request, token: str, shape: str = "full_day"):
    """Wizard step 2 — the details form.

    Branches by ``shape`` to ask for the right inputs:
      - full_day → start date + end date
      - late_arrival → date + arrival time (shift_from..shift_to)
      - early_leave → date + leave time (shift_from..shift_to)
      - midday_gap → date + leave/return times within shift

    Shows a balance panel that the client-side JS keeps up to date as
    the user changes inputs; the JS lives in `static/kiosk_time_off.js`.
    Bad token → /timeclock; bad shape → back to the shape picker (never
    render the form with an invalid shape value)."""
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/timeclock", status_code=303)
    p = _person_by_id(person_id)
    if not p or not p.get("odoo_id"):
        return RedirectResponse(url="/timeclock", status_code=303)
    if shape not in _VALID_SHAPES:
        return RedirectResponse(
            url=f"/timeclock/time-off/{_mint_token(person_id)}",
            status_code=303,
        )
    fresh = _mint_token(person_id)
    types = _fetch_visible_leave_types(shape)
    balances = _refresh_and_load_balances(p["odoo_id"])
    # Cast numeric Decimals to floats so the JSON-embedded JS payload
    # in the template gets plain numbers instead of "Decimal('15.00')"
    # repr (Jinja's `{{ x }}` would print the Decimal verbatim).
    balances_by_type = {
        b["holiday_status_id"]: {
            "unit": b["unit"],
            "available": float(b["available"]),
            "available_practical": float(b["available_practical"]),
            "pending": float(b["pending"]),
        }
        for b in balances
    }
    shift_from, shift_to = _shift_window_for(p["odoo_id"])
    # For partial-day shapes, the type is fixed (always the unpaid
    # hour-unit type — typically "Custom Hours" in Odoo). The user
    # doesn't pick; we send the first available hour-unit type id
    # automatically. `types` is already filtered to hour-unit-only
    # for those shapes by `_fetch_visible_leave_types`.
    partial_day_type = types[0] if (shape != "full_day" and types) else None
    work_weekdays = sorted(schedule_store.current().work_weekdays)
    return templates.TemplateResponse(
        request,
        "kiosk_time_off_request_details.html",
        {
            "person": p,
            "token": fresh,
            "shape": shape,
            "leave_types": types,
            "partial_day_type": partial_day_type,
            "balances_by_type": balances_by_type,
            "shift_from": shift_from,
            "shift_to": shift_to,
            "today_iso": _date.today().isoformat(),
            "work_weekdays": work_weekdays,
            "bilingual": bool(p.get("spanish_speaker")),
        },
    )


# ----- Wizard step 3 — submit handler (Task 18) -----


def _parse_time_to_float(s: str | None) -> float | None:
    """Convert a "HH:MM" string from an HTML ``<input type="time">`` into a
    decimal-hour float so it can be compared against shift bounds.

    Returns None on missing or malformed input — callers treat None as
    "no time provided" and either skip validation (full-day shape) or
    return a user-facing error (partial-day shapes that need the value)."""
    if not s:
        return None
    try:
        hh, mm = s.split(":")
        return int(hh) + int(mm) / 60.0
    except (ValueError, AttributeError):
        return None


def _shape_to_hour_bounds(
    shape: str,
    time_a: str,
    time_b: str,
    shift_from: float,
    shift_to: float,
) -> tuple[float | None, float | None, str | None]:
    """Validate user-supplied times against the shape and shift window.

    Returns ``(hour_from, hour_to, error)``:
      - full_day → ``(None, None, None)``: no hours stored
      - late_arrival → arrival ``time_b`` must be inside the shift and
        after the start; hours span ``(shift_from, arrival)``
      - early_leave → leave ``time_a`` must be inside the shift and
        before the end; hours span ``(leave, shift_to)``
      - midday_gap → both ``time_a`` and ``time_b`` inside the shift,
        with ``time_b > time_a``; hours span ``(time_a, time_b)``

    Returning the (None, None, msg) tuple instead of raising lets the
    submit handler re-render the details form with the error string in
    the existing ``k-error`` banner, matching the same UX as the rest of
    the kiosk forms."""
    if shape == "full_day":
        return (None, None, None)
    a = _parse_time_to_float(time_a)
    b = _parse_time_to_float(time_b)
    if shape == "late_arrival":
        if b is None:
            return (None, None, "Arrival time required")
        if b <= shift_from:
            return (None, None, "Arrival time must be after shift start")
        if b > shift_to:
            return (None, None, "Arrival time must be within your shift")
        return (shift_from, b, None)
    if shape == "early_leave":
        if a is None:
            return (None, None, "Leave time required")
        if a < shift_from:
            return (None, None, "Leave time must be after shift start")
        if a >= shift_to:
            return (None, None, "Leave time must be before shift end")
        return (a, shift_to, None)
    if shape == "midday_gap":
        if a is None or b is None:
            return (None, None, "Both times required")
        if a < shift_from or b > shift_to or b <= a:
            return (None, None, "Times must be within your shift, end > start")
        return (a, b, None)
    return (None, None, f"Unknown shape: {shape}")


def _compute_working_hours_json(
    shape: str,
    hour_from: float | None,
    hour_to: float | None,
    shift_from: float,
    shift_to: float,
) -> list[dict] | None:
    """Return the COMPLEMENT of the leave window — the ranges the employee
    is still working — as a list of ``{from, to}`` dicts.

    For ``full_day`` we return ``None`` (whole shift is off, no working
    complement exists). For partial-day shapes, we return up to two
    ranges: the morning window before the leave and the afternoon window
    after it. If the leave somehow covers the whole shift (shouldn't
    happen post-validation), we fall back to a single range covering the
    whole shift so the column doesn't end up empty.

    Stored in the ``working_hours_json`` JSONB column so the scheduler
    cascade and the kiosk calendar can render partial-day leaves with
    the actual hours-worked breakdown without re-deriving from times."""
    if shape == "full_day":
        return None
    if hour_from is None or hour_to is None:
        return None
    out: list[dict] = []
    if hour_from > shift_from:
        out.append({"from": shift_from, "to": hour_from})
    if hour_to < shift_to:
        out.append({"from": hour_to, "to": shift_to})
    return out or [{"from": shift_from, "to": shift_to}]


def _insert_request_row(
    *,
    person_odoo_id: int,
    shape: str,
    holiday_status_id: int,
    date_from: _date,
    date_to: _date,
    hour_from: float | None,
    hour_to: float | None,
    working_hours_json: list[dict] | None,
    note: str | None,
) -> int:
    """Insert a draft ``time_off_requests`` row and return its id.

    Uses ``db.cursor()`` so the INSERT and the RETURNING id fetch share
    a single transaction — the row is either committed with a real id
    or rolled back entirely. New rows always start ``state='draft'`` /
    ``synced_to_odoo=FALSE`` so the sync sweep picks them up if the
    immediate ``push_one`` call fails."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO time_off_requests "
            "(person_odoo_id, originating_kiosk_user, shape, "
            " holiday_status_id, date_from, date_to, hour_from, hour_to, "
            " working_hours_json, note, state, synced_to_odoo) "
            "VALUES (%s, TRUE, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', FALSE) "
            "RETURNING id",
            (
                person_odoo_id, shape, holiday_status_id, date_from, date_to,
                hour_from, hour_to,
                _json.dumps(working_hours_json) if working_hours_json else None,
                note,
            ),
        )
        return cur.fetchone()["id"]


def _queue_push(request_id: int) -> None:
    """Run the push synchronously from the background-task slot.

    Exists as a standalone module function (not an inline lambda inside
    the route) so tests can monkeypatch it to capture which request_id
    was queued without having to monkeypatch the sync engine itself."""
    time_off_sync.push_one(request_id)


@router.post(
    "/timeclock/time-off/request/{token}/submit",
    response_class=HTMLResponse,
)
def request_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    token: str,
    shape: str = Form(...),
    holiday_status_id: int = Form(...),
    date_from: str = Form(...),
    date_to: str = Form(...),
    time_a: str = Form(default=""),
    time_b: str = Form(default=""),
    note: str = Form(default=""),
):
    """Wizard step 3 — server-side validation, persist a draft row, queue
    the Odoo push, render the success page.

    Validation cascade (bail out at the first failure):
      1. Token + person check (same gate as the other routes)
      2. Shape in the known set
      3. Dates parse as ISO and end >= start (we silently swap if not)
      4. Times pass ``_shape_to_hour_bounds`` against the shift window

    On any time-validation failure we re-render the details form with a
    422 status + the ``error`` message in the existing ``k-error`` banner,
    so the user sees what to fix without losing their place in the wizard.

    On success we insert the row in ``state='draft'`` /
    ``synced_to_odoo=FALSE`` and schedule a background ``push_one`` —
    that's the immediate first sync attempt. If it fails, the 60s sweep
    in ``time_off_sync.retry_unsynced_requests`` will keep retrying."""
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/timeclock", status_code=303)
    p = _person_by_id(person_id)
    if not p or not p.get("odoo_id"):
        return RedirectResponse(url="/timeclock", status_code=303)
    if shape not in _VALID_SHAPES:
        return RedirectResponse(
            url=f"/timeclock/time-off/{_mint_token(person_id)}",
            status_code=303,
        )
    try:
        df = _date.fromisoformat(date_from)
        dt = _date.fromisoformat(date_to)
    except ValueError:
        return RedirectResponse(
            url=f"/timeclock/time-off/{_mint_token(person_id)}",
            status_code=303,
        )
    # Partial-day shapes (arrive late / leave early / mid-day gap) are always
    # a SINGLE day: the user picks one date (submitted as date_from); the
    # hidden date_to isn't a real second date. Force both ends to the selected
    # date so we never post a today->selected multi-day span.
    if shape != "full_day":
        dt = df
    elif dt < df:
        # Full-day: swap an accidentally inverted range rather than reject.
        df, dt = dt, df

    shift_from, shift_to = _shift_window_for(p["odoo_id"])
    hour_from, hour_to, err = _shape_to_hour_bounds(
        shape, time_a, time_b, shift_from, shift_to,
    )
    if err:
        # Re-render the details form with the error in the existing
        # k-error banner. balances_by_type matches the shape that
        # `request_details` builds so the form's JS payload stays valid.
        balances = time_off_balances.get_for_employee(p["odoo_id"])
        balances_by_type = {
            b["holiday_status_id"]: {
                "unit": b["unit"],
                "available": float(b["available"]),
                "available_practical": float(b["available_practical"]),
                "pending": float(b["pending"]),
            }
            for b in balances
        }
        rerender_types = _fetch_visible_leave_types(shape)
        rerender_partial = (
            rerender_types[0] if (shape != "full_day" and rerender_types) else None
        )
        return templates.TemplateResponse(
            request,
            "kiosk_time_off_request_details.html",
            {
                "person": p,
                "token": _mint_token(person_id),
                "shape": shape,
                "leave_types": rerender_types,
                "partial_day_type": rerender_partial,
                "balances_by_type": balances_by_type,
                "shift_from": shift_from,
                "shift_to": shift_to,
                "today_iso": _date.today().isoformat(),
                "work_weekdays": sorted(schedule_store.current().work_weekdays),
                "error": err,
                "bilingual": bool(p.get("spanish_speaker")),
            },
            status_code=422,
        )

    # Full-day shape against an hour-unit type (e.g., "Unpaid Time Off")
    # needs hour bounds covering the full shift each day, otherwise Odoo
    # won't compute a duration. Day-unit types stay at None bounds.
    if shape == "full_day" and _type_request_unit(holiday_status_id) == "hour":
        hour_from = shift_from
        hour_to = shift_to

    working_hours = _compute_working_hours_json(
        shape, hour_from, hour_to, shift_from, shift_to,
    )

    request_id = _insert_request_row(
        person_odoo_id=p["odoo_id"],
        shape=shape,
        holiday_status_id=holiday_status_id,
        date_from=df,
        date_to=dt,
        hour_from=hour_from,
        hour_to=hour_to,
        working_hours_json=working_hours,
        note=note.strip() or None,
    )
    background_tasks.add_task(_queue_push, request_id)

    return templates.TemplateResponse(
        request,
        "kiosk_time_off_success.html",
        {
            "person": p,
            "token": _mint_token(person_id),
            "shape": shape,
            "date_from": df.isoformat(),
            "date_to": dt.isoformat(),
            "bilingual": bool(p.get("spanish_speaker")),
        },
    )


# ----- My Requests list, detail, and cancel handler (Task 19) -----


def _load_request(rid: int, person_odoo_id: int) -> dict | None:
    """Fetch one ``time_off_requests`` row scoped to the caller's
    ``person_odoo_id`` so a leaked URL with a different employee's
    request id can't fall through to a render. Returns the row dict
    on a hit, ``None`` on miss — callers redirect on None."""
    rows = db.query(
        "SELECT id, person_odoo_id, originating_kiosk_user, shape, "
        "holiday_status_id, date_from, date_to, hour_from, hour_to, "
        "note, state, odoo_leave_id, sync_error "
        "FROM time_off_requests WHERE id = %s AND person_odoo_id = %s",
        (rid, person_odoo_id),
    )
    return rows[0] if rows else None


def _set_row_state(rid: int, state: str) -> None:
    """Flip a row to a new state and mark it unsynced so the next sweep
    picks it up. Used by the cancel handler to drive a row from
    ``confirm``/``validate`` to ``draft_cancel`` before queuing the push
    that translates ``draft_cancel`` into ``refuse_leave`` on Odoo."""
    db.execute(
        "UPDATE time_off_requests SET state = %s, synced_to_odoo = FALSE, "
        "updated_at = now() WHERE id = %s",
        (state, rid),
    )


def _list_my_requests(person_odoo_id: int) -> list[dict]:
    """All of one person's requests, newest first, joined to the local
    leave-types cache so the list can show the friendly type name
    instead of a numeric id. Capped at 100 because the kiosk is a
    glanceable surface — older requests still exist in the DB if HR
    needs them, but a worker doesn't need to scroll past a year of
    history on a touchscreen."""
    rows = db.query(
        "SELECT r.id, r.shape, r.date_from, r.date_to, r.hour_from, "
        "r.hour_to, r.state, r.note, r.holiday_status_id, "
        "r.originating_kiosk_user, t.name AS type_name "
        "FROM time_off_requests r "
        "LEFT JOIN leave_types_cache t "
        "ON t.holiday_status_id = r.holiday_status_id "
        "WHERE r.person_odoo_id = %s "
        "ORDER BY r.created_at DESC LIMIT 100",
        (person_odoo_id,),
    )
    return rows


def _state_to_bucket(state: str) -> str:
    """Map the raw ``hr.leave.state`` value (plus our local draft_*
    states) to a one-word bucket the kiosk UI can color-code. Keeps
    the template branch-free; new Odoo states fall through to the
    raw value so a sync change doesn't crash render."""
    if state in ("confirm", "validate1", "draft", "draft_edit"):
        return "Pending"
    if state == "validate":
        return "Approved"
    if state in ("refuse", "cancel", "draft_cancel"):
        return "Rejected"
    return state


@router.get("/timeclock/time-off/mine/{token}", response_class=HTMLResponse)
def mine_list(request: Request, token: str):
    """My Requests — newest 100 requests for the calling employee.

    Mints a fresh token on render so a user reading the list (or
    pausing to think about which one to tap) has the full TTL before
    they navigate into a detail page. Each row links to the detail
    view with the fresh token already baked into the href."""
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/timeclock", status_code=303)
    p = _person_by_id(person_id)
    if not p or not p.get("odoo_id"):
        return RedirectResponse(url="/timeclock", status_code=303)
    fresh = _mint_token(person_id)
    rows = _list_my_requests(p["odoo_id"])
    for r in rows:
        r["bucket"] = _state_to_bucket(r["state"])
    return templates.TemplateResponse(
        request,
        "kiosk_time_off_mine.html",
        {"person": p, "token": fresh, "requests": rows, "bilingual": bool(p.get("spanish_speaker"))},
    )


@router.get("/timeclock/time-off/mine/{token}/{rid}",
            response_class=HTMLResponse)
def mine_detail(request: Request, token: str, rid: int):
    """My Requests detail — the row + a Cancel button when applicable.

    The Cancel button only renders for kiosk-originated, non-terminal
    rows; HR-entered rows (originating_kiosk_user=FALSE) shouldn't be
    cancellable from the employee's surface, and refused/cancelled
    rows have nothing left to do. A row id that doesn't exist (or
    belongs to a different person) bounces back to the list, never
    leaks data, never crashes."""
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/timeclock", status_code=303)
    p = _person_by_id(person_id)
    if not p or not p.get("odoo_id"):
        return RedirectResponse(url="/timeclock", status_code=303)
    row = _load_request(rid, p["odoo_id"])
    if not row:
        return RedirectResponse(
            url=f"/timeclock/time-off/mine/{_mint_token(person_id)}",
            status_code=303,
        )
    row["bucket"] = _state_to_bucket(row["state"])
    fresh = _mint_token(person_id)
    return templates.TemplateResponse(
        request,
        "kiosk_time_off_mine_detail.html",
        {"person": p, "token": fresh, "request_row": row, "bilingual": bool(p.get("spanish_speaker"))},
    )


@router.post("/timeclock/time-off/mine/{token}/{rid}/cancel",
             response_class=HTMLResponse)
def mine_cancel(
    request: Request,
    background_tasks: BackgroundTasks,
    token: str,
    rid: int,
):
    """Cancel a request. Two branches:

      - ``odoo_leave_id IS NULL`` — the row never made it to Odoo
        (still in the initial draft/retry queue). DELETE the local
        row outright; there's nothing on the Odoo side to refuse, and
        keeping a stale draft around would just be noise in the list.
      - ``odoo_leave_id`` is set — flip the local state to
        ``draft_cancel`` and queue a background push. The push routes
        through ``time_off_sync._push_cancel`` which calls
        ``odoo_client.refuse_leave`` (the same action Odoo uses for
        both pending-cancel and approved-cancel). The local row stays
        in place so the sweep can retry on failure and the user sees
        the row land in the Rejected bucket once the push completes.

    Auth gate is the standard token + person check; missing row
    bounces to the list with a fresh token rather than 404-ing on the
    employee."""
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/timeclock", status_code=303)
    p = _person_by_id(person_id)
    if not p or not p.get("odoo_id"):
        return RedirectResponse(url="/timeclock", status_code=303)
    row = _load_request(rid, p["odoo_id"])
    if not row:
        return RedirectResponse(
            url=f"/timeclock/time-off/mine/{_mint_token(person_id)}",
            status_code=303,
        )
    if row["odoo_leave_id"] is None:
        # Never made it to Odoo — just delete locally.
        db.execute(
            "DELETE FROM time_off_requests WHERE id = %s",
            (rid,),
        )
    else:
        _set_row_state(rid, "draft_cancel")
        background_tasks.add_task(_queue_push, rid)
    return RedirectResponse(
        url=f"/timeclock/time-off/mine/{_mint_token(person_id)}",
        status_code=303,
    )


# ----- Edit handler — re-open the wizard for an existing request (Task 28) -----


def _update_request_row(
    *,
    rid: int,
    person_odoo_id: int,
    shape: str,
    holiday_status_id: int,
    date_from: _date,
    date_to: _date,
    hour_from: float | None,
    hour_to: float | None,
    working_hours_json: list[dict] | None,
    note: str | None,
) -> None:
    """Update an existing ``time_off_requests`` row to the new field
    values and flip it to ``state='draft_edit'`` so the sync sweep picks
    it up as a write back to the same Odoo ``hr.leave`` record.

    Scoped by ``person_odoo_id`` so a leaked rid can't be used to mutate
    another employee's row. Mirrors ``_insert_request_row`` for shape;
    keeps ``odoo_leave_id`` untouched so the push routes through
    ``time_off_sync._push_edit`` (write) instead of ``_push_new``."""
    db.execute(
        "UPDATE time_off_requests SET shape = %s, holiday_status_id = %s, "
        "date_from = %s, date_to = %s, hour_from = %s, hour_to = %s, "
        "working_hours_json = %s, note = %s, "
        "state = 'draft_edit', synced_to_odoo = FALSE, "
        "updated_at = now() "
        "WHERE id = %s AND person_odoo_id = %s",
        (
            shape, holiday_status_id, date_from, date_to,
            hour_from, hour_to,
            _json.dumps(working_hours_json) if working_hours_json else None,
            note, rid, person_odoo_id,
        ),
    )


@router.get("/timeclock/time-off/mine/{token}/{rid}/edit",
            response_class=HTMLResponse)
def mine_edit(request: Request, token: str, rid: int):
    """Re-open the details form pre-filled with this row's current values.

    Same HMAC gate + row-ownership scope as the rest of the /mine routes;
    a stale id (or one for a different employee) bounces to the list. The
    template branches on ``edit_mode`` so the form ``action`` posts to the
    edit submit handler instead of the new-request submit handler.

    Re-uses ``_fetch_visible_leave_types(row["shape"])`` so the visible
    type list stays consistent with the row's existing shape — an edit
    keeps the same shape (changing shape mid-edit would mean a different
    request entirely; the user can cancel and re-submit if they need a
    different shape)."""
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/timeclock", status_code=303)
    p = _person_by_id(person_id)
    if not p or not p.get("odoo_id"):
        return RedirectResponse(url="/timeclock", status_code=303)
    row = _load_request(rid, p["odoo_id"])
    if not row:
        return RedirectResponse(
            url=f"/timeclock/time-off/mine/{_mint_token(person_id)}",
            status_code=303,
        )
    fresh = _mint_token(person_id)
    types = _fetch_visible_leave_types(row["shape"])
    balances = _refresh_and_load_balances(p["odoo_id"])
    # Cast Decimals to floats so the JSON-embedded JS payload in the
    # template gets plain numbers, matching the new-request branch.
    balances_by_type = {
        b["holiday_status_id"]: {
            "unit": b["unit"],
            "available": float(b["available"]),
            "available_practical": float(b["available_practical"]),
            "pending": float(b["pending"]),
        }
        for b in balances
    }
    shift_from, shift_to = _shift_window_for(p["odoo_id"])
    partial_day_type = (
        types[0] if (row["shape"] != "full_day" and types) else None
    )

    return templates.TemplateResponse(
        request,
        "kiosk_time_off_request_details.html",
        {
            "person": p,
            "token": fresh,
            "shape": row["shape"],
            "leave_types": types,
            "partial_day_type": partial_day_type,
            "balances_by_type": balances_by_type,
            "shift_from": shift_from,
            "shift_to": shift_to,
            "today_iso": _date.today().isoformat(),
            "work_weekdays": sorted(schedule_store.current().work_weekdays),
            "edit_mode": True,
            "edit_rid": rid,
            "bilingual": bool(p.get("spanish_speaker")),
            "prefill": {
                "holiday_status_id": row["holiday_status_id"],
                "date_from": (
                    row["date_from"].isoformat() if row["date_from"] else ""
                ),
                "date_to": (
                    row["date_to"].isoformat() if row["date_to"] else ""
                ),
                "hour_from": (
                    float(row["hour_from"])
                    if row["hour_from"] is not None else None
                ),
                "hour_to": (
                    float(row["hour_to"])
                    if row["hour_to"] is not None else None
                ),
                "note": row["note"] or "",
            },
        },
    )


@router.post("/timeclock/time-off/mine/{token}/{rid}/edit",
             response_class=HTMLResponse)
def mine_edit_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    token: str,
    rid: int,
    shape: str = Form(...),
    holiday_status_id: int = Form(...),
    date_from: str = Form(...),
    date_to: str = Form(...),
    time_a: str = Form(default=""),
    time_b: str = Form(default=""),
    note: str = Form(default=""),
):
    """Persist edits to an existing request + queue the Odoo write.

    Validation cascade mirrors ``request_submit`` (Task 18) exactly — bad
    token → /timeclock, bad row → list, bad shape/date → back to the detail
    page, bad time → re-render the form in edit_mode with the error. On
    success we UPDATE the row to ``draft_edit`` (not ``draft`` — the row
    already exists on Odoo) and schedule a background ``push_one`` that
    routes through ``time_off_sync._push_edit`` to write the changed
    fields back to the same ``hr.leave`` record.

    The 60s sweep in ``time_off_sync.retry_unsynced_requests`` will keep
    retrying if the immediate push fails, same as the new-request flow."""
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/timeclock", status_code=303)
    p = _person_by_id(person_id)
    if not p or not p.get("odoo_id"):
        return RedirectResponse(url="/timeclock", status_code=303)
    row = _load_request(rid, p["odoo_id"])
    if not row:
        return RedirectResponse(
            url=f"/timeclock/time-off/mine/{_mint_token(person_id)}",
            status_code=303,
        )
    if shape not in _VALID_SHAPES:
        return RedirectResponse(
            url=f"/timeclock/time-off/mine/{_mint_token(person_id)}/{rid}",
            status_code=303,
        )
    try:
        df = _date.fromisoformat(date_from)
        dt = _date.fromisoformat(date_to)
    except ValueError:
        return RedirectResponse(
            url=f"/timeclock/time-off/mine/{_mint_token(person_id)}/{rid}",
            status_code=303,
        )
    # Partial-day shapes are single-day; force end = start (see request_submit).
    if shape != "full_day":
        dt = df
    elif dt < df:
        df, dt = dt, df

    shift_from, shift_to = _shift_window_for(p["odoo_id"])
    hour_from, hour_to, err = _shape_to_hour_bounds(
        shape, time_a, time_b, shift_from, shift_to,
    )
    if err:
        # Re-render the form in edit mode with the error so the user can
        # fix it without bouncing back to the detail page and losing
        # their inputs. balances_by_type matches the shape that the GET
        # branch builds so the form's JS payload stays valid.
        types = _fetch_visible_leave_types(shape)
        balances = _refresh_and_load_balances(p["odoo_id"])
        balances_by_type = {
            b["holiday_status_id"]: {
                "unit": b["unit"],
                "available": float(b["available"]),
                "available_practical": float(b["available_practical"]),
                "pending": float(b["pending"]),
            }
            for b in balances
        }
        partial_day_type = (
            types[0] if (shape != "full_day" and types) else None
        )
        return templates.TemplateResponse(
            request,
            "kiosk_time_off_request_details.html",
            {
                "person": p,
                "token": _mint_token(person_id),
                "shape": shape,
                "leave_types": types,
                "partial_day_type": partial_day_type,
                "balances_by_type": balances_by_type,
                "shift_from": shift_from,
                "shift_to": shift_to,
                "today_iso": _date.today().isoformat(),
                "work_weekdays": sorted(schedule_store.current().work_weekdays),
                "edit_mode": True,
                "edit_rid": rid,
                "error": err,
                "bilingual": bool(p.get("spanish_speaker")),
            },
            status_code=422,
        )

    # Same hour-bound injection as request_submit so edits to a
    # full-day request against an hour-unit type stay consistent.
    if shape == "full_day" and _type_request_unit(holiday_status_id) == "hour":
        hour_from = shift_from
        hour_to = shift_to

    working_hours = _compute_working_hours_json(
        shape, hour_from, hour_to, shift_from, shift_to,
    )
    _update_request_row(
        rid=rid,
        person_odoo_id=p["odoo_id"],
        shape=shape,
        holiday_status_id=holiday_status_id,
        date_from=df,
        date_to=dt,
        hour_from=hour_from,
        hour_to=hour_to,
        working_hours_json=working_hours,
        note=note.strip() or None,
    )
    background_tasks.add_task(_queue_push, rid)
    return RedirectResponse(
        url=f"/timeclock/time-off/mine/{_mint_token(person_id)}/{rid}",
        status_code=303,
    )


# ----- Who's Out calendar (Task 20) -----


def _fmt_hf(h: float) -> str:
    """Format a decimal-hour float as a 12-hour clock string.

    ``6.5 -> "6:30am"``, ``14.0 -> "2:00pm"``, ``12.0 -> "12:00pm"``,
    ``0.0 -> "12:00am"``. Used by the calendar labels so the timing
    shows up in a glanceable form on the kiosk; matches how the rest
    of the punch UI prints clock times."""
    hh = int(h)
    mm = int(round((h - hh) * 60))
    suffix = "am" if hh < 12 else "pm"
    disp = hh if hh <= 12 else hh - 12
    if disp == 0:
        disp = 12
    return f"{disp}:{mm:02d}{suffix}"


def _label_for(r: dict) -> str:
    """Render a privacy-safe timing label for one approved leave row.

    Deliberately omits the leave-type name — coworkers should see that
    someone is out and when, but not why. The four shapes map to:
      - ``full_day``   -> ``"full day"``
      - ``late_arrival`` -> ``"arrives 9:00am"`` (arrival = hour_to)
      - ``early_leave``  -> ``"leaves 2:00pm"`` (leave = hour_from)
      - ``midday_gap``   -> ``"10:00am–12:00pm"`` (gap = hour_from..hour_to)
    """
    if r["shape"] == "full_day":
        return "full day"
    hf = float(r["hour_from"] or 0)
    ht = float(r["hour_to"] or 0)
    if r["shape"] == "late_arrival":
        return f"arrives {_fmt_hf(ht)}"
    if r["shape"] == "early_leave":
        return f"leaves {_fmt_hf(hf)}"
    return f"{_fmt_hf(hf)}–{_fmt_hf(ht)}"


def _parse_holiday_date(s):
    """Odoo returns 'YYYY-MM-DD HH:MM:SS' strings for datetime fields.
    Strip the time component to get a date for the per-day fan-out.

    Tolerant of already-parsed values (passing in a ``date`` returns it
    as-is) so callers don't have to branch on type; returns ``None`` on
    anything unparseable so the caller can skip that holiday rather than
    crash the whole calendar render."""
    if not s:
        return None
    if hasattr(s, "isoformat"):  # already a date
        return s
    try:
        return _date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _approved_by_day(start_d: _date, end_d: _date) -> dict:
    """Return ``{date: [{name, label, source}, ...]}`` for approved
    leaves and company-wide public holidays overlapping ``[start_d, end_d]``.

    Only reads ``state='validate'`` for leaves — the kiosk calendar
    shouldn't surface pending requests because HR may still refuse them;
    once approved, every overlapping day in the request range is fanned
    out so the cell-by-cell template loop doesn't have to do its own
    date math. Names come from the local ``people`` table joined on
    ``odoo_id`` so we never need an Odoo round-trip on render.

    Public holidays come from ``resource.calendar.leaves`` (rows with
    ``resource_id=False``) and are tagged ``source='holiday'`` so the
    templates can style them distinctly from per-employee leaves. A
    failing Odoo call swallows to an empty list so the calendar still
    renders the rest of the day's entries during a transient outage."""
    rows = db.query(
        "SELECT r.shape, r.date_from, r.date_to, r.hour_from, r.hour_to, "
        "p.name AS person_name "
        "FROM time_off_requests r "
        "JOIN people p ON p.odoo_id = r.person_odoo_id "
        "WHERE r.state = 'validate' "
        "AND r.date_to >= %s AND r.date_from <= %s "
        "ORDER BY p.name",
        (start_d, end_d),
    )
    by_day: dict = {}
    for r in rows:
        label = _label_for(r)
        cur = max(r["date_from"], start_d)
        end = min(r["date_to"], end_d)
        while cur <= end:
            by_day.setdefault(cur, []).append({
                "name": r["person_name"], "label": label,
            })
            cur = cur + _td(days=1)

    # Merge in company-wide public holidays. Failure is non-fatal — a
    # transient Odoo outage on the calendar route shouldn't break the
    # whole render; the rest of the day's entries (from the local
    # mirror) still show.
    try:
        holidays = odoo_client.fetch_public_holidays(start_d, end_d)
    except Exception:  # noqa: BLE001 — never let the holiday fetch crash render
        holidays = []
    for h in holidays:
        h_start = _parse_holiday_date(h.get("date_from"))
        h_end = _parse_holiday_date(h.get("date_to"))
        if not h_start or not h_end:
            continue
        cur = max(h_start, start_d)
        end = min(h_end, end_d)
        while cur <= end:
            by_day.setdefault(cur, []).append({
                "name": h.get("name") or "Plant Closed",
                "label": "Plant Closed",
                "source": "holiday",
            })
            cur = cur + _td(days=1)
    return by_day


@router.get("/timeclock/time-off/calendar/{token}", response_class=HTMLResponse)
def time_off_calendar(request: Request, token: str, month: str | None = None):
    """Who's Out — a month-grid calendar of approved leaves.

    Builds a standard Mon-first month-datescalendar for the current
    month, padded to full weeks (leading/trailing days from adjacent
    months are flagged ``outside`` so the template can fade them).
    Each cell carries the list of people out that day plus a timing
    label (no leave type — privacy). Token bounces to ``/timeclock`` on
    failure, identical to the other routes in this module."""
    person_id = _verify_token(token)
    if person_id is None:
        return RedirectResponse(url="/timeclock", status_code=303)
    p = _person_by_id(person_id)
    if not p:
        return RedirectResponse(url="/timeclock", status_code=303)
    today = _date.today()
    # Which month to render. `month` ("YYYY-MM") comes from the prev/next nav
    # links; anything missing or malformed falls back to the current month so
    # a stale/typo'd URL never 500s the kiosk.
    first = today.replace(day=1)
    if month:
        try:
            y, m = str(month).split("-")
            first = _date(int(y), int(m), 1)
        except (ValueError, TypeError):
            first = today.replace(day=1)
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    last = next_first - _td(days=1)
    range_start = first - _td(days=first.weekday())
    range_end = last + _td(days=(6 - last.weekday()))
    off_map = _approved_by_day(range_start, range_end)

    weeks = _cal.Calendar(firstweekday=0).monthdatescalendar(
        first.year, first.month,
    )
    week_cells = []
    for week in weeks:
        w = []
        for d in week:
            w.append({
                "num": d.day,
                "outside": d.month != first.month,
                "is_today": d == today,
                "weekend": d.weekday() >= 5,
                "names": off_map.get(d, []),
            })
        week_cells.append(w)

    # Prev/next month anchors (YYYY-MM) for the nav links.
    prev_first = (first - _td(days=1)).replace(day=1)
    fresh = _mint_token(person_id)
    return templates.TemplateResponse(
        request,
        "kiosk_time_off_calendar.html",
        {
            "person": p,
            "token": fresh,
            "heading": first.strftime("%B %Y"),
            "weeks": week_cells,
            "prev_month": prev_first.strftime("%Y-%m"),
            "next_month": next_first.strftime("%Y-%m"),
            "is_current_month": first == today.replace(day=1),
            "bilingual": bool(p.get("spanish_speaker")),
        },
    )
