from __future__ import annotations

from collections.abc import Callable

from fastapi.responses import JSONResponse

LIVE_TRANSFER_MESSAGE = "Use Luke's floor app to move this worker."


def _json_error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


def live_transfer_is_disabled() -> bool:
    """Suppress this legacy action only after positive live activation."""
    from . import attendance_location_policy

    try:
        return attendance_location_policy.live_is_active()
    except Exception:  # noqa: BLE001 - unreadable rollout state stays pre-live
        return False


def transfer(
    body: dict,
    actor_upn=None,
    actor_name=None,
    friendly_error: Callable[[Exception], str] = str,
) -> JSONResponse:
    """Blocking half of /api/exceptions/breakdown/transfer.

    The legacy transfer still uses the station stop for its Odoo move, while
    the exclusion cap is clamped to the worker's personal start so it can
    never create a negative window.
    """
    from . import (
        inbox_keys,
        inbox_log,
        machine_breakdown,
        staffing_transfer,
        wc_attributions,
    )

    if live_transfer_is_disabled():
        return _json_error(LIVE_TRANSFER_MESSAGE, 410)

    incident_id = body.get("incident_id")
    person_name = str(body.get("person_name") or "").strip()
    employee_odoo_id = body.get("employee_odoo_id")
    if employee_odoo_id is not None and (
        isinstance(employee_odoo_id, bool)
        or not isinstance(employee_odoo_id, int)
        or employee_odoo_id <= 0
    ):
        return _json_error("employee_odoo_id must be a positive integer", 400)
    to_wc = str(body.get("to_wc") or "").strip()
    if not incident_id or not person_name or not to_wc:
        return _json_error("incident_id, person_name, and to_wc are required", 400)

    incident = machine_breakdown.get_incident(incident_id)
    if incident is None:
        return _json_error("incident not found", 404)

    transfer_at = incident["detected_stop_utc"]
    row = wc_attributions.open_breakdown_row(
        incident["day"],
        incident["wc_name"],
        person_name,
        employee_odoo_id=employee_odoo_id,
    )
    if row is not None:
        exclusion_end = max(transfer_at, row.get("start_utc") or transfer_at)
        wc_attributions.cap_breakdown(row["id"], exclusion_end)

    # Note: the breakdown-row cap above is not rolled back if decide_and_apply
    # fails below; a failed transfer leaves the operator's exclusion capped
    # as-if-departed. Retrying the transfer re-caps harmlessly (cap_breakdown
    # is idempotent on an already-closed row) but does not reopen the
    # exclusion -- a manual wc_attributions.reopen_breakdown would be needed
    # if this matters in practice. Matches this file's existing convention
    # for Odoo-calling handlers (_approve_time_off_sync, _refuse_time_off_sync):
    # log and return a friendly error, no rollback.
    try:
        if employee_odoo_id is None:
            result = staffing_transfer.decide_and_apply(
                person_name, to_wc, transfer_at
            )
        else:
            result = staffing_transfer.decide_and_apply(
                person_name,
                to_wc,
                transfer_at,
                employee_odoo_id=employee_odoo_id,
            )
    except Exception as e:
        return _json_error(friendly_error(e), 500)

    eid = inbox_log.log_event_safe(
        item_kind="breakdown",
        item_key=inbox_keys.breakdown(
            incident["wc_name"],
            incident["detected_stop_utc"].isoformat(),
            f"odoo:{employee_odoo_id}"
            if employee_odoo_id is not None
            else person_name,
        ),
        person_name=person_name,
        category_label="Machine Breakdown",
        action="transfer",
        outcome=f"Transferred to {to_wc}",
        after_value=to_wc,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
        reversible=True,
        detail={
            "closed_id": result.get("closed_id"),
            "new_id": result.get("new_id"),
            "attribution_id": row["id"] if row is not None else None,
        },
    )
    return JSONResponse({"ok": True, "event_id": eid, "transfer": result.get("transfer")})


def snooze(body: dict) -> JSONResponse:
    """Blocking half of /api/exceptions/breakdown/snooze."""
    from . import machine_breakdown

    incident_id = body.get("incident_id")
    person_name = str(body.get("person_name") or "").strip()
    employee_odoo_id = body.get("employee_odoo_id")
    if not incident_id or not person_name:
        return _json_error("incident_id and person_name are required", 400)
    if employee_odoo_id is not None and (
        isinstance(employee_odoo_id, bool)
        or not isinstance(employee_odoo_id, int)
        or employee_odoo_id <= 0
    ):
        return _json_error("employee_odoo_id must be a positive integer", 400)
    machine_breakdown.snooze_operator(
        incident_id,
        person_name,
        employee_odoo_id=employee_odoo_id,
    )
    return JSONResponse({"ok": True})


def dismiss(body: dict, actor_upn=None, actor_name=None) -> JSONResponse:
    """Blocking half of /api/exceptions/breakdown/dismiss ("Not a
    breakdown"): atomically resolves the incident and receives the exact
    exclusion rows deleted under that same incident lock for undo."""
    from . import inbox_keys, inbox_log, machine_breakdown

    incident_id = body.get("incident_id")
    if not incident_id:
        return _json_error("incident_id is required", 400)
    incident = machine_breakdown.get_incident(incident_id)
    if incident is None:
        return _json_error("incident not found", 404)

    snapshot_rows = machine_breakdown.dismiss_incident(incident_id)
    if snapshot_rows is None:
        return _json_error("incident is already resolved", 409)

    eid = inbox_log.log_event_safe(
        item_kind="breakdown",
        item_key=inbox_keys.breakdown(incident["wc_name"], incident["detected_stop_utc"].isoformat()),
        person_name=None,
        category_label="Machine Breakdown",
        action="dismiss",
        outcome="Not a breakdown",
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
        reversible=True,
        detail={"rows": snapshot_rows, "incident_id": incident_id},
    )
    return JSONResponse({"ok": True, "event_id": eid})


def report(body: dict) -> JSONResponse:
    """Blocking half of /api/exceptions/breakdown/report (the manual
    "+ Report a breakdown" button)."""
    from . import machine_breakdown, staffing

    wc_name = str(body.get("wc_name") or "").strip()
    if wc_name not in {loc.name for loc in staffing.LOCATIONS}:
        return _json_error("unknown work center", 400)
    result = machine_breakdown.report_manual(wc_name)
    return JSONResponse(result)
